"""GIS feature rasterization into a sparse semantic voxel world."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from hashlib import blake2b
from math import ceil, floor, hypot
import re

from ..core.exceptions import DataLimitError
from ..domain.enums import FeatureType, GeometryType, HeightSource, ModelStyle
from ..domain.models import (
    BoundingBox,
    BuildingAnalysis,
    GeoFeature,
    GeoPoint,
    RasterizationReport,
    VoxelSettings,
)
from ..domain.ports import ProgressCallback
from .projection import LocalMetricProjector
from .world import VoxelWorld
from .building_analysis import BuildingAnalyzer
from .style_profiles import style_profile

_NUMBER_PATTERN = re.compile(r"[-+]?\d+(?:\.\d+)?")


class FeatureRasterizer:
    """Convert classified GIS geometry into block occupancy."""

    _ROAD_WIDTHS_METERS: dict[str, float] = {
        "motorway": 14.0,
        "motorway_link": 8.0,
        "trunk": 12.0,
        "trunk_link": 7.0,
        "primary": 10.0,
        "primary_link": 7.0,
        "secondary": 8.0,
        "secondary_link": 6.0,
        "tertiary": 7.0,
        "tertiary_link": 5.0,
        "residential": 6.0,
        "living_street": 5.0,
        "service": 4.0,
        "unclassified": 5.0,
        "pedestrian": 4.0,
        "footway": 2.0,
        "path": 1.5,
        "cycleway": 2.0,
        "track": 3.0,
    }

    _INFILL_ROAD_TYPES = {
        "secondary",
        "secondary_link",
        "tertiary",
        "tertiary_link",
        "residential",
        "living_street",
        "service",
        "unclassified",
    }

    _CATEGORY_ORDER = (
        FeatureType.GREEN,
        FeatureType.WATER,
        FeatureType.ROAD,
        FeatureType.BRIDGE,
        FeatureType.TREE,
        FeatureType.BUILDING,
    )

    def rasterize(
        self,
        bounds: BoundingBox,
        features: Sequence[GeoFeature],
        settings: VoxelSettings,
        max_cells: int,
        progress: ProgressCallback,
        projector: LocalMetricProjector | None = None,
        building_analyses: Mapping[str, BuildingAnalysis] | None = None,
    ) -> tuple[VoxelWorld, LocalMetricProjector, RasterizationReport]:
        """Rasterize enabled categories and report building-height quality."""
        self._height_source_counts: dict[HeightSource, int] = defaultdict(int)
        self._skipped_small_buildings = 0
        self._skipped_parent_buildings = 0
        if projector is None:
            projector = LocalMetricProjector(
                bounds,
                settings.voxel_size,
                settings.vertical_step,
            )
        if building_analyses is None:
            building_analyses, building_records, _accuracy = BuildingAnalyzer().analyze(
                features, projector, settings
            )
        else:
            _analyses, building_records, _accuracy = BuildingAnalyzer().analyze(
                features, projector, settings
            )
        self._building_analyses = dict(building_analyses)
        self._building_records = tuple(building_records)
        estimated_surface = projector.width_cells * projector.height_cells
        if settings.include_terrain and estimated_surface > max_cells:
            raise DataLimitError(
                f"Terrain alone requires approximately {estimated_surface:,} voxels, "
                f"above the configured limit of {max_cells:,}. Increase Voxel Size "
                "or select a smaller area."
            )

        world = VoxelWorld(settings.chunk_size, max_cells)
        if settings.include_terrain:
            progress(0.03, "Creating terrain blocks")
            self._rasterize_terrain(world, projector)

        grouped: dict[FeatureType, list[GeoFeature]] = defaultdict(list)
        for feature in features:
            grouped[feature.feature_type].append(feature)
        urban_zones = [
            feature
            for feature in grouped[FeatureType.TERRAIN]
            if feature.geometry_type is GeometryType.POLYGON
            and feature.tags.get("landuse")
            in {"residential", "commercial", "retail", "mixed_use"}
        ]

        building_parts_by_parent = {
            feature.tags.get("ovmg:building_id", "")
            for feature in grouped[FeatureType.BUILDING]
            if feature.tags.get("ovmg:is_part") == "yes"
        }
        building_parts_by_parent.discard("")

        profile = style_profile(settings.model_style)
        enabled_order = [
            category
            for category in self._CATEGORY_ORDER
            if self._is_enabled(category, settings)
            and not (category is FeatureType.BUILDING and profile.direct_buildings)
        ]
        total_features = sum(len(grouped[category]) for category in enabled_order)
        completed = 0

        for category in enabled_order:
            for feature in grouped[category]:
                effective_parts = settings.use_building_parts and profile.allow_building_parts
                if (
                    category is FeatureType.BUILDING
                    and feature.tags.get("ovmg:is_part") == "yes"
                    and not effective_parts
                ):
                    completed += 1
                    continue
                if (
                    category is FeatureType.BUILDING
                    and effective_parts
                    and feature.tags.get("ovmg:has_parts") == "yes"
                    and feature.source_id.rsplit("/", 1)[-1] in building_parts_by_parent
                ):
                    self._skipped_parent_buildings += 1
                    completed += 1
                    continue
                self._rasterize_feature(world, projector, feature, settings)
                completed += 1
                if completed % 25 == 0 or completed == total_features:
                    ratio = completed / max(1, total_features)
                    progress(0.05 + ratio * 0.80, f"Voxelizing {category.value}")

        if (
            settings.include_buildings
            and settings.generate_approximate_buildings
            and settings.approximate_building_density > 0.0
        ):
            progress(0.86, "Generating approximate missing buildings")
            self._generate_approximate_buildings(
                world,
                projector,
                features,
                urban_zones,
                settings,
            )

        if settings.include_trees and settings.tree_density > 0.0:
            progress(0.92, "Distributing trees across green areas")
            self._generate_green_area_trees(world, projector, settings)

        if profile.direct_buildings:
            self._height_source_counts.clear()
            for record in self._building_records:
                self._height_source_counts[record.height_source] += 1
        report = RasterizationReport(
            height_source_counts=dict(self._height_source_counts),
            skipped_small_buildings=self._skipped_small_buildings,
            skipped_parent_buildings=self._skipped_parent_buildings,
            building_records=self._building_records,
        )
        return world, projector, report

    @staticmethod
    def _is_enabled(category: FeatureType, settings: VoxelSettings) -> bool:
        return {
            FeatureType.BUILDING: settings.include_buildings,
            FeatureType.ROAD: settings.include_roads,
            FeatureType.WATER: settings.include_water,
            FeatureType.GREEN: settings.include_green,
            FeatureType.BRIDGE: settings.include_bridges,
            FeatureType.TREE: settings.include_trees,
            FeatureType.TERRAIN: settings.include_terrain,
        }[category]

    @staticmethod
    def _rasterize_terrain(
        world: VoxelWorld,
        projector: LocalMetricProjector,
    ) -> None:
        for x in range(projector.width_cells):
            for y in range(projector.height_cells):
                world.add(FeatureType.TERRAIN, (x, y, 0))

    def _rasterize_feature(
        self,
        world: VoxelWorld,
        projector: LocalMetricProjector,
        feature: GeoFeature,
        settings: VoxelSettings,
    ) -> None:
        if feature.feature_type is FeatureType.BUILDING:
            self._rasterize_building(world, projector, feature, settings)
        elif feature.feature_type in {FeatureType.ROAD, FeatureType.BRIDGE}:
            self._rasterize_transport(world, projector, feature, settings)
        elif feature.feature_type in {FeatureType.WATER, FeatureType.GREEN}:
            self._rasterize_surface(world, projector, feature, settings)
        elif feature.feature_type is FeatureType.TREE:
            self._rasterize_explicit_tree(world, projector, feature, settings)

    def _rasterize_building(
        self,
        world: VoxelWorld,
        projector: LocalMetricProjector,
        feature: GeoFeature,
        settings: VoxelSettings,
    ) -> None:
        """Rasterize a footprint with independent XY and vertical resolution."""
        is_proxy = feature.geometry_type is GeometryType.POINT
        if is_proxy:
            if not settings.generate_landmark_proxies or not feature.points:
                return
            footprint = self._landmark_proxy_cells(projector, feature, settings)
        elif feature.geometry_type is GeometryType.POLYGON:
            area_m2 = self._feature_area_m2(projector, feature)
            if (
                area_m2 < settings.minimum_building_area
                and not self._is_landmark_without_building_footprint(feature.tags)
            ):
                self._skipped_small_buildings += 1
                return
            footprint = self._polygon_cells(projector, feature)
            if self._is_landmark_without_building_footprint(feature.tags):
                footprint = self._compact_landmark_footprint(
                    projector,
                    feature,
                    footprint,
                    settings,
                )
        else:
            return
        if not footprint:
            return

        analysis = self._building_analyses.get(feature.source_id)
        if analysis is None:
            height, height_source = self._building_height(feature, footprint, settings)
            minimum_height = self._tag_number(feature.tags, "min_height") or 0.0
            roof_shape = feature.tags.get("roof:shape", "").casefold()
            roof_height = self._roof_height(
                feature, footprint, roof_shape, height, settings
            )
        else:
            height = analysis.height_m
            height_source = analysis.height_source
            minimum_height = analysis.minimum_height_m
            roof_shape = analysis.roof_shape
            roof_height = analysis.roof_height_m
        self._height_source_counts[height_source] += 1
        footprint = self._stylize_building_footprint(footprint, settings)
        if not footprint:
            return
        start_layer = 1 + max(0, floor(minimum_height / settings.vertical_step))
        wall_height = max(settings.vertical_step, height - roof_height)
        wall_layers = max(1, ceil(wall_height / settings.vertical_step))
        for x, y in footprint:
            for z in range(start_layer, start_layer + wall_layers):
                world.add(FeatureType.BUILDING, (x, y, z))

        if roof_height <= 0.0:
            return
        roof_layers = max(1, ceil(roof_height / settings.vertical_step))
        roof_start = start_layer + wall_layers
        for layer_index in range(roof_layers):
            roof_cells = self._roof_layer_cells(
                footprint,
                roof_shape,
                layer_index,
                roof_layers,
            )
            for x, y in roof_cells:
                world.add(
                    FeatureType.BUILDING,
                    (x, y, roof_start + layer_index),
                )

    @staticmethod
    def _stylize_building_footprint(
        footprint: set[tuple[int, int]],
        settings: VoxelSettings,
    ) -> set[tuple[int, int]]:
        """Apply explicit block quantization for Minecraft-style buildings."""
        macro = style_profile(settings.model_style).minecraft_macro_cells
        if macro <= 1 or not footprint:
            return footprint
        blocks = {(x // macro, y // macro) for x, y in footprint}
        return {
            (block_x * macro + dx, block_y * macro + dy)
            for block_x, block_y in blocks
            for dx in range(macro)
            for dy in range(macro)
        }

    def _roof_height(
        self,
        feature: GeoFeature,
        footprint: set[tuple[int, int]],
        roof_shape: str,
        total_height: float,
        settings: VoxelSettings,
    ) -> float:
        """Resolve an explicit or conservative inferred roof height."""
        if not settings.use_roof_shapes or roof_shape in {"", "flat", "none"}:
            return 0.0
        explicit = self._tag_number(feature.tags, "roof:height")
        if explicit is not None and explicit > 0.0:
            return min(explicit, max(0.0, total_height - settings.vertical_step))
        roof_levels = self._tag_number(feature.tags, "roof:levels")
        if roof_levels is not None and roof_levels > 0.0:
            inferred = roof_levels * settings.level_height
        else:
            xs = [cell[0] for cell in footprint]
            ys = [cell[1] for cell in footprint]
            width_m = (max(xs) - min(xs) + 1) * settings.voxel_size
            depth_m = (max(ys) - min(ys) + 1) * settings.voxel_size
            minimum_span = min(width_m, depth_m)
            if roof_shape in {"dome", "onion", "cone"}:
                inferred = min(12.0, minimum_span * 0.42)
            else:
                inferred = min(8.0, minimum_span * 0.28)
        maximum = max(0.0, total_height - settings.vertical_step)
        return min(maximum, max(settings.vertical_step, inferred))

    @staticmethod
    def _roof_layer_cells(
        footprint: set[tuple[int, int]],
        roof_shape: str,
        layer_index: int,
        layer_count: int,
    ) -> set[tuple[int, int]]:
        """Return a stepped roof slice for common Overture/OSM roof shapes."""
        if not footprint:
            return set()
        xs = [cell[0] for cell in footprint]
        ys = [cell[1] for cell in footprint]
        min_x, max_x = min(xs), max(xs)
        min_y, max_y = min(ys), max(ys)
        width = max_x - min_x + 1
        depth = max_y - min_y + 1
        progress = (layer_index + 1) / max(1, layer_count)

        if roof_shape in {"gabled", "gambrel", "saltbox", "skillion"}:
            shrink_y = width >= depth
            maximum_inset = max(0, (depth if shrink_y else width) // 2)
            inset = min(maximum_inset, int(progress * (maximum_inset + 0.5)))
            if shrink_y:
                return {
                    (x, y) for x, y in footprint if min_y + inset <= y <= max_y - inset
                }
            return {(x, y) for x, y in footprint if min_x + inset <= x <= max_x - inset}

        if roof_shape in {"dome", "onion", "cone"}:
            center_x = (min_x + max_x) * 0.5
            center_y = (min_y + max_y) * 0.5
            radius_x = max(0.5, width * 0.5)
            radius_y = max(0.5, depth * 0.5)
            remaining_radius = max(0.12, (1.0 - progress * progress) ** 0.5)
            cells = {
                (x, y)
                for x, y in footprint
                if (
                    ((x + 0.5 - center_x) / radius_x) ** 2
                    + ((y + 0.5 - center_y) / radius_y) ** 2
                    <= remaining_radius * remaining_radius
                )
            }
            return cells or {
                min(
                    footprint, key=lambda c: abs(c[0] - center_x) + abs(c[1] - center_y)
                )
            }

        maximum_inset = max(0, min(width, depth) // 2)
        inset = min(maximum_inset, int(progress * (maximum_inset + 0.5)))
        cells = {
            (x, y)
            for x, y in footprint
            if (
                min_x + inset <= x <= max_x - inset
                and min_y + inset <= y <= max_y - inset
            )
        }
        return cells or {min(footprint)}

    @staticmethod
    def _feature_area_m2(
        projector: LocalMetricProjector,
        feature: GeoFeature,
    ) -> float:
        """Approximate polygon area in the local metric projection."""

        def ring_area(ring: Sequence[GeoPoint]) -> float:
            metric = [projector.project(point) for point in ring]
            if len(metric) < 3:
                return 0.0
            total = 0.0
            previous = metric[-1]
            for current in metric:
                total += previous.x * current.y - current.x * previous.y
                previous = current
            return abs(total) * 0.5

        outer = sum(ring_area(ring) for ring in feature.outer_rings)
        inner = sum(ring_area(ring) for ring in feature.inner_rings)
        return max(0.0, outer - inner)

    def _rasterize_transport(
        self,
        world: VoxelWorld,
        projector: LocalMetricProjector,
        feature: GeoFeature,
        settings: VoxelSettings,
    ) -> None:
        if len(feature.points) < 2:
            return
        width_meters = self._transport_width(feature.tags) * style_profile(settings.model_style).road_width_scale
        radius_cells = max(0.0, (width_meters * 0.5) / settings.voxel_size)
        category = feature.feature_type
        if category is FeatureType.BRIDGE:
            layer = int(self._tag_number(feature.tags, "layer") or 1)
            clearance = max(3.0, layer * settings.level_height)
            z_layer = 1 + max(1, ceil(clearance / settings.vertical_step))
        else:
            z_layer = 0

        points = [projector.grid_float(point) for point in feature.points]
        for start, end in zip(points, points[1:]):
            for center_x, center_y in self._sample_grid_segment(start, end):
                for offset_x, offset_y in self._disk_offsets(radius_cells):
                    x = center_x + offset_x
                    y = center_y + offset_y
                    if projector.contains_cell(x, y):
                        world.add(category, (x, y, z_layer))

    def _rasterize_surface(
        self,
        world: VoxelWorld,
        projector: LocalMetricProjector,
        feature: GeoFeature,
        settings: VoxelSettings,
    ) -> None:
        category = feature.feature_type
        if feature.geometry_type is GeometryType.POLYGON:
            for x, y in self._polygon_cells(projector, feature):
                world.add(category, (x, y, 0))
            return
        if feature.geometry_type is GeometryType.LINE and len(feature.points) >= 2:
            if category is FeatureType.WATER:
                width = self._water_width(feature.tags, settings)
            else:
                width = self._tag_number(feature.tags, "width") or 4.0
            radius = max(0.0, (width * 0.5) / projector.voxel_size)
            points = [projector.grid_float(point) for point in feature.points]
            for start, end in zip(points, points[1:]):
                for center_x, center_y in self._sample_grid_segment(start, end):
                    for offset_x, offset_y in self._disk_offsets(radius):
                        x = center_x + offset_x
                        y = center_y + offset_y
                        if projector.contains_cell(x, y):
                            world.add(category, (x, y, 0))

    def _rasterize_explicit_tree(
        self,
        world: VoxelWorld,
        projector: LocalMetricProjector,
        feature: GeoFeature,
        settings: VoxelSettings,
    ) -> None:
        if not feature.points:
            return
        x, y = projector.grid_cell(feature.points[0])
        if projector.contains_cell(x, y):
            self._add_tree(world, projector, x, y, settings)

    def _generate_approximate_buildings(
        self,
        world: VoxelWorld,
        projector: LocalMetricProjector,
        features: Sequence[GeoFeature],
        urban_zones: Sequence[GeoFeature],
        settings: VoxelSettings,
    ) -> None:
        """Recover plausible residential massing where OSM footprints are sparse.

        The recovery is deterministic and conservative. It uses mapped local
        streets as the primary spatial signal, prefers mapped residential or
        commercial land-use polygons when available, and never overwrites roads,
        water, green areas, bridges, or explicit building footprints.
        """
        road_surface = {(x, y) for x, y, z in world.cells(FeatureType.ROAD) if z == 0}
        if not road_surface:
            return

        urban_zone_cells: set[tuple[int, int]] = set()
        for zone in urban_zones:
            urban_zone_cells.update(self._polygon_cells(projector, zone))

        local_road_centers: set[tuple[int, int]] = set()
        for feature in features:
            if feature.feature_type is not FeatureType.ROAD:
                continue
            if feature.geometry_type is not GeometryType.LINE:
                continue
            if feature.tags.get("highway") not in self._INFILL_ROAD_TYPES:
                continue
            points = [projector.grid_float(point) for point in feature.points]
            for start, end in zip(points, points[1:]):
                local_road_centers.update(self._sample_grid_segment(start, end))
        if not local_road_centers:
            local_road_centers = set(road_surface)

        minimum_distance = max(1, ceil(5.0 / settings.voxel_size))
        maximum_distance = max(
            minimum_distance + 1,
            ceil(22.0 / settings.voxel_size),
        )
        band_offsets = [
            (offset_x, offset_y)
            for offset_x in range(-maximum_distance, maximum_distance + 1)
            for offset_y in range(-maximum_distance, maximum_distance + 1)
            if minimum_distance <= max(abs(offset_x), abs(offset_y)) <= maximum_distance
        ]

        candidates: set[tuple[int, int]] = set()
        for center_x, center_y in local_road_centers:
            for offset_x, offset_y in band_offsets:
                x = center_x + offset_x
                y = center_y + offset_y
                if not projector.contains_cell(x, y):
                    continue
                candidates.add((x, y))

        blocked_surface = set(road_surface)
        for category in (
            FeatureType.WATER,
            FeatureType.GREEN,
            FeatureType.BRIDGE,
        ):
            blocked_surface.update((x, y) for x, y, _z in world.cells(category))

        explicit_buildings = {
            (x, y) for x, y, z in world.cells(FeatureType.BUILDING) if z >= 1
        }
        reserved: set[tuple[int, int]] = set()
        for x, y in explicit_buildings:
            for offset_x in (-1, 0, 1):
                for offset_y in (-1, 0, 1):
                    reserved.add((x + offset_x, y + offset_y))

        minimum_dimension = max(1, round(7.0 / settings.voxel_size))
        maximum_dimension = max(
            minimum_dimension,
            round(13.0 / settings.voxel_size),
        )
        dimension_span = maximum_dimension - minimum_dimension + 1
        stride = max(2, ceil(10.0 / settings.voxel_size))
        density_limit = int(settings.approximate_building_density * 10_000)
        surface_area = projector.width_cells * projector.height_cells
        maximum_buildings = min(
            12_000,
            max(
                100,
                int(surface_area * 0.012 * settings.approximate_building_density),
            ),
        )

        generated = 0
        for anchor_x, anchor_y in sorted(candidates):
            seed = self._stable_cell_hash(anchor_x, anchor_y)
            if seed % stride:
                continue
            inside_urban_zone = (anchor_x, anchor_y) in urban_zone_cells
            effective_density = (
                density_limit
                if inside_urban_zone or not urban_zone_cells
                else int(density_limit * 0.55)
            )
            if (seed >> 8) % 10_000 >= effective_density:
                continue

            width = minimum_dimension + (seed >> 16) % dimension_span
            depth = minimum_dimension + (seed >> 20) % dimension_span
            if (seed >> 24) & 1:
                width, depth = depth, width

            start_x = anchor_x - width // 2
            start_y = anchor_y - depth // 2
            footprint = {
                (x, y)
                for x in range(start_x, start_x + width)
                for y in range(start_y, start_y + depth)
            }
            if not footprint:
                continue
            if any(not projector.contains_cell(x, y) for x, y in footprint):
                continue
            if footprint & blocked_surface:
                continue
            if footprint & reserved:
                continue
            if not any(cell in candidates for cell in footprint):
                continue

            level_seed = (seed >> 4) & 0xFF
            if level_seed > 242:
                levels = 5
            elif level_seed > 212:
                levels = 4
            else:
                levels = 1 + level_seed % 3
            height = levels * settings.level_height
            layer_count = max(1, ceil(height / settings.vertical_step))
            for x, y in footprint:
                for z in range(1, 1 + layer_count):
                    world.add(FeatureType.BUILDING, (x, y, z))

            for x, y in footprint:
                for offset_x in (-1, 0, 1):
                    for offset_y in (-1, 0, 1):
                        reserved.add((x + offset_x, y + offset_y))
            generated += 1
            if generated >= maximum_buildings:
                break

    @staticmethod
    def _stable_cell_hash(x: int, y: int) -> int:
        """Return a stable unsigned hash for deterministic procedural recovery."""
        return (
            (x * 73_856_093) ^ (y * 19_349_663) ^ ((x + y) * 83_492_791)
        ) & 0xFFFFFFFF

    def _generate_green_area_trees(
        self,
        world: VoxelWorld,
        projector: LocalMetricProjector,
        settings: VoxelSettings,
    ) -> None:
        green_xy = {(x, y) for x, y, z in world.cells(FeatureType.GREEN) if z == 0}
        spacing_cells = max(1, ceil(6.0 / settings.voxel_size))
        threshold = int(settings.tree_density * 10_000)
        for x, y in sorted(green_xy):
            if x % spacing_cells or y % spacing_cells:
                continue
            deterministic = (x * 73856093 ^ y * 19349663) & 0xFFFF
            if deterministic % 10_000 >= threshold:
                continue
            if projector.contains_cell(x, y):
                self._add_tree(world, projector, x, y, settings)

    @staticmethod
    def _add_tree(
        world: VoxelWorld,
        projector: LocalMetricProjector,
        x: int,
        y: int,
        settings: VoxelSettings,
    ) -> None:
        trunk_layers = max(1, ceil(4.0 / settings.vertical_step))
        canopy_radius = (
            0 if settings.voxel_size >= 5.0 else max(1, ceil(1.5 / settings.voxel_size))
        )
        base_z = 1
        for z in range(base_z, base_z + trunk_layers):
            if projector.contains_cell(x, y):
                world.add(FeatureType.TREE, (x, y, z))
        canopy_z = base_z + trunk_layers
        if canopy_radius == 0:
            if projector.contains_cell(x, y):
                world.add(FeatureType.TREE, (x, y, canopy_z))
            return
        for offset_x in range(-canopy_radius, canopy_radius + 1):
            for offset_y in range(-canopy_radius, canopy_radius + 1):
                if abs(offset_x) + abs(offset_y) <= canopy_radius + 1:
                    canopy_x = x + offset_x
                    canopy_y = y + offset_y
                    if projector.contains_cell(canopy_x, canopy_y):
                        world.add(
                            FeatureType.TREE,
                            (canopy_x, canopy_y, canopy_z),
                        )
        if projector.contains_cell(x, y):
            world.add(FeatureType.TREE, (x, y, canopy_z + 1))

    def _polygon_cells(
        self,
        projector: LocalMetricProjector,
        feature: GeoFeature,
    ) -> set[tuple[int, int]]:
        outer_rings = [
            tuple(projector.grid_float(point) for point in ring)
            for ring in feature.outer_rings
            if len(ring) >= 3
        ]
        inner_rings = [
            tuple(projector.grid_float(point) for point in ring)
            for ring in feature.inner_rings
            if len(ring) >= 3
        ]
        if not outer_rings:
            return set()

        cells: set[tuple[int, int]] = set()
        for outer in outer_rings:
            min_x = max(0, floor(min(point[0] for point in outer)))
            max_x = min(
                projector.width_cells - 1,
                ceil(max(point[0] for point in outer)),
            )
            min_y = max(0, floor(min(point[1] for point in outer)))
            max_y = min(
                projector.height_cells - 1,
                ceil(max(point[1] for point in outer)),
            )
            for x in range(min_x, max_x + 1):
                for y in range(min_y, max_y + 1):
                    sample = (x + 0.5, y + 0.5)
                    if not self._point_in_ring(sample, outer):
                        continue
                    if any(self._point_in_ring(sample, inner) for inner in inner_rings):
                        continue
                    cells.add((x, y))
        if not cells:
            first_outer = outer_rings[0]
            centroid_points = first_outer[:-1] or first_outer
            centroid_x = sum(point[0] for point in centroid_points) / max(
                1,
                len(centroid_points),
            )
            centroid_y = sum(point[1] for point in centroid_points) / max(
                1,
                len(centroid_points),
            )
            fallback = (floor(centroid_x), floor(centroid_y))
            if projector.contains_cell(*fallback):
                cells.add(fallback)
        return cells

    @staticmethod
    def _point_in_ring(
        point: tuple[float, float],
        ring: Sequence[tuple[float, float]],
    ) -> bool:
        x, y = point
        inside = False
        previous_x, previous_y = ring[-1]
        for current_x, current_y in ring:
            intersects = (current_y > y) != (previous_y > y)
            if intersects:
                denominator = previous_y - current_y
                if abs(denominator) < 1e-12:
                    denominator = 1e-12
                x_intersection = (previous_x - current_x) * (
                    y - current_y
                ) / denominator + current_x
                if x < x_intersection:
                    inside = not inside
            previous_x, previous_y = current_x, current_y
        return inside

    @staticmethod
    def _sample_grid_segment(
        start: tuple[float, float],
        end: tuple[float, float],
    ) -> Iterable[tuple[int, int]]:
        delta_x = end[0] - start[0]
        delta_y = end[1] - start[1]
        steps = max(1, ceil(max(abs(delta_x), abs(delta_y)) * 2.0))
        visited: set[tuple[int, int]] = set()
        for step in range(steps + 1):
            factor = step / steps
            cell = (
                floor(start[0] + delta_x * factor),
                floor(start[1] + delta_y * factor),
            )
            if cell not in visited:
                visited.add(cell)
                yield cell

    @staticmethod
    def _disk_offsets(radius: float) -> Iterable[tuple[int, int]]:
        if radius <= 0:
            yield 0, 0
            return
        extent = ceil(radius)
        for x in range(-extent, extent + 1):
            for y in range(-extent, extent + 1):
                if hypot(x, y) <= radius + 0.15:
                    yield x, y

    def _building_height(
        self,
        feature: GeoFeature,
        footprint: set[tuple[int, int]],
        settings: VoxelSettings,
    ) -> tuple[float, HeightSource]:
        """Return height in meters and the quality/source classification."""
        tags = feature.tags
        is_part = tags.get("ovmg:is_part") == "yes"
        explicit_height = self._tag_number(tags, "height")
        if explicit_height is not None and explicit_height > 0.0:
            source = HeightSource.BUILDING_PART if is_part else HeightSource.REAL_HEIGHT
            return explicit_height, source
        levels = self._tag_number(tags, "building:levels")
        if levels is None:
            levels = self._tag_number(tags, "levels")
        if levels is not None and levels > 0.0:
            roof_levels = self._tag_number(tags, "roof:levels") or 0.0
            height = (levels + max(0.0, roof_levels * 0.5)) * settings.level_height
            source = HeightSource.BUILDING_PART if is_part else HeightSource.REAL_LEVELS
            return height, source
        if settings.infer_missing_heights:
            source = HeightSource.BUILDING_PART if is_part else HeightSource.INFERRED
            return self._infer_building_height(feature, footprint, settings), source
        source = HeightSource.BUILDING_PART if is_part else HeightSource.DEFAULT
        return settings.default_building_height, source

    def _infer_building_height(
        self,
        feature: GeoFeature,
        footprint: set[tuple[int, int]],
        settings: VoxelSettings,
    ) -> float:
        """Infer a deterministic, semantically plausible missing height."""
        tags = feature.tags
        seed = int.from_bytes(
            blake2b(feature.source_id.encode("utf-8"), digest_size=4).digest(),
            "big",
        )
        unit = seed / 0xFFFFFFFF
        area_m2 = len(footprint) * settings.voxel_size * settings.voxel_size

        man_made = tags.get("man_made", "")
        historic = tags.get("historic", "")
        amenity = tags.get("amenity", "")
        building = tags.get("building", tags.get("building:part", ""))

        if man_made == "minaret":
            return 28.0 + unit * 18.0
        if man_made in {"tower", "water_tower"} or historic == "tower":
            return 22.0 + unit * 28.0
        if historic in {"monument", "memorial"}:
            return 10.0 + unit * 16.0
        if historic in {"castle", "fort", "ruins"}:
            return 12.0 + unit * 12.0
        if amenity == "place_of_worship" or building in {
            "mosque",
            "church",
            "cathedral",
            "synagogue",
            "temple",
        }:
            return 12.0 + unit * 10.0
        if amenity in {"hospital", "university", "college"}:
            return 12.0 + unit * 15.0
        if amenity in {
            "school",
            "clinic",
            "police",
            "fire_station",
            "townhall",
            "courthouse",
            "library",
            "community_centre",
            "marketplace",
        }:
            return 8.0 + unit * 10.0
        if tags.get("tourism") in {"museum", "hotel", "attraction"}:
            return 10.0 + unit * 16.0
        if tags.get("office") == "government":
            return 10.0 + unit * 14.0

        level_ranges: dict[str, tuple[int, int]] = {
            "apartments": (3, 7),
            "office": (3, 8),
            "commercial": (2, 6),
            "retail": (2, 5),
            "hotel": (3, 8),
            "hospital": (2, 6),
            "school": (2, 4),
            "university": (2, 5),
            "house": (1, 3),
            "detached": (1, 3),
            "semidetached_house": (1, 3),
            "residential": (2, 4),
            "industrial": (1, 3),
            "warehouse": (1, 2),
            "garage": (1, 1),
            "garages": (1, 1),
            "shed": (1, 1),
        }
        default_levels = max(
            1,
            round(settings.default_building_height / settings.level_height),
        )
        minimum, maximum = level_ranges.get(
            building,
            (max(1, default_levels - 1), default_levels + 1),
        )
        if area_m2 >= 2500.0:
            maximum += 2
        elif area_m2 >= 1000.0:
            maximum += 1
        levels_inferred = minimum + int(unit * (maximum - minimum + 1))
        levels_inferred = min(maximum, max(minimum, levels_inferred))
        return max(settings.level_height, levels_inferred * settings.level_height)

    def _landmark_proxy_cells(
        self,
        projector: LocalMetricProjector,
        feature: GeoFeature,
        settings: VoxelSettings,
    ) -> set[tuple[int, int]]:
        """Create a compact footprint around a mapped landmark point."""
        x, y = projector.grid_cell(feature.points[0])
        if not projector.contains_cell(x, y):
            return set()
        radius_m = self._landmark_radius_meters(feature.tags)
        radius_cells = max(0.5, radius_m / settings.voxel_size)
        return {
            (x + offset_x, y + offset_y)
            for offset_x, offset_y in self._disk_offsets(radius_cells)
            if projector.contains_cell(x + offset_x, y + offset_y)
        }

    def _compact_landmark_footprint(
        self,
        projector: LocalMetricProjector,
        feature: GeoFeature,
        polygon_cells: set[tuple[int, int]],
        settings: VoxelSettings,
    ) -> set[tuple[int, int]]:
        """Avoid turning a large worship/heritage site boundary into one building."""
        maximum_area = 2500.0
        area_m2 = len(polygon_cells) * settings.voxel_size * settings.voxel_size
        if area_m2 <= maximum_area or not feature.outer_rings:
            return polygon_cells
        ring = feature.outer_rings[0]
        points = ring[:-1] or ring
        center = GeoPoint(
            longitude=sum(point.longitude for point in points) / len(points),
            latitude=sum(point.latitude for point in points) / len(points),
        )
        proxy = GeoFeature(
            source_id=feature.source_id,
            feature_type=feature.feature_type,
            geometry_type=GeometryType.POINT,
            tags=feature.tags,
            points=(center,),
        )
        return self._landmark_proxy_cells(projector, proxy, settings)

    @staticmethod
    def _is_landmark_without_building_footprint(tags: Mapping[str, str]) -> bool:
        institutional = {
            "school",
            "college",
            "university",
            "hospital",
            "clinic",
            "police",
            "fire_station",
            "townhall",
            "courthouse",
            "library",
            "community_centre",
            "marketplace",
        }
        return (
            "building" not in tags
            and "building:part" not in tags
            and (
                tags.get("amenity") == "place_of_worship"
                or tags.get("amenity") in institutional
                or tags.get("tourism") in {"museum", "hotel", "attraction"}
                or tags.get("office") == "government"
                or tags.get("man_made") in {"tower", "minaret", "water_tower"}
                or tags.get("historic")
                in {"monument", "memorial", "castle", "fort", "tower", "ruins"}
            )
        )

    @staticmethod
    def _landmark_radius_meters(tags: Mapping[str, str]) -> float:
        if tags.get("man_made") == "minaret":
            return 4.5
        if tags.get("man_made") in {"tower", "water_tower"}:
            return 6.0
        if tags.get("amenity") == "place_of_worship":
            return 12.0
        if tags.get("amenity") in {"hospital", "university", "college"}:
            return 18.0
        if tags.get("amenity") in {
            "school",
            "clinic",
            "police",
            "fire_station",
            "townhall",
            "courthouse",
            "library",
            "community_centre",
            "marketplace",
        }:
            return 13.0
        if tags.get("tourism") in {"museum", "hotel", "attraction"}:
            return 14.0
        if tags.get("office") == "government":
            return 14.0
        if tags.get("historic") in {"castle", "fort", "ruins"}:
            return 14.0
        return 7.0

    def _water_width(
        self,
        tags: Mapping[str, str],
        settings: VoxelSettings,
    ) -> float:
        """Resolve line-water width and enforce a major-river minimum.

        Major rivers in OSM sometimes carry a narrow or stale ``width`` value
        intended for a local segment. Such a value must not collapse a broad
        river corridor to a one-voxel line, so the configured width acts as a
        minimum rather than only as a missing-data fallback.
        """
        explicit_width = self._tag_number(tags, "width")
        if explicit_width is None:
            explicit_width = self._tag_number(tags, "est_width")

        waterway = tags.get("waterway", "")
        water = tags.get("water", "")
        names = " ".join(
            tags.get(key, "") for key in ("name", "name:en", "name:ar", "alt_name")
        ).casefold()
        is_named_major_river = any(
            token in names for token in ("tigris", "dijla", "دجلة")
        )
        if waterway == "river" or water == "river" or is_named_major_river:
            return max(
                settings.fallback_river_width,
                explicit_width or 0.0,
            )

        if explicit_width is not None and explicit_width > 0.0:
            return explicit_width
        return {
            "canal": 20.0,
            "stream": 6.0,
            "drain": 3.0,
            "ditch": 2.0,
        }.get(waterway, 8.0)

    def _transport_width(self, tags: Mapping[str, str]) -> float:
        explicit_width = self._tag_number(tags, "width")
        if explicit_width is not None and explicit_width > 0.0:
            return explicit_width
        lanes = self._tag_number(tags, "lanes")
        if lanes is not None and lanes > 0.0:
            return max(2.0, lanes * 3.2)
        return self._ROAD_WIDTHS_METERS.get(tags.get("highway", ""), 4.0)

    @staticmethod
    def _tag_number(tags: Mapping[str, str], key: str) -> float | None:
        raw = tags.get(key)
        if raw is None:
            return None
        match = _NUMBER_PATTERN.search(raw.replace(",", "."))
        if match is None:
            return None
        try:
            return float(match.group(0))
        except ValueError:
            return None
