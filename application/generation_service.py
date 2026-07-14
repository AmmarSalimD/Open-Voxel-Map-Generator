"""Application use cases for generating and deleting voxel maps."""

from __future__ import annotations

from collections import Counter
from dataclasses import replace
from math import sqrt

from ..core.exceptions import DataLimitError
from ..domain.enums import (
    FeatureType,
    GenerationStage,
    InputMode,
    LabelKind,
    LargeAreaMode,
    ModelStyle,
)
from ..domain.models import (
    BoundingBox,
    BuildingAccuracyRecord,
    BuildingStatistics,
    CurvedDetailPayload,
    GenerationResult,
    GeographicDataset,
    LabelPayload,
    MapBuildRequest,
    MeshPayload,
    ProjectStatistics,
)
from ..domain.ports import (
    GeographicDataSource,
    Geocoder,
    ProgressCallback,
    SceneRepository,
)
from ..voxel.accuracy_overlay import AccuracyOverlayBuilder
from ..voxel.building_analysis import BuildingAnalyzer
from ..voxel.building_mesh_builder import DirectBuildingMeshBuilder
from ..voxel.building_validation import BuildingSafetyValidator
from ..voxel.curved_details import CurvedDetailBuilder
from ..voxel.labels import MapLabelBuilder
from ..voxel.mesh_builder import GreedyChunkMesher
from ..voxel.projection import LocalMetricProjector
from ..voxel.rasterizer import FeatureRasterizer


class MapGenerationService:
    """Coordinate GIS retrieval, voxelization, optional detail, and scene IO."""

    _TILE_SURFACE_RATIO = 0.35
    _MAX_TILE_COUNT = 64

    def __init__(
        self,
        geocoder: Geocoder,
        data_source: GeographicDataSource,
        rasterizer: FeatureRasterizer,
        mesher: GreedyChunkMesher,
        curved_detail_builder: CurvedDetailBuilder,
        label_builder: MapLabelBuilder,
        scene_repository: SceneRepository,
        building_analyzer: BuildingAnalyzer | None = None,
        direct_building_builder: DirectBuildingMeshBuilder | None = None,
        accuracy_overlay_builder: AccuracyOverlayBuilder | None = None,
        building_validator: BuildingSafetyValidator | None = None,
    ) -> None:
        self._geocoder = geocoder
        self._data_source = data_source
        self._rasterizer = rasterizer
        self._mesher = mesher
        self._curved_detail_builder = curved_detail_builder
        self._label_builder = label_builder
        self._scene_repository = scene_repository
        self._building_analyzer = building_analyzer or BuildingAnalyzer()
        self._direct_building_builder = direct_building_builder or DirectBuildingMeshBuilder()
        self._accuracy_overlay_builder = accuracy_overlay_builder or AccuracyOverlayBuilder()
        self._building_validator = building_validator or BuildingSafetyValidator()

    def generate(
        self,
        request: MapBuildRequest,
        progress: ProgressCallback,
    ) -> GenerationResult:
        """Execute one complete generation workflow synchronously."""
        progress(0.0, GenerationStage.VALIDATING.value)
        request.validate()

        if request.input_mode is InputMode.AREA_NAME:
            progress(0.03, GenerationStage.GEOCODING.value)
            bounds = self._geocoder.resolve(request.area_name)
        else:
            bounds = request.bounding_box
        bounds.validate()

        projector = LocalMetricProjector(
            bounds,
            request.voxel.voxel_size,
            request.voxel.vertical_step,
        )
        estimated_surface = projector.width_cells * projector.height_cells
        generation_multiplier = self._generation_multiplier(request)
        should_split = (
            request.large_area_mode is LargeAreaMode.SPLIT_TILES
            and estimated_surface * generation_multiplier
            > int(request.max_voxel_cells * 0.85)
        )
        if should_split:
            return self._generate_tiled(request, bounds, projector, progress)

        if request.voxel.include_terrain and estimated_surface > request.max_voxel_cells:
            raise DataLimitError(
                f"The selected area needs approximately {estimated_surface:,} "
                "terrain voxels before buildings are added. Reduce the selected "
                "rectangle, lower Quality, or enable Split Into Map Tiles under "
                "Advanced Options."
            )
        return self._generate_single(request, bounds, progress)

    def _generate_single(
        self,
        request: MapBuildRequest,
        bounds: BoundingBox,
        progress: ProgressCallback,
    ) -> GenerationResult:
        progress(0.08, GenerationStage.DOWNLOADING.value)
        projector = LocalMetricProjector(
            bounds, request.voxel.voxel_size, request.voxel.vertical_step
        )
        dataset = self._filter_suppressed_buildings(
            self._data_source.fetch_features(bounds),
            request.voxel.excluded_building_source_ids,
        )
        dataset, _validation_report = self._building_validator.filter_dataset(
            dataset, projector, request.voxel
        )
        building_analyses, building_records, accuracy_statistics = (
            self._building_analyzer.analyze(
                dataset.features, projector, request.voxel
            )
        )

        progress(0.23, GenerationStage.VOXELIZING.value)
        world, projector, raster_report = self._rasterizer.rasterize(
            bounds,
            dataset.features,
            request.voxel,
            request.max_voxel_cells,
            lambda ratio, message: progress(0.23 + ratio * 0.30, message),
            projector=projector,
            building_analyses=building_analyses,
        )

        progress(0.53, GenerationStage.MESHING.value)
        meshes = self._styled_voxel_meshes(
            self._mesher.build(
                world,
                projector,
                lambda ratio, message: progress(0.53 + ratio * 0.16, message),
                project_name=request.project_name,
            ),
            request.voxel.model_style,
        )
        direct_buildings = self._direct_building_builder.build(
            dataset.features,
            building_analyses,
            projector,
            request.voxel,
            request.project_name,
        )
        accuracy_overlays = self._accuracy_overlay_builder.build(
            dataset.features,
            building_analyses,
            projector,
            request.voxel,
            request.project_name,
        )
        meshes.extend(direct_buildings)
        meshes.extend(accuracy_overlays)

        progress(0.72, GenerationStage.DETAILS.value)
        curved_details = self._curved_detail_builder.build(
            dataset.features,
            projector,
            request.voxel,
            request.project_name,
            building_analyses=building_analyses,
        )
        labels = self._label_builder.build(
            dataset.features,
            bounds,
            projector,
            request.voxel,
            self._display_area_name(request),
        )

        building_statistics = dataset.building_statistics.with_height_counts(
            raster_report.height_source_counts
        )
        statistics = ProjectStatistics(
            source_feature_count=len(dataset.features),
            voxel_count=world.voxel_count,
            chunk_count=len(meshes),
            object_count=len(meshes) + len(curved_details),
            category_counts=world.category_counts(),
            curved_detail_count=len(curved_details),
            label_count=len(labels),
            building_statistics=building_statistics,
            attributions=dataset.attributions,
            tile_count=1,
            warnings=dataset.warnings,
            building_accuracy=accuracy_statistics,
            building_records=building_records,
        )
        return self._write_project(
            request,
            bounds,
            meshes,
            curved_details,
            labels,
            statistics,
            progress,
            progress_start=0.82,
        )

    def _generate_tiled(
        self,
        request: MapBuildRequest,
        bounds: BoundingBox,
        global_projector: LocalMetricProjector,
        progress: ProgressCallback,
    ) -> GenerationResult:
        """Generate aligned sub-grids and assemble them in one Blender project."""
        tile_projectors = self._aligned_tile_projectors(
            global_projector,
            request.max_voxel_cells,
            request.voxel.chunk_size,
            self._generation_multiplier(request),
        )
        if len(tile_projectors) > self._MAX_TILE_COUNT:
            raise DataLimitError(
                f"The selected rectangle requires {len(tile_projectors)} map tiles, "
                f"above the supported limit of {self._MAX_TILE_COUNT}. Reduce the "
                "area or lower Quality."
            )

        meshes: list[MeshPayload] = []
        curved_by_source: dict[tuple[str, str], CurvedDetailPayload] = {}
        label_candidates: list[LabelPayload] = []
        unique_features: dict[str, object] = {}
        category_counts: Counter[FeatureType] = Counter()
        height_counts: Counter[object] = Counter()
        attributions: list[str] = []
        runtime_warnings: list[str] = []
        merged_duplicates = 0
        overture_release = ""
        voxel_count = 0
        building_records_by_source: dict[str, BuildingAccuracyRecord] = {}
        total_tiles = len(tile_projectors)
        tile_label_settings = replace(
            request.voxel,
            include_area_labels=False,
        )

        for index, (tile_x, tile_y, tile_projector) in enumerate(tile_projectors):
            tile_start = 0.08 + (index / total_tiles) * 0.70
            tile_span = 0.70 / total_tiles
            progress(
                tile_start,
                f"Downloading map tile {index + 1}/{total_tiles}",
            )
            dataset = self._filter_suppressed_buildings(
                self._data_source.fetch_features(tile_projector.bounds),
                request.voxel.excluded_building_source_ids,
            )
            dataset, _validation_report = self._building_validator.filter_dataset(
                dataset, tile_projector, request.voxel
            )
            for feature in dataset.features:
                unique_features.setdefault(feature.source_id, feature)
            for attribution in dataset.attributions:
                if attribution not in attributions:
                    attributions.append(attribution)
            for warning in dataset.warnings:
                if warning not in runtime_warnings:
                    runtime_warnings.append(warning)
            merged_duplicates += dataset.building_statistics.merged_duplicates
            if not overture_release and dataset.building_statistics.overture_release:
                overture_release = dataset.building_statistics.overture_release

            building_analyses, tile_records, _tile_accuracy = (
                self._building_analyzer.analyze(
                    dataset.features, tile_projector, request.voxel
                )
            )
            for record in tile_records:
                building_records_by_source.setdefault(record.source_id, record)

            world, _projector, report = self._rasterizer.rasterize(
                tile_projector.bounds,
                dataset.features,
                request.voxel,
                request.max_voxel_cells,
                lambda ratio, message, start=tile_start, span=tile_span: progress(
                    start + ratio * span * 0.46,
                    f"Tile {index + 1}/{total_tiles}: {message}",
                ),
                projector=tile_projector,
                building_analyses=building_analyses,
            )
            tile_meshes = self._styled_voxel_meshes(
                self._mesher.build(
                    world,
                    tile_projector,
                    lambda ratio, message, start=tile_start, span=tile_span: progress(
                        start + span * 0.46 + ratio * span * 0.30,
                        f"Tile {index + 1}/{total_tiles}: {message}",
                    ),
                    project_name=(
                        f"{request.project_name}_T{tile_x:02d}_{tile_y:02d}"
                    ),
                ),
                request.voxel.model_style,
            )
            tile_meshes.extend(
                self._direct_building_builder.build(
                    dataset.features,
                    building_analyses,
                    tile_projector,
                    request.voxel,
                    f"{request.project_name}_T{tile_x:02d}_{tile_y:02d}",
                )
            )
            tile_meshes.extend(
                self._accuracy_overlay_builder.build(
                    dataset.features,
                    building_analyses,
                    tile_projector,
                    request.voxel,
                    f"{request.project_name}_T{tile_x:02d}_{tile_y:02d}",
                )
            )
            meshes.extend(tile_meshes)
            voxel_count += world.voxel_count
            category_counts.update(world.category_counts())
            height_counts.update(report.height_source_counts)

            for detail in self._curved_detail_builder.build(
                dataset.features,
                tile_projector,
                request.voxel,
                f"{request.project_name}_T{tile_x:02d}_{tile_y:02d}",
                building_analyses=building_analyses,
            ):
                curved_by_source.setdefault(
                    (detail.source_id, detail.kind.value),
                    detail,
                )
            label_candidates.extend(
                self._label_builder.build(
                    dataset.features,
                    tile_projector.bounds,
                    tile_projector,
                    tile_label_settings,
                    self._display_area_name(request),
                )
            )
            progress(
                tile_start + tile_span,
                f"Completed map tile {index + 1}/{total_tiles}",
            )

        if request.voxel.generate_labels and request.voxel.include_area_labels:
            area_only_settings = replace(
                request.voxel,
                include_street_labels=False,
                include_landmark_labels=False,
                include_area_labels=True,
            )
            label_candidates.extend(
                self._label_builder.build(
                    (),
                    bounds,
                    global_projector,
                    area_only_settings,
                    self._display_area_name(request),
                )
            )

        labels = self._deduplicate_labels(
            label_candidates,
            request.voxel.maximum_label_count,
        )
        curved_details = tuple(curved_by_source.values())
        features = tuple(unique_features.values())
        unique_buildings = [
            feature
            for feature in features
            if getattr(feature, "feature_type", None) is FeatureType.BUILDING
        ]
        overture_parts = sum(
            1
            for feature in unique_buildings
            if str(getattr(feature, "source_id", "")).startswith(
                "overture/building_part/"
            )
        )
        overture_buildings = sum(
            1
            for feature in unique_buildings
            if str(getattr(feature, "source_id", "")).startswith(
                "overture/building/"
            )
        )
        osm_buildings = max(
            0,
            len(unique_buildings) - overture_buildings - overture_parts,
        )
        building_statistics = BuildingStatistics(
            osm_buildings=osm_buildings,
            overture_buildings=overture_buildings,
            overture_parts=overture_parts,
            merged_duplicates=merged_duplicates,
            final_building_features=len(unique_buildings),
            overture_release=overture_release,
        ).with_height_counts(height_counts)

        building_records = tuple(building_records_by_source.values())
        accuracy_statistics = self._building_analyzer.summarize(building_records)
        statistics = ProjectStatistics(
            source_feature_count=len(features),
            voxel_count=voxel_count,
            chunk_count=len(meshes),
            object_count=len(meshes) + len(curved_details),
            category_counts=dict(category_counts),
            curved_detail_count=len(curved_details),
            label_count=len(labels),
            building_statistics=building_statistics,
            attributions=tuple(attributions) or ("© OpenStreetMap contributors",),
            tile_count=total_tiles,
            warnings=tuple(runtime_warnings),
            building_accuracy=accuracy_statistics,
            building_records=building_records,
        )
        return self._write_project(
            request,
            bounds,
            meshes,
            curved_details,
            labels,
            statistics,
            progress,
            progress_start=0.80,
        )

    @staticmethod
    def _filter_suppressed_buildings(
        dataset: GeographicDataset,
        source_ids: tuple[str, ...],
    ) -> GeographicDataset:
        """Remove user-suppressed buildings before analysis and mesh generation."""
        suppressed = {str(value) for value in source_ids if str(value)}
        if not suppressed:
            return dataset
        removed = [
            feature
            for feature in dataset.features
            if feature.feature_type is FeatureType.BUILDING
            and feature.source_id in suppressed
        ]
        if not removed:
            return dataset
        features = tuple(
            feature
            for feature in dataset.features
            if not (
                feature.feature_type is FeatureType.BUILDING
                and feature.source_id in suppressed
            )
        )
        statistics = dataset.building_statistics
        osm_removed = sum(
            1 for feature in removed if not feature.source_id.startswith("overture/")
        )
        overture_parts_removed = sum(
            1
            for feature in removed
            if feature.source_id.startswith("overture/building_part/")
        )
        overture_buildings_removed = sum(
            1
            for feature in removed
            if feature.source_id.startswith("overture/building/")
        )
        filtered_statistics = replace(
            statistics,
            osm_buildings=max(0, statistics.osm_buildings - osm_removed),
            overture_buildings=max(
                0, statistics.overture_buildings - overture_buildings_removed
            ),
            overture_parts=max(
                0, statistics.overture_parts - overture_parts_removed
            ),
            final_building_features=max(
                0, statistics.final_building_features - len(removed)
            ),
        )
        warnings = (
            *dataset.warnings,
            f"Applied {len(removed)} building correction exclusion(s).",
        )
        return replace(
            dataset,
            features=features,
            building_statistics=filtered_statistics,
            warnings=warnings,
        )

    def _write_project(
        self,
        request: MapBuildRequest,
        bounds: BoundingBox,
        meshes: list[MeshPayload] | tuple[MeshPayload, ...],
        curved_details: tuple[CurvedDetailPayload, ...] | list[CurvedDetailPayload],
        labels: tuple[LabelPayload, ...] | list[LabelPayload],
        statistics: ProjectStatistics,
        progress: ProgressCallback,
        progress_start: float,
    ) -> GenerationResult:
        progress(progress_start, GenerationStage.FINALIZING.value)
        created_objects = self._scene_repository.replace_project(
            request.project_name,
            bounds,
            request.voxel,
            meshes,
            curved_details,
            labels,
            statistics,
            lambda ratio, message: progress(
                progress_start + ratio * (0.99 - progress_start),
                message,
            ),
        )
        final_statistics = ProjectStatistics(
            source_feature_count=statistics.source_feature_count,
            voxel_count=statistics.voxel_count,
            chunk_count=statistics.chunk_count,
            object_count=created_objects,
            category_counts=statistics.category_counts,
            curved_detail_count=statistics.curved_detail_count,
            label_count=statistics.label_count,
            building_statistics=statistics.building_statistics,
            attributions=statistics.attributions,
            tile_count=statistics.tile_count,
            warnings=statistics.warnings,
            building_accuracy=statistics.building_accuracy,
            building_records=statistics.building_records,
        )
        progress(1.0, "Generation complete")
        return GenerationResult(bounds=bounds, statistics=final_statistics)

    @classmethod
    def _aligned_tile_projectors(
        cls,
        projector: LocalMetricProjector,
        max_voxel_cells: int,
        chunk_size: int,
        generation_multiplier: float = 1.0,
    ) -> list[tuple[int, int, LocalMetricProjector]]:
        target_surface = max(
            chunk_size * chunk_size,
            int(
                max_voxel_cells
                * 0.75
                / max(1.0, float(generation_multiplier))
            ),
        )
        side = max(chunk_size, int(sqrt(target_surface)))
        side = max(chunk_size, (side // chunk_size) * chunk_size)
        jobs: list[tuple[int, int, LocalMetricProjector]] = []
        tile_y = 0
        for start_y in range(0, projector.height_cells, side):
            tile_x = 0
            height = min(side, projector.height_cells - start_y)
            for start_x in range(0, projector.width_cells, side):
                width = min(side, projector.width_cells - start_x)
                jobs.append(
                    (
                        tile_x,
                        tile_y,
                        projector.grid_window(start_x, start_y, width, height),
                    )
                )
                tile_x += 1
            tile_y += 1
        return jobs

    @staticmethod
    def _generation_multiplier(request: MapBuildRequest) -> float:
        """Reserve per-tile space for vertical and non-terrain geometry."""
        quality = str(request.voxel.quality_preset.value).upper()
        return {"LOW": 1.5, "MEDIUM": 2.5, "HIGH": 4.0}.get(quality, 4.0)

    @staticmethod
    def _deduplicate_labels(
        labels: list[LabelPayload],
        maximum: int,
    ) -> tuple[LabelPayload, ...]:
        best: dict[tuple[str, str], LabelPayload] = {}
        for label in labels:
            if label.kind is LabelKind.STREET:
                key = (label.kind.value, label.text.casefold().strip())
            elif label.kind is LabelKind.AREA:
                key = (label.kind.value, "area")
            else:
                key = (
                    label.kind.value,
                    label.source_id or label.text.casefold().strip(),
                )
            current = best.get(key)
            if current is None or label.importance > current.importance:
                best[key] = label
        ordered = sorted(
            best.values(),
            key=lambda item: (-item.importance, item.text.casefold()),
        )
        return tuple(ordered[: max(0, maximum)])

    @staticmethod
    def _display_area_name(request: MapBuildRequest) -> str:
        return (
            request.area_name.strip()
            if request.area_name.strip()
            else request.project_name
        )

    @staticmethod
    def _styled_voxel_meshes(
        meshes: list[MeshPayload],
        model_style: ModelStyle,
    ) -> list[MeshPayload]:
        """Attach stable style variants to voxel-based scene layers."""
        if model_style is ModelStyle.MINECRAFT:
            return [
                replace(
                    payload,
                    material_variant=(
                        payload.material_variant
                        or f"minecraft|{payload.category.value.lower()}"
                    ),
                    collection_group=(
                        payload.collection_group or "Minecraft_Blocks"
                    ),
                )
                for payload in meshes
            ]
        if model_style is ModelStyle.CLASSIC_VOXEL:
            return [
                replace(
                    payload,
                    material_variant=(
                        payload.material_variant
                        or f"classic_voxel|{payload.category.value.lower()}"
                    ),
                )
                for payload in meshes
            ]
        if model_style is ModelStyle.ARCHITECTURAL_MODEL:
            return [
                replace(
                    payload,
                    material_variant=f"architectural|{payload.category.value.lower()}",
                    collection_group=(
                        payload.collection_group or "Architectural_Model"
                    ),
                )
                for payload in meshes
            ]
        return meshes

    def delete(self, project_name: str) -> int:
        """Delete a generated project from the Blender scene."""
        return self._scene_repository.delete_project(project_name)
