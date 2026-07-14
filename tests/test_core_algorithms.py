"""Automated tests for GIS normalization, merging, voxelization, and meshing."""

from __future__ import annotations

from pathlib import Path
import ast
import base64
import json
import unittest
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

from ..domain.enums import (
    BuildingSource,
    FeatureType,
    GeometryType,
    HeightSource,
    GeometryStyle,
    LabelLanguage,
    LabelMode,
    LargeAreaMode,
    InputMode,
)
from ..domain.models import (
    BoundingBox,
    BuildingStatistics,
    GeoFeature,
    GeographicDataset,
    GeoPoint,
    VoxelSettings,
    MapBuildRequest,
)
from ..domain.ports import GeographicDataSource, SceneRepository
from ..application.generation_service import MapGenerationService
from ..infrastructure.geocoding.nominatim import NominatimGeocoder
from ..infrastructure.osm.overpass import OverpassDataSource
from ..infrastructure.osm.query_builder import OverpassQueryBuilder
from ..infrastructure.overture.buildings import OvertureBuildingsDataSource
from ..infrastructure.overture.hybrid import HybridGeographicDataSource
from ..infrastructure.overture.runtime import (
    _local_native_root,
    _pyarrow_private_dlls,
    _shapely_private_dlls,
)
from ..infrastructure.map_selector.metrics import (
    AreaLoadLevel,
    AreaMetricsCalculator,
)
from ..infrastructure.map_selector.session import (
    MapSelectorConfig,
    MapSelectorSessionManager,
)
from ..voxel.curved_details import CurvedDetailBuilder
from ..voxel.labels import MapLabelBuilder
from ..voxel.mesh_builder import GreedyChunkMesher
from ..voxel.projection import LocalMetricProjector
from ..voxel.rasterizer import FeatureRasterizer
from ..voxel.vertical_layout import SemanticVerticalLayout
from ..voxel.world import VoxelWorld


class _FakeHttpClient:
    """Return predefined Nominatim payloads without network access."""

    def __init__(self, payload: list[dict[str, object]]) -> None:
        self.payload = payload

    def get_json(self, _url: str, query: object = None) -> object:
        del query
        return self.payload


class _StaticSource(GeographicDataSource):
    """In-memory geographic source used by hybrid-source tests."""

    def __init__(self, dataset: GeographicDataset) -> None:
        self.dataset = dataset

    def fetch_features(self, _bounds: BoundingBox) -> GeographicDataset:
        return self.dataset


class _StaticGeocoder:
    """Return one configured box for application-service tests."""

    def __init__(self, bounds: BoundingBox) -> None:
        self.bounds = bounds

    def resolve(self, _area_name: str) -> BoundingBox:
        return self.bounds


class _CaptureSceneRepository(SceneRepository):
    """Capture generated statistics without requiring Blender."""

    def __init__(self) -> None:
        self.statistics = None
        self.meshes = ()

    def replace_project(
        self,
        _project_name,
        _bounds,
        _settings,
        meshes,
        _curved_details,
        _labels,
        statistics,
        progress,
    ) -> int:
        self.meshes = tuple(meshes)
        self.statistics = statistics
        progress(1.0, "Captured")
        return len(self.meshes)

    def delete_project(self, _project_name: str) -> int:
        return 0


class CoreAlgorithmTests(unittest.TestCase):
    """Verify the source, height, rasterization, and greedy-mesh pipeline."""

    def test_building_editor_does_not_reopen_duplicate_dialog(self) -> None:
        """Viewport picking must update the existing editor instead of opening a second dialog."""
        editor_path = Path(__file__).parents[1] / "presentation" / "building_editor.py"
        source = editor_path.read_text(encoding="utf-8")
        self.assertNotIn("_reopen_editor_later", source)
        self.assertIn("reset_building_editor_defaults", source)

    def test_registration_class_names_are_bound(self) -> None:
        """Every class referenced by _CLASSES must be imported or defined."""
        registration_path = Path(__file__).parents[1] / "presentation" / "registration.py"
        tree = ast.parse(registration_path.read_text(encoding="utf-8"))
        bound_names: set[str] = set()
        class_names: list[str] = []
        for node in tree.body:
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                bound_names.add(node.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    bound_names.add(alias.asname or alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    bound_names.add(alias.asname or alias.name)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        bound_names.add(target.id)
                if any(
                    isinstance(target, ast.Name) and target.id == "_CLASSES"
                    for target in node.targets
                ):
                    class_names = [
                        item.id for item in node.value.elts if isinstance(item, ast.Name)
                    ]
        missing = [name for name in class_names if name not in bound_names]
        self.assertEqual([], missing)

    def test_registration_has_no_duplicate_classes(self) -> None:
        """Blender classes must be registered once to avoid startup failures."""
        registration_path = Path(__file__).parents[1] / "presentation" / "registration.py"
        tree = ast.parse(registration_path.read_text(encoding="utf-8"))
        class_tuple = next(
            node.value
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "_CLASSES" for target in node.targets)
        )
        names = [
            item.id
            for item in class_tuple.elts
            if isinstance(item, ast.Name)
        ]
        self.assertEqual(len(names), len(set(names)))
        self.assertIn("OVMG_OT_RestoreRecommendedDefaults", names)
        self.assertIn("OVMG_OT_ResetBuildingEditorDefaults", names)

    def test_editable_prism_handles_index_tessellation_results(self) -> None:
        """Prevent Blender 5.1 integer tessellation results from being treated as Vectors."""
        helper_path = (
            Path(__file__).parents[1]
            / "infrastructure"
            / "blender"
            / "editable_buildings.py"
        )
        source = helper_path.read_text(encoding="utf-8")
        self.assertIn("flattened_keys[value]", source)
        self.assertNotIn("round(value.x, 8), round(value.y, 8)) for value in triangle", source)

    def test_static_ui_helpers_do_not_reference_undefined_cls(self) -> None:
        """Prevent Blender wizard steps from failing with a draw-time NameError."""
        panel_path = Path(__file__).parents[1] / "presentation" / "panels.py"
        tree = ast.parse(panel_path.read_text(encoding="utf-8"))
        violations: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            decorators = {
                decorator.id
                for decorator in node.decorator_list
                if isinstance(decorator, ast.Name)
            }
            if "staticmethod" not in decorators:
                continue
            argument_names = {argument.arg for argument in node.args.args}
            referenced_names = {
                child.id for child in ast.walk(node) if isinstance(child, ast.Name)
            }
            if "cls" in referenced_names and "cls" not in argument_names:
                violations.append(f"{node.name} at line {node.lineno}")
        self.assertEqual([], violations)

    def test_extension_local_native_fallback_is_complete(self) -> None:
        """Ship critical native DLLs outside Blender's shared wheel cache."""
        native_root = _local_native_root()
        numpy_names = {path.name for path in (native_root / "numpy.libs").glob("*.dll")}
        pyarrow_names = {path.name for path in (native_root / "pyarrow").glob("*.dll")}
        pyarrow_lib_names = {
            path.name for path in (native_root / "pyarrow.libs").glob("*.dll")
        }
        shapely_names = {path.name for path in (native_root / "shapely.libs").glob("*.dll")}
        self.assertTrue(any(name.startswith("libscipy_openblas") for name in numpy_names))
        self.assertTrue(any(name.startswith("msvcp140-") for name in numpy_names))
        self.assertIn("arrow.dll", pyarrow_names)
        self.assertIn("arrow_compute.dll", pyarrow_names)
        self.assertIn("arrow_python.dll", pyarrow_names)
        self.assertIn("parquet.dll", pyarrow_names)
        self.assertTrue(any(name.startswith("msvcp140-") for name in pyarrow_lib_names))
        self.assertTrue(any(name.startswith("geos-") for name in shapely_names))
        self.assertTrue(any(name.startswith("geos_c-") for name in shapely_names))

    def test_pyarrow_private_dlls_use_dependency_safe_order(self) -> None:
        """Load Arrow core libraries before Python and dataset wrappers."""
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            package_directory = root / "pyarrow"
            libs_directory = root / "pyarrow.libs"
            package_directory.mkdir()
            libs_directory.mkdir()
            for name in (
                "arrow_python.dll",
                "arrow_dataset.dll",
                "arrow_acero.dll",
                "parquet.dll",
                "arrow_compute.dll",
                "arrow.dll",
            ):
                (package_directory / name).touch()
            for name in (
                "msvcp140-deadbeef.dll",
                "msvcp140_atomic_wait-cafebabe.dll",
            ):
                (libs_directory / name).touch()

            ordered = [
                path.name
                for path in _pyarrow_private_dlls(package_directory, libs_directory)
            ]

        self.assertEqual(
            ordered,
            [
                "msvcp140-deadbeef.dll",
                "msvcp140_atomic_wait-cafebabe.dll",
                "arrow.dll",
                "arrow_compute.dll",
                "parquet.dll",
                "arrow_acero.dll",
                "arrow_dataset.dll",
                "arrow_python.dll",
            ],
        )

    def test_shapely_private_dlls_use_dependency_safe_order(self) -> None:
        """Load bundled C++ runtime before GEOS core and GEOS C API."""
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            names = (
                "geos_c-012345.dll",
                "geos-abcdef.dll",
                "msvcp140-fedcba.dll",
                "future_dependency.dll",
            )
            for name in names:
                (directory / name).touch()
            ordered = [path.name for path in _shapely_private_dlls(directory)]

        self.assertEqual(
            ordered,
            [
                "msvcp140-fedcba.dll",
                "geos-abcdef.dll",
                "geos_c-012345.dll",
                "future_dependency.dll",
            ],
        )

    def setUp(self) -> None:
        self.bounds = BoundingBox(33.3800, 44.3600, 33.3840, 44.3640)
        self.projector = LocalMetricProjector(
            self.bounds,
            voxel_size=2.0,
            vertical_step=1.0,
        )

    def settings(self, **overrides: object) -> VoxelSettings:
        """Create compact deterministic settings for unit tests."""
        values: dict[str, object] = {
            "voxel_size": 2.0,
            "vertical_step": 1.0,
            "chunk_size": 32,
            "default_building_height": 9.0,
            "level_height": 3.0,
            "tree_density": 0.0,
            "include_terrain": False,
            "include_roads": True,
            "include_water": True,
            "include_green": True,
            "include_bridges": True,
            "include_trees": False,
        }
        values.update(overrides)
        return VoxelSettings(**values)

    def rectangle_feature(
        self,
        source_id: str,
        tags: dict[str, str] | None = None,
        half_lon: float = 0.00008,
        half_lat: float = 0.00006,
    ) -> GeoFeature:
        """Create a building rectangle centered in the test bounds."""
        center = self.bounds.center
        ring = (
            GeoPoint(center.longitude - half_lon, center.latitude - half_lat),
            GeoPoint(center.longitude + half_lon, center.latitude - half_lat),
            GeoPoint(center.longitude + half_lon, center.latitude + half_lat),
            GeoPoint(center.longitude - half_lon, center.latitude + half_lat),
            GeoPoint(center.longitude - half_lon, center.latitude - half_lat),
        )
        return GeoFeature(
            source_id=source_id,
            feature_type=FeatureType.BUILDING,
            geometry_type=GeometryType.POLYGON,
            tags=tags or {"building": "residential"},
            outer_rings=(ring,),
        )

    def test_overpass_query_contains_required_layers(self) -> None:
        query = OverpassQueryBuilder.build(self.bounds)
        self.assertIn(self.bounds.as_overpass(), query)
        self.assertIn('way["building:part"]', query)
        self.assertIn('node["amenity"="place_of_worship"]', query)
        self.assertIn('way["waterway"="riverbank"]', query)
        self.assertIn(
            'way["landuse"~"^(residential|commercial|retail|mixed_use)$"]',
            query,
        )

    def test_geocoder_prefers_administrative_area_over_tiny_poi(self) -> None:
        payload = [
            {
                "display_name": "Al-Adhamiya School, Baghdad, Iraq",
                "category": "amenity",
                "type": "school",
                "addresstype": "amenity",
                "osm_type": "way",
                "place_rank": 30,
                "importance": 0.60,
                "lat": "33.380764",
                "lon": "44.398869",
                "boundingbox": [
                    "33.380714",
                    "33.380814",
                    "44.397919",
                    "44.399818",
                ],
            },
            {
                "display_name": "Adhamiyah, Baghdad, Iraq",
                "category": "boundary",
                "type": "administrative",
                "addresstype": "district",
                "osm_type": "relation",
                "place_rank": 18,
                "importance": 0.45,
                "lat": "33.38972",
                "lon": "44.37194",
                "boundingbox": [
                    "33.360000",
                    "33.420000",
                    "44.340000",
                    "44.405000",
                ],
            },
        ]
        bounds = NominatimGeocoder(
            "https://example.invalid/search",
            _FakeHttpClient(payload),
        ).resolve("Al-Adhamiya, Baghdad, Iraq")
        self.assertAlmostEqual(bounds.west, 44.34, places=3)
        self.assertAlmostEqual(bounds.east, 44.405, places=3)

    def test_relation_paths_are_joined_into_closed_ring(self) -> None:
        a = GeoPoint(0.0, 0.0)
        b = GeoPoint(1.0, 0.0)
        c = GeoPoint(1.0, 1.0)
        d = GeoPoint(0.0, 1.0)
        rings = OverpassDataSource._assemble_rings([(a, b, c), (c, d, a)])
        self.assertEqual(len(rings), 1)
        self.assertEqual(rings[0][0], rings[0][-1])

    def test_two_by_two_slab_greedy_merges_to_six_quads(self) -> None:
        world = VoxelWorld(chunk_size=32, max_cells=1000)
        world.add_many(
            FeatureType.TERRAIN,
            [(0, 0, 0), (1, 0, 0), (0, 1, 0), (1, 1, 0)],
        )
        meshes = GreedyChunkMesher().build(
            world,
            self.projector,
            lambda _ratio, _message: None,
        )
        self.assertEqual(len(meshes), 1)
        self.assertEqual(len(meshes[0].faces), 6)

    def test_road_surface_is_below_terrain(self) -> None:
        terrain_interval = SemanticVerticalLayout.cell_interval(
            FeatureType.TERRAIN,
            0,
            self.projector.voxel_size,
            self.projector.vertical_step,
        )
        road_interval = SemanticVerticalLayout.cell_interval(
            FeatureType.ROAD,
            0,
            self.projector.voxel_size,
            self.projector.vertical_step,
        )
        self.assertEqual(road_interval, (-0.18, 0.0))
        self.assertGreater(terrain_interval[1], road_interval[1])

    def test_independent_vertical_step_preserves_eleven_meter_height(self) -> None:
        feature = self.rectangle_feature(
            "overture/building/height-test",
            {"building": "residential", "height": "11"},
        )
        world, _projector, report = FeatureRasterizer().rasterize(
            self.bounds,
            [feature],
            self.settings(),
            max_cells=200_000,
            progress=lambda _ratio, _message: None,
        )
        z_layers = {z for _x, _y, z in world.cells(FeatureType.BUILDING)}
        self.assertEqual(min(z_layers), 1)
        self.assertEqual(max(z_layers), 11)
        self.assertEqual(
            report.height_source_counts[HeightSource.REAL_HEIGHT],
            1,
        )

    def test_min_floor_offsets_building_part_vertically(self) -> None:
        feature = self.rectangle_feature(
            "overture/building_part/floating",
            {
                "building:part": "yes",
                "ovmg:is_part": "yes",
                "building:min_level": "2",
                "height": "4",
            },
        )
        world, _projector, _report = FeatureRasterizer().rasterize(
            self.bounds,
            [feature],
            self.settings(),
            max_cells=200_000,
            progress=lambda _ratio, _message: None,
        )
        z_layers = {z for _x, _y, z in world.cells(FeatureType.BUILDING)}
        self.assertEqual(min(z_layers), 7)
        self.assertEqual(max(z_layers), 10)

    def test_parent_is_skipped_when_overture_parts_are_present(self) -> None:
        parent = self.rectangle_feature(
            "overture/building/parent-1",
            {
                "building": "residential",
                "height": "12",
                "ovmg:has_parts": "yes",
            },
            half_lon=0.00012,
            half_lat=0.00010,
        )
        part = self.rectangle_feature(
            "overture/building_part/part-1",
            {
                "building:part": "yes",
                "ovmg:is_part": "yes",
                "ovmg:building_id": "parent-1",
                "height": "6",
            },
        )
        _world, _projector, report = FeatureRasterizer().rasterize(
            self.bounds,
            [parent, part],
            self.settings(use_building_parts=True),
            max_cells=200_000,
            progress=lambda _ratio, _message: None,
        )
        self.assertEqual(report.skipped_parent_buildings, 1)
        self.assertEqual(
            report.height_source_counts[HeightSource.BUILDING_PART],
            1,
        )

    def test_stepped_roof_narrows_toward_top(self) -> None:
        feature = self.rectangle_feature(
            "overture/building/roofed",
            {
                "building": "residential",
                "height": "8",
                "roof:height": "3",
                "roof:shape": "dome",
            },
            half_lon=0.00013,
            half_lat=0.00010,
        )
        world, _projector, _report = FeatureRasterizer().rasterize(
            self.bounds,
            [feature],
            self.settings(use_roof_shapes=True),
            max_cells=200_000,
            progress=lambda _ratio, _message: None,
        )
        counts_by_layer: dict[int, int] = {}
        for _x, _y, z in world.cells(FeatureType.BUILDING):
            counts_by_layer[z] = counts_by_layer.get(z, 0) + 1
        self.assertLess(counts_by_layer[max(counts_by_layer)], counts_by_layer[1])

    def test_small_non_landmark_footprint_is_discarded(self) -> None:
        tiny = self.rectangle_feature(
            "overture/building/tiny",
            {"building": "shed", "height": "3"},
            half_lon=0.000004,
            half_lat=0.000004,
        )
        world, _projector, report = FeatureRasterizer().rasterize(
            self.bounds,
            [tiny],
            self.settings(minimum_building_area=12.0),
            max_cells=200_000,
            progress=lambda _ratio, _message: None,
        )
        self.assertFalse(world.cells(FeatureType.BUILDING))
        self.assertEqual(report.skipped_small_buildings, 1)

    def test_approximate_buildings_are_off_by_default(self) -> None:
        center = self.bounds.center
        road = GeoFeature(
            source_id="way/road",
            feature_type=FeatureType.ROAD,
            geometry_type=GeometryType.LINE,
            tags={"highway": "residential"},
            points=(
                GeoPoint(self.bounds.west, center.latitude),
                GeoPoint(self.bounds.east, center.latitude),
            ),
        )
        world, _projector, _report = FeatureRasterizer().rasterize(
            self.bounds,
            [road],
            self.settings(generate_approximate_buildings=False),
            max_cells=200_000,
            progress=lambda _ratio, _message: None,
        )
        self.assertFalse(world.cells(FeatureType.BUILDING))

    def test_major_river_enforces_configured_minimum_width(self) -> None:
        width = FeatureRasterizer()._water_width(
            {"waterway": "river", "width": "12", "name": "Tigris"},
            self.settings(fallback_river_width=220.0),
        )
        self.assertEqual(width, 220.0)

    def test_overture_row_parser_preserves_height_parts_and_sources(self) -> None:
        import shapely

        polygon = shapely.Polygon(
            [
                (44.3610, 33.3810),
                (44.3612, 33.3810),
                (44.3612, 33.3812),
                (44.3610, 33.3812),
                (44.3610, 33.3810),
            ]
        )
        row = {
            "id": "part-123",
            "geometry": shapely.to_wkb(polygon),
            "building_id": "building-456",
            "height": 14.5,
            "min_floor": 2,
            "roof_shape": "gabled",
            "sources": [
                {"dataset": "OpenStreetMap", "record_id": "w987@3"},
                {"dataset": "Example", "record_id": "way/654"},
            ],
        }
        feature = OvertureBuildingsDataSource(True)._row_to_feature(
            row,
            "building_part",
            shapely,
        )
        self.assertIsNotNone(feature)
        assert feature is not None
        self.assertEqual(feature.tags["height"], "14.5")
        self.assertEqual(feature.tags["building:min_level"], "2")
        self.assertEqual(feature.tags["roof:shape"], "gabled")
        self.assertEqual(feature.tags["ovmg:building_id"], "building-456")
        self.assertIn("way/987", feature.tags["ovmg:osm_ids"])
        self.assertIn("way/654", feature.tags["ovmg:osm_ids"])

    def test_hybrid_source_removes_overture_osm_duplicate(self) -> None:
        osm_building = self.rectangle_feature(
            "way/10",
            {
                "building": "residential",
                "building:levels": "4",
                "roof:shape": "gabled",
                "building:material": "brick",
                "building:colour": "#b07050",
                "ovmg:source": "osm",
            },
        )
        center = self.bounds.center
        road = GeoFeature(
            source_id="way/20",
            feature_type=FeatureType.ROAD,
            geometry_type=GeometryType.LINE,
            tags={"highway": "residential", "ovmg:source": "osm"},
            points=(
                GeoPoint(self.bounds.west, center.latitude),
                GeoPoint(self.bounds.east, center.latitude),
            ),
        )
        overture_building = self.rectangle_feature(
            "overture/building/abc",
            {
                "building": "residential",
                "ovmg:source": "overture",
                "ovmg:osm_ids": "way/10",
            },
        )
        source = HybridGeographicDataSource(
            osm_source=_StaticSource(
                GeographicDataset(
                    features=(road, osm_building),
                    building_statistics=BuildingStatistics(osm_buildings=1),
                )
            ),
            overture_source=_StaticSource(
                GeographicDataset(
                    features=(overture_building,),
                    building_statistics=BuildingStatistics(
                        overture_buildings=1,
                        final_building_features=1,
                        overture_release="test-release",
                    ),
                    attributions=("© Overture Maps Foundation",),
                )
            ),
            building_source=BuildingSource.HYBRID,
            use_building_parts=True,
        )
        dataset = source.fetch_features(self.bounds)
        buildings = [
            feature
            for feature in dataset.features
            if feature.feature_type is FeatureType.BUILDING
        ]
        self.assertEqual(len(buildings), 1)
        self.assertEqual(buildings[0].source_id, "overture/building/abc")
        self.assertEqual(buildings[0].tags["building:levels"], "4")
        self.assertEqual(buildings[0].tags["roof:shape"], "gabled")
        self.assertEqual(buildings[0].tags["building:material"], "brick")
        self.assertEqual(buildings[0].tags["building:colour"], "#b07050")
        self.assertEqual(
            buildings[0].tags["ovmg:attribute_fusion"],
            "overture_geometry+osm_attributes",
        )
        self.assertEqual(dataset.building_statistics.merged_duplicates, 1)
        self.assertIn(road, dataset.features)

    def test_curved_details_are_strictly_optional(self) -> None:
        feature = self.rectangle_feature(
            "osm/way/mosque",
            {
                "building": "mosque",
                "amenity": "place_of_worship",
                "roof:shape": "dome",
                "height": "16",
            },
        )
        disabled = CurvedDetailBuilder().build(
            [feature],
            self.projector,
            self.settings(geometry_style=GeometryStyle.VOXEL_ONLY),
            "Test",
        )
        enabled = CurvedDetailBuilder().build(
            [feature],
            self.projector,
            self.settings(
                geometry_style=GeometryStyle.VOXEL_CURVED,
                curved_detail_segments=12,
            ),
            "Test",
        )
        self.assertEqual(disabled, ())
        self.assertEqual(len(enabled), 1)
        self.assertGreater(len(enabled[0].vertices), 12)
        self.assertGreater(len(enabled[0].faces), 12)

    def test_label_builder_deduplicates_named_street_segments(self) -> None:
        center = self.bounds.center
        road_a = GeoFeature(
            source_id="way/1",
            feature_type=FeatureType.ROAD,
            geometry_type=GeometryType.LINE,
            tags={
                "highway": "primary",
                "name": "Main Street",
                "name:ar": "الشارع الرئيسي",
            },
            points=(
                GeoPoint(center.longitude - 0.0008, center.latitude),
                GeoPoint(center.longitude + 0.0008, center.latitude),
            ),
        )
        road_b = GeoFeature(
            source_id="way/2",
            feature_type=FeatureType.ROAD,
            geometry_type=GeometryType.LINE,
            tags={"highway": "residential", "name": "Main Street"},
            points=(
                GeoPoint(center.longitude - 0.0002, center.latitude + 0.0002),
                GeoPoint(center.longitude + 0.0002, center.latitude + 0.0002),
            ),
        )
        labels = MapLabelBuilder().build(
            [road_a, road_b],
            self.bounds,
            self.projector,
            self.settings(
                generate_labels=True,
                label_mode=LabelMode.METADATA_ONLY,
                label_language=LabelLanguage.ARABIC,
                include_area_labels=False,
                include_landmark_labels=False,
            ),
            "Adhamiya",
        )
        self.assertEqual(len(labels), 1)
        self.assertEqual(labels[0].text, "الشارع الرئيسي")


class CrashSafePreviewTests(unittest.TestCase):
    """Guard the browser-to-Blender handoff against unsafe UI operations."""

    def test_async_poll_does_not_create_images_or_invoke_popup(self) -> None:
        package = Path(__file__).resolve().parents[1]
        operators = (package / "presentation" / "operators.py").read_text()
        thumbnail = (
            package
            / "infrastructure"
            / "blender"
            / "selection_thumbnail.py"
        ).read_text()
        poll = operators.split("def _poll_map_selector_result", 1)[1].split(
            "class OVMG_OT_SelectAreaOnMap", 1
        )[0]
        self.assertNotIn("bpy.ops", poll)
        self.assertNotIn("bpy.data.images.load", poll)
        self.assertNotIn("template_preview", thumbnail)
        self.assertIn("bpy.utils.previews", thumbnail)


class TiledGenerationTests(unittest.TestCase):
    """Verify aligned subdivision for oversized selected areas."""

    def test_grid_windows_share_exact_world_boundary(self) -> None:
        bounds = BoundingBox(33.38, 44.36, 33.40, 44.38)
        projector = LocalMetricProjector(bounds, voxel_size=5.0, vertical_step=1.0)
        left = projector.grid_window(0, 0, 32, 32)
        right = projector.grid_window(32, 0, 32, 32)
        self.assertEqual(
            left.grid_vertex_to_world(32, 0, 0),
            right.grid_vertex_to_world(0, 0, 0),
        )

    def test_service_splits_oversized_area_into_aligned_tiles(self) -> None:
        bounds = BoundingBox(33.38, 44.36, 33.40, 44.38)
        repository = _CaptureSceneRepository()
        service = MapGenerationService(
            geocoder=_StaticGeocoder(bounds),
            data_source=_StaticSource(GeographicDataset(features=())),
            rasterizer=FeatureRasterizer(),
            mesher=GreedyChunkMesher(),
            curved_detail_builder=CurvedDetailBuilder(),
            label_builder=MapLabelBuilder(),
            scene_repository=repository,
        )
        settings = VoxelSettings(
            voxel_size=10.0,
            vertical_step=1.0,
            chunk_size=32,
            default_building_height=9.0,
            level_height=3.0,
            tree_density=0.0,
            include_buildings=False,
            include_roads=False,
            include_water=False,
            include_green=False,
            include_bridges=False,
            include_trees=False,
        )
        result = service.generate(
            MapBuildRequest(
                project_name="TileTest",
                input_mode=InputMode.VISUAL_MAP,
                area_name="Tile Test",
                bounding_box=bounds,
                voxel=settings,
                max_voxel_cells=100_000,
                large_area_mode=LargeAreaMode.SPLIT_TILES,
            ),
            lambda _ratio, _message: None,
        )
        self.assertGreater(result.statistics.tile_count, 1)
        self.assertEqual(result.statistics.tile_count, repository.statistics.tile_count)
        self.assertGreater(result.statistics.voxel_count, 0)


class MapSelectorTests(unittest.TestCase):
    """Verify load estimation and localhost selector transport."""

    def tearDown(self) -> None:
        MapSelectorSessionManager.stop()

    def test_area_metrics_classify_safe_and_large_requests(self) -> None:
        small = BoundingBox(33.38, 44.36, 33.39, 44.37)
        safe = AreaMetricsCalculator.calculate(small, 5.0, 12_000_000)
        self.assertEqual(safe.load_level, AreaLoadLevel.SAFE)
        large = BoundingBox(33.30, 44.20, 33.50, 44.50)
        heavy = AreaMetricsCalculator.calculate(large, 1.0, 12_000_000)
        self.assertEqual(heavy.load_level, AreaLoadLevel.TOO_LARGE)
        self.assertGreater(heavy.estimated_surface_cells, safe.estimated_surface_cells)

    def test_high_quality_preflight_reserves_vertical_detail_budget(self) -> None:
        bounds = BoundingBox(33.2613, 44.3615, 33.2847, 44.3915)
        surface_only = AreaMetricsCalculator.calculate(bounds, 1.5, 12_000_000)
        high_quality = AreaMetricsCalculator.calculate(bounds, 1.5, 12_000_000, 4.0)
        self.assertEqual(surface_only.load_level, AreaLoadLevel.SAFE)
        self.assertEqual(high_quality.load_level, AreaLoadLevel.TOO_LARGE)
        self.assertEqual(
            high_quality.estimated_generation_cells,
            high_quality.estimated_surface_cells * 4,
        )

    def test_local_selector_accepts_valid_rectangle(self) -> None:
        bounds = BoundingBox(33.38, 44.36, 33.39, 44.37)
        url = MapSelectorSessionManager.start(
            MapSelectorConfig(
                scene_name="Scene",
                area_name="Adhamiya",
                bounds=bounds,
                voxel_size=2.5,
                max_voxel_cells=12_000_000,
                quality_name="Medium",
                nominatim_endpoint="https://example.invalid/search",
                user_agent="OVMG-Test",
                network_timeout=5,
            )
        )
        parsed = urlparse(url)
        token = parse_qs(parsed.query)["token"][0]
        root = f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"
        with urlopen(f"{root}/config?token={token}", timeout=5) as response:
            config = json.loads(response.read().decode("utf-8"))
        self.assertEqual(config["quality"], "Medium")

        thumbnail = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
            "/x8AAusB9Wl6WbQAAAAASUVORK5CYII="
        )
        payload = json.dumps(
            {
                "south": 33.381,
                "west": 44.361,
                "north": 33.388,
                "east": 44.369,
                "display_name": "Selected Adhamiya",
                "thumbnail": (
                    "data:image/png;base64,"
                    + base64.b64encode(thumbnail).decode("ascii")
                ),
            }
        ).encode("utf-8")
        request = Request(
            f"{root}/selection?token={token}",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=5) as response:
            result_payload = json.loads(response.read().decode("utf-8"))
        self.assertTrue(result_payload["ok"])
        result = MapSelectorSessionManager.consume_result()
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.display_name, "Selected Adhamiya")
        self.assertAlmostEqual(result.bounds.south, 33.381)
        self.assertEqual(result.thumbnail_mime_type, "image/png")
        self.assertEqual(result.thumbnail_bytes, thumbnail)
        self.assertEqual(MapSelectorSessionManager.active_url(), url)

    def test_selector_html_exposes_resize_handles_and_confirmation_copy(self) -> None:
        html_path = (
            Path(__file__).parents[1]
            / "infrastructure"
            / "map_selector"
            / "web"
            / "index.html"
        )
        html = html_path.read_text(encoding="utf-8")
        self.assertIn("ovmg-handle", html)
        self.assertIn("Send Preview to Blender", html)
        self.assertIn("Review and Confirm", html)

    def test_simplified_building_editor_is_registered(self) -> None:
        """Expose the on-demand dialog and direct viewport pickers."""
        registration_path = Path(__file__).parents[1] / "presentation" / "registration.py"
        source = registration_path.read_text(encoding="utf-8")
        required = (
            "OVMG_OT_OpenBuildingEditor",
            "OVMG_OT_PickBuildingInteractive",
            "OVMG_OT_PlaceBuildingInteractive",
            "OVMG_OT_MakeInspectedBuildingEditable",
        )
        for name in required:
            self.assertIn(name, source)

    def test_public_panel_has_no_post_generation_editor_workspace(self) -> None:
        """Keep the public interface limited to the six generation steps."""
        panel_path = Path(__file__).parents[1] / "presentation" / "panels.py"
        source = panel_path.read_text(encoding="utf-8")
        self.assertNotIn("def _draw_ready", source)
        self.assertNotIn('text="Edit Buildings"', source)
        self.assertNotIn('text="Advanced Options"', source)
        self.assertIn('text="Map generated successfully"', source)

    def test_building_editor_supports_direct_click_workflow(self) -> None:
        """Select and place buildings without manual 3D Cursor positioning."""
        editor_path = Path(__file__).parents[1] / "presentation" / "building_editor.py"
        source = editor_path.read_text(encoding="utf-8")
        self.assertIn("scene.ray_cast", source)
        self.assertIn('item.type == "WINDOW"', source)
        self.assertIn("event.mouse_x - region.x", source)
        self.assertIn('bl_idname = "ovmg.pick_building_interactive"', source)
        self.assertIn('bl_idname = "ovmg.place_building_interactive"', source)
        self.assertIn('bl_idname = "ovmg.open_building_editor"', source)
        self.assertIn("building_editor_mode", source)

    def test_building_editor_presets_are_available(self) -> None:
        """Provide practical one-click presets for common missing buildings."""
        properties_path = Path(__file__).parents[1] / "presentation" / "properties.py"
        source = properties_path.read_text(encoding="utf-8")
        for preset in ("SMALL_HOUSE", "RESIDENTIAL", "COMMERCIAL", "MOSQUE", "TOWER"):
            self.assertIn(f'"{preset}"', source)
        self.assertIn("_apply_building_editor_preset", source)


if __name__ == "__main__":
    unittest.main()

class WizardAndHistoryTests(unittest.TestCase):
    """Verify v1.4 guided-workflow domain helpers without Blender."""

    def test_area_history_adds_deduplicates_and_finds(self) -> None:
        from ..infrastructure.map_selector.history import AreaHistoryStore

        class Preferences:
            saved_area_history_json = ""
            saved_area_limit = 3

        preferences = Preferences()
        bounds = BoundingBox(33.35, 44.34, 33.38, 44.39)
        first = AreaHistoryStore.add(preferences, "Adhamiya", bounds)
        second = AreaHistoryStore.add(preferences, "Adhamiya Updated", bounds)
        entries = AreaHistoryStore.load(preferences)
        self.assertEqual(len(entries), 1)
        self.assertEqual(first.key, second.key)
        self.assertEqual(entries[0].name, "Adhamiya Updated")
        self.assertIsNotNone(AreaHistoryStore.find(preferences, first.key))

    def test_real_and_low_poly_styles_enable_curved_details(self) -> None:
        from ..domain.enums import MaterialStyle, ModelStyle

        real = self._settings(ModelStyle.REAL, MaterialStyle.REALISTIC)
        low_poly = self._settings(ModelStyle.LOW_POLY, MaterialStyle.SIMPLE)
        minecraft = self._settings(ModelStyle.MINECRAFT, MaterialStyle.SIMPLE)
        self.assertTrue(real.use_curved_details)
        self.assertTrue(low_poly.use_curved_details)
        self.assertFalse(minecraft.use_curved_details)

    @staticmethod
    def _settings(model_style, material_style) -> VoxelSettings:
        return VoxelSettings(
            voxel_size=2.5,
            vertical_step=1.0,
            chunk_size=64,
            default_building_height=9.0,
            level_height=3.0,
            tree_density=0.2,
            model_style=model_style,
            material_style=material_style,
        )

class OvertureFallbackTests(unittest.TestCase):
    """Verify Hybrid mode remains usable when optional binary wheels fail."""

    class _FailingOvertureSource(GeographicDataSource):
        def fetch_features(self, _bounds: BoundingBox) -> GeographicDataset:
            from ..core.exceptions import DependencyError

            raise DependencyError("pyarrow could not load")

    @staticmethod
    def _osm_dataset() -> GeographicDataset:
        building = GeoFeature(
            source_id="way/1",
            feature_type=FeatureType.BUILDING,
            geometry_type=GeometryType.POLYGON,
            tags={"building": "yes"},
            outer_rings=((
                GeoPoint(44.0, 33.0),
                GeoPoint(44.001, 33.0),
                GeoPoint(44.001, 33.001),
                GeoPoint(44.0, 33.001),
                GeoPoint(44.0, 33.0),
            ),),
        )
        return GeographicDataset(features=(building,))

    def test_hybrid_falls_back_to_osm_with_warning(self) -> None:
        source = HybridGeographicDataSource(
            osm_source=_StaticSource(self._osm_dataset()),
            overture_source=self._FailingOvertureSource(),
            building_source=BuildingSource.HYBRID,
            use_building_parts=True,
        )
        result = source.fetch_features(BoundingBox(33.0, 44.0, 33.01, 44.01))
        self.assertEqual(len(result.features), 1)
        self.assertEqual(result.building_statistics.osm_buildings, 1)
        self.assertTrue(result.warnings)
        self.assertIn("continued with OpenStreetMap", result.warnings[0])

    def test_overture_only_keeps_dependency_error(self) -> None:
        from ..core.exceptions import DependencyError

        source = HybridGeographicDataSource(
            osm_source=_StaticSource(self._osm_dataset()),
            overture_source=self._FailingOvertureSource(),
            building_source=BuildingSource.OVERTURE_ONLY,
            use_building_parts=True,
        )
        with self.assertRaises(DependencyError):
            source.fetch_features(BoundingBox(33.0, 44.0, 33.01, 44.01))


class BuildingAccuracyAndStyleTests(unittest.TestCase):
    """Verify v1.5 building provenance and genuinely different style geometry."""

    def test_shapely_private_dlls_use_dependency_safe_order(self) -> None:
        """Load bundled C++ runtime before GEOS core and GEOS C API."""
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            names = (
                "geos_c-012345.dll",
                "geos-abcdef.dll",
                "msvcp140-fedcba.dll",
                "future_dependency.dll",
            )
            for name in names:
                (directory / name).touch()
            ordered = [path.name for path in _shapely_private_dlls(directory)]

        self.assertEqual(
            ordered,
            [
                "msvcp140-fedcba.dll",
                "geos-abcdef.dll",
                "geos_c-012345.dll",
                "future_dependency.dll",
            ],
        )

    def setUp(self) -> None:
        from ..domain.enums import MaterialStyle, ModelStyle

        self.MaterialStyle = MaterialStyle
        self.ModelStyle = ModelStyle
        self.bounds = BoundingBox(33.38, 44.36, 33.384, 44.364)
        self.projector = LocalMetricProjector(
            self.bounds,
            voxel_size=2.0,
            vertical_step=1.0,
        )
        center = self.bounds.center
        ring = (
            GeoPoint(center.longitude - 0.000071, center.latitude - 0.000052),
            GeoPoint(center.longitude + 0.000083, center.latitude - 0.000052),
            GeoPoint(center.longitude + 0.000083, center.latitude + 0.000061),
            GeoPoint(center.longitude - 0.000071, center.latitude + 0.000061),
            GeoPoint(center.longitude - 0.000071, center.latitude - 0.000052),
        )
        self.feature = GeoFeature(
            source_id="overture/building/accuracy-test",
            feature_type=FeatureType.BUILDING,
            geometry_type=GeometryType.POLYGON,
            tags={
                "building": "apartments",
                "height": "17.4",
                "roof:shape": "gabled",
                "roof:height": "2.4",
                "building:material": "brick",
                "building:colour": "#b57b55",
                "ovmg:source": "overture",
                "ovmg:source_datasets": "OpenStreetMap, Microsoft ML Buildings",
            },
            outer_rings=(ring,),
        )

    def settings(self, style, **overrides) -> VoxelSettings:
        values = dict(
            voxel_size=2.0,
            vertical_step=1.0,
            chunk_size=32,
            default_building_height=9.0,
            level_height=3.0,
            tree_density=0.0,
            include_terrain=False,
            include_roads=False,
            include_water=False,
            include_green=False,
            include_bridges=False,
            include_trees=False,
            model_style=style,
            material_style=self.MaterialStyle.REALISTIC,
            generate_facade_detail=True,
            use_source_facade_hints=True,
        )
        values.update(overrides)
        return VoxelSettings(**values)

    def test_analyzer_preserves_direct_height_roof_and_facade_sources(self) -> None:
        from ..domain.enums import FacadeSource, HeightSource, RoofSource
        from ..voxel.building_analysis import BuildingAnalyzer

        analyses, records, statistics = BuildingAnalyzer().analyze(
            [self.feature],
            self.projector,
            self.settings(self.ModelStyle.REAL),
        )
        result = analyses[self.feature.source_id]
        self.assertAlmostEqual(result.height_m, 17.4, places=3)
        self.assertEqual(result.height_source, HeightSource.REAL_HEIGHT)
        self.assertEqual(result.roof_source, RoofSource.SOURCE_TAG)
        self.assertEqual(result.facade_source, FacadeSource.SOURCE_MATERIAL)
        self.assertEqual(result.facade_profile, "residential_brick")
        self.assertEqual(len(records), 1)
        self.assertEqual(statistics.high_confidence, 1)

    def test_real_and_low_poly_build_direct_geometry_but_minecraft_does_not(self) -> None:
        from ..voxel.building_analysis import BuildingAnalyzer
        from ..voxel.building_mesh_builder import DirectBuildingMeshBuilder

        builder = DirectBuildingMeshBuilder()
        real_settings = self.settings(self.ModelStyle.REAL)
        real_analysis, _, _ = BuildingAnalyzer().analyze(
            [self.feature], self.projector, real_settings
        )
        real_meshes = builder.build(
            [self.feature], real_analysis, self.projector, real_settings, "RealTest"
        )
        self.assertTrue(real_meshes)
        self.assertIn("Real_Buildings", real_meshes[0].collection_group)
        self.assertIn("building|residential_brick", real_meshes[0].material_variant)

        low_settings = self.settings(self.ModelStyle.LOW_POLY)
        low_analysis, _, _ = BuildingAnalyzer().analyze(
            [self.feature], self.projector, low_settings
        )
        low_meshes = builder.build(
            [self.feature], low_analysis, self.projector, low_settings, "LowTest"
        )
        self.assertTrue(low_meshes)
        self.assertLessEqual(len(low_meshes[0].vertices), len(real_meshes[0].vertices))

        minecraft_settings = self.settings(self.ModelStyle.MINECRAFT)
        minecraft_analysis, _, _ = BuildingAnalyzer().analyze(
            [self.feature], self.projector, minecraft_settings
        )
        self.assertEqual(
            builder.build(
                [self.feature],
                minecraft_analysis,
                self.projector,
                minecraft_settings,
                "MinecraftTest",
            ),
            [],
        )

    def test_minecraft_preserves_source_height_and_keeps_block_roof(self) -> None:
        from ..domain.enums import RoofSource
        from ..voxel.building_analysis import BuildingAnalyzer

        settings = self.settings(self.ModelStyle.MINECRAFT)
        analyses, _, _ = BuildingAnalyzer().analyze(
            [self.feature], self.projector, settings
        )
        result = analyses[self.feature.source_id]
        self.assertAlmostEqual(result.height_m, 17.4, places=3)
        self.assertEqual(result.roof_shape, "flat")
        self.assertEqual(result.roof_source, RoofSource.FLAT_DEFAULT)

    def test_accuracy_overlay_is_optional_and_has_confidence_variant(self) -> None:
        from ..voxel.accuracy_overlay import AccuracyOverlayBuilder
        from ..voxel.building_analysis import BuildingAnalyzer

        disabled = self.settings(self.ModelStyle.REAL, show_accuracy_overlay=False)
        analyses, _, _ = BuildingAnalyzer().analyze(
            [self.feature], self.projector, disabled
        )
        self.assertEqual(
            AccuracyOverlayBuilder().build(
                [self.feature], analyses, self.projector, disabled, "Test"
            ),
            [],
        )
        enabled = self.settings(
            self.ModelStyle.REAL,
            show_accuracy_overlay=True,
            accuracy_overlay_limit=10,
        )
        analyses, _, _ = BuildingAnalyzer().analyze(
            [self.feature], self.projector, enabled
        )
        payloads = AccuracyOverlayBuilder().build(
            [self.feature], analyses, self.projector, enabled, "Test"
        )
        self.assertTrue(payloads)
        self.assertTrue(payloads[0].material_variant.startswith("accuracy|"))

    def test_selector_poll_does_not_reference_undefined_stats(self) -> None:
        operators_path = Path(__file__).parents[1] / "presentation" / "operators.py"
        tree = ast.parse(operators_path.read_text(encoding="utf-8"))
        poll = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "_poll_map_selector_result"
        )
        local_names = {
            target.id
            for node in ast.walk(poll)
            if isinstance(node, (ast.Assign, ast.AnnAssign))
            for target in (
                node.targets if isinstance(node, ast.Assign) else [node.target]
            )
            if isinstance(target, ast.Name)
        }
        references = {
            node.id for node in ast.walk(poll) if isinstance(node, ast.Name)
        }
        self.assertFalse("stats" in references and "stats" not in local_names)


class BuildingCorrectionCoreTests(unittest.TestCase):
    """Verify v1.6 persistent correction data and source suppression."""

    def setUp(self) -> None:
        self.bounds = BoundingBox(33.38, 44.36, 33.384, 44.364)
        self.projector = LocalMetricProjector(
            self.bounds,
            voxel_size=2.0,
            vertical_step=1.0,
        )
        center = self.bounds.center
        self.ring = (
            GeoPoint(center.longitude - 0.00006, center.latitude - 0.00005),
            GeoPoint(center.longitude + 0.00006, center.latitude - 0.00005),
            GeoPoint(center.longitude + 0.00006, center.latitude + 0.00005),
            GeoPoint(center.longitude - 0.00006, center.latitude + 0.00005),
            GeoPoint(center.longitude - 0.00006, center.latitude - 0.00005),
        )
        self.feature = GeoFeature(
            source_id="overture/building/correction-test",
            feature_type=FeatureType.BUILDING,
            geometry_type=GeometryType.POLYGON,
            tags={
                "building": "residential",
                "height": "12.5",
                "roof:shape": "gabled",
                "roof:height": "2.0",
                "building:colour": "#c0a080",
                "roof:colour": "#604830",
                "ovmg:source": "overture",
            },
            outer_rings=(self.ring,),
        )

    def settings(self, **overrides: object) -> VoxelSettings:
        values: dict[str, object] = {
            "voxel_size": 2.0,
            "vertical_step": 1.0,
            "chunk_size": 32,
            "default_building_height": 9.0,
            "level_height": 3.0,
            "tree_density": 0.0,
            "include_terrain": False,
            "include_roads": False,
            "include_water": False,
            "include_green": False,
            "include_bridges": False,
            "include_trees": False,
        }
        values.update(overrides)
        return VoxelSettings(**values)

    def test_accuracy_record_contains_editable_local_footprint(self) -> None:
        from ..voxel.building_analysis import BuildingAnalyzer

        _analyses, records, _statistics = BuildingAnalyzer().analyze(
            [self.feature],
            self.projector,
            self.settings(),
        )
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertTrue(record.outer_rings_xy)
        self.assertEqual(record.outer_rings_xy[0][0], record.outer_rings_xy[0][-1])
        self.assertAlmostEqual(record.roof_height_m, 2.0, places=3)
        self.assertEqual(record.facade_color, "#c0a080")
        self.assertEqual(record.roof_color, "#604830")

    def test_suppressed_source_is_removed_before_generation(self) -> None:
        dataset = GeographicDataset(
            features=(self.feature,),
            building_statistics=BuildingStatistics(
                overture_buildings=1,
                final_building_features=1,
            ),
        )
        filtered = MapGenerationService._filter_suppressed_buildings(
            dataset,
            (self.feature.source_id,),
        )
        self.assertEqual(filtered.features, ())
        self.assertEqual(filtered.building_statistics.overture_buildings, 0)
        self.assertEqual(filtered.building_statistics.final_building_features, 0)
        self.assertTrue(any("correction exclusion" in item for item in filtered.warnings))

    def test_unsuppressed_dataset_is_returned_unchanged(self) -> None:
        dataset = GeographicDataset(features=(self.feature,))
        self.assertIs(
            MapGenerationService._filter_suppressed_buildings(dataset, ()),
            dataset,
        )

    def test_registration_contains_building_correction_operators(self) -> None:
        registration_path = Path(__file__).parents[1] / "presentation" / "registration.py"
        source = registration_path.read_text(encoding="utf-8")
        required = (
            "OVMG_OT_CreateBuildingReplacement",
            "OVMG_OT_MarkInspectedBuildingDeleted",
            "OVMG_OT_RestoreInspectedBuilding",
            "OVMG_OT_ApplySelectedUserBuilding",
            "OVMG_OT_RegenerateWithCorrections",
            "OVMG_OT_ClearBuildingCorrections",
        )
        for name in required:
            self.assertIn(name, source)

    def test_numeric_editor_rebuilds_stored_footprint_and_roof(self) -> None:
        editable_path = (
            Path(__file__).parents[1]
            / "infrastructure"
            / "blender"
            / "editable_buildings.py"
        )
        editable_source = editable_path.read_text(encoding="utf-8")
        operators_source = (
            Path(__file__).parents[1] / "presentation" / "operators.py"
        ).read_text(encoding="utf-8")
        self.assertIn('obj["ovmg_footprint_outer_json"]', editable_source)
        self.assertIn("def rebuild_editable_building", editable_source)
        self.assertIn("rebuilt = rebuild_editable_building", operators_source)


class StableDefaultSafetyTests(unittest.TestCase):
    """Verify the v1.7.3 simplified and always-safe generation policy."""

    def setUp(self) -> None:
        self.bounds = BoundingBox(33.38, 44.36, 33.39, 44.37)
        self.projector = LocalMetricProjector(
            self.bounds,
            voxel_size=2.5,
            vertical_step=1.0,
        )
        self.settings = VoxelSettings(
            voxel_size=2.5,
            vertical_step=1.0,
            chunk_size=64,
            default_building_height=9.0,
            level_height=3.0,
            tree_density=0.2,
            use_building_parts=False,
            generate_landmark_proxies=False,
            generate_approximate_buildings=False,
            minimum_building_area=15.0,
        )

    def test_oversized_building_is_rejected_before_meshing(self) -> None:
        from ..voxel.building_validation import BuildingSafetyValidator

        ring = (
            GeoPoint(44.3601, 33.3801),
            GeoPoint(44.3699, 33.3801),
            GeoPoint(44.3699, 33.3899),
            GeoPoint(44.3601, 33.3899),
            GeoPoint(44.3601, 33.3801),
        )
        feature = GeoFeature(
            source_id="overture/building/giant-invalid",
            feature_type=FeatureType.BUILDING,
            geometry_type=GeometryType.POLYGON,
            tags={"building": "yes", "ovmg:source": "overture"},
            outer_rings=(ring,),
        )
        filtered, report = BuildingSafetyValidator().filter_dataset(
            GeographicDataset(features=(feature,)),
            self.projector,
            self.settings,
        )
        self.assertEqual(filtered.features, ())
        self.assertEqual(report.rejected_oversized, 1)

    def test_optional_proxy_and_approximate_buildings_are_rejected(self) -> None:
        from ..voxel.building_validation import BuildingSafetyValidator

        center = self.bounds.center
        proxy = GeoFeature(
            source_id="node/proxy",
            feature_type=FeatureType.BUILDING,
            geometry_type=GeometryType.POINT,
            tags={"amenity": "place_of_worship"},
            points=(center,),
        )
        approximate = GeoFeature(
            source_id="approximate/1",
            feature_type=FeatureType.BUILDING,
            geometry_type=GeometryType.POLYGON,
            tags={"building": "residential", "ovmg:approximate": "yes"},
            outer_rings=((
                GeoPoint(center.longitude - 0.00005, center.latitude - 0.00005),
                GeoPoint(center.longitude + 0.00005, center.latitude - 0.00005),
                GeoPoint(center.longitude + 0.00005, center.latitude + 0.00005),
                GeoPoint(center.longitude - 0.00005, center.latitude + 0.00005),
                GeoPoint(center.longitude - 0.00005, center.latitude - 0.00005),
            ),),
        )
        filtered, report = BuildingSafetyValidator().filter_dataset(
            GeographicDataset(features=(proxy, approximate)),
            self.projector,
            self.settings,
        )
        self.assertEqual(filtered.features, ())
        self.assertEqual(report.rejected_optional, 2)

    def test_direct_source_height_is_preserved(self) -> None:
        from ..voxel.building_analysis import BuildingAnalyzer

        center = self.bounds.center
        ring = (
            GeoPoint(center.longitude - 0.00005, center.latitude - 0.00005),
            GeoPoint(center.longitude + 0.00005, center.latitude - 0.00005),
            GeoPoint(center.longitude + 0.00005, center.latitude + 0.00005),
            GeoPoint(center.longitude - 0.00005, center.latitude + 0.00005),
            GeoPoint(center.longitude - 0.00005, center.latitude - 0.00005),
        )
        feature = GeoFeature(
            source_id="overture/building/source-height",
            feature_type=FeatureType.BUILDING,
            geometry_type=GeometryType.POLYGON,
            tags={"building": "yes", "height": "52"},
            outer_rings=(ring,),
        )
        analyses, _records, _statistics = BuildingAnalyzer().analyze(
            (feature,), self.projector, self.settings
        )
        self.assertAlmostEqual(analyses[feature.source_id].height_m, 52.0)
        self.assertEqual(
            analyses[feature.source_id].height_source,
            HeightSource.REAL_HEIGHT,
        )

    def test_sixteen_source_floors_are_preserved(self) -> None:
        from ..voxel.building_analysis import BuildingAnalyzer

        center = self.bounds.center
        ring = (
            GeoPoint(center.longitude - 0.00005, center.latitude - 0.00005),
            GeoPoint(center.longitude + 0.00005, center.latitude - 0.00005),
            GeoPoint(center.longitude + 0.00005, center.latitude + 0.00005),
            GeoPoint(center.longitude - 0.00005, center.latitude + 0.00005),
            GeoPoint(center.longitude - 0.00005, center.latitude - 0.00005),
        )
        feature = GeoFeature(
            source_id="overture/building/sixteen-floors",
            feature_type=FeatureType.BUILDING,
            geometry_type=GeometryType.POLYGON,
            tags={"building": "tower", "building:levels": "16"},
            outer_rings=(ring,),
        )
        analyses, _records, _statistics = BuildingAnalyzer().analyze(
            (feature,), self.projector, self.settings
        )
        self.assertAlmostEqual(analyses[feature.source_id].height_m, 48.0)
        self.assertEqual(
            analyses[feature.source_id].height_source,
            HeightSource.REAL_LEVELS,
        )

    def test_advanced_generation_panel_is_not_public(self) -> None:
        panel_source = (
            Path(__file__).parents[1] / "presentation" / "panels.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("def _draw_advanced", panel_source)
        for property_name in (
            "generate_approximate_buildings",
            "generate_landmark_proxies",
            "use_building_parts",
            "infer_missing_heights",
            "default_building_height",
            "level_height",
            "minimum_building_area",
            "fallback_river_width",
        ):
            self.assertNotIn(f'prop(settings, "{property_name}")', panel_source)

    def test_runtime_settings_ignore_stale_risky_saved_values(self) -> None:
        operator_source = (
            Path(__file__).parents[1] / "presentation" / "operators.py"
        ).read_text(encoding="utf-8")
        self.assertIn("generate_landmark_proxies=False", operator_source)
        self.assertIn("generate_approximate_buildings=False", operator_source)
        panel_text = (
            Path(__file__).parents[1] / "presentation" / "panels.py"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "High is the default for maximum accuracy",
            panel_text,
        )
        self.assertNotIn("def _draw_advanced", panel_text)

class SimplifiedFinalWorkflowTests(unittest.TestCase):
    """Verify the six-step UI, accurate defaults, naming, and architectural style."""

    def test_architectural_model_style_is_available_and_direct(self) -> None:
        from ..domain.enums import ModelStyle
        from ..voxel.style_profiles import style_profile

        profile = style_profile(ModelStyle.ARCHITECTURAL_MODEL)
        self.assertTrue(profile.direct_buildings)
        self.assertTrue(profile.allow_building_parts)
        self.assertTrue(profile.allow_roof_shapes)
        self.assertTrue(profile.allow_curved_details)

    def test_public_defaults_are_high_accuracy_and_not_adhamiya_specific(self) -> None:
        properties_path = Path(__file__).parents[1] / "presentation" / "properties.py"
        source = properties_path.read_text(encoding="utf-8")
        self.assertIn('default="REAL"', source)
        self.assertIn('default="REALISTIC"', source)
        self.assertIn('default="HIGH"', source)
        self.assertIn('"ARCHITECTURAL_MODEL"', source)
        self.assertIn('default="Baghdad, Iraq"', source)
        self.assertNotIn('default="Al-Adhamiya, Baghdad, Iraq"', source)

    def test_main_panel_stays_in_six_step_workflow(self) -> None:
        operators_path = Path(__file__).parents[1] / "presentation" / "operators.py"
        panels_path = Path(__file__).parents[1] / "presentation" / "panels.py"
        operators = operators_path.read_text(encoding="utf-8")
        panels = panels_path.read_text(encoding="utf-8")
        self.assertNotIn("settings.wizard_step = 8", operators)
        self.assertIn("settings.wizard_step = 6", operators)
        review_start = panels.index("    def _draw_review_step(")
        review_end = panels.index(
            "    @classmethod\n    def _draw_pending_area(",
            review_start,
        )
        review_source = panels[review_start:review_end]
        self.assertIn('text="Map generated successfully"', review_source)
        self.assertNotIn('text="Advanced Options"', review_source)
        self.assertNotIn('text="Edit Buildings"', review_source)

    def test_normal_area_confirmation_is_not_drawn_as_an_error(self) -> None:
        panels_path = Path(__file__).parents[1] / "presentation" / "panels.py"
        source = panels_path.read_text(encoding="utf-8")
        pending_start = source.index("    def _draw_pending_area(")
        pending_end = source.index("    @staticmethod\n    def _draw_navigation", pending_start)
        pending_source = source[pending_start:pending_end]
        self.assertNotIn("pending.alert = True", pending_source)
        self.assertIn("New Area Ready for Confirmation", pending_source)

    def test_selector_reverse_geocodes_the_actual_rectangle_center(self) -> None:
        session_path = (
            Path(__file__).parents[1]
            / "infrastructure"
            / "map_selector"
            / "session.py"
        )
        web_path = (
            Path(__file__).parents[1]
            / "infrastructure"
            / "map_selector"
            / "web"
            / "index.html"
        )
        session_source = session_path.read_text(encoding="utf-8")
        web_source = web_path.read_text(encoding="utf-8")
        self.assertIn('parsed.path == "/reverse"', session_source)
        self.assertIn("def _reverse_place", session_source)
        self.assertIn("async function resolveSelectionName()", web_source)
        self.assertIn("display_name: resolvedName", web_source)

    def test_architectural_palette_is_export_compatible(self) -> None:
        materials_path = (
            Path(__file__).parents[1]
            / "infrastructure"
            / "blender"
            / "materials.py"
        )
        source = materials_path.read_text(encoding="utf-8")
        self.assertIn("_ARCHITECTURAL_COLORS", source)
        self.assertIn('variant.startswith("architectural|")', source)
        self.assertIn("_configure_architectural", source)
