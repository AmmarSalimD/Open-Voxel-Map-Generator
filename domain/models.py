"""Immutable domain models used across the extension."""

from __future__ import annotations

from dataclasses import dataclass, field
from math import isfinite
from typing import Mapping, Sequence

from .enums import (
    AccuracyConfidence,
    BuildingSource,
    CurvedDetailKind,
    FacadeSource,
    FeatureType,
    FootprintSource,
    GeometryStyle,
    GeometryType,
    HeightSource,
    InputMode,
    LargeAreaMode,
    LabelKind,
    LabelLanguage,
    LabelMode,
    MaterialStyle,
    ModelStyle,
    QualityPreset,
    RoofSource,
)
from ..core.exceptions import ValidationError


@dataclass(frozen=True, slots=True)
class GeoPoint:
    """A longitude/latitude point in WGS84 decimal degrees."""

    longitude: float
    latitude: float

    def validate(self) -> None:
        """Validate coordinate ranges and finite values."""
        if not isfinite(self.longitude) or not isfinite(self.latitude):
            raise ValidationError("Geographic coordinates must be finite numbers.")
        if not -180.0 <= self.longitude <= 180.0:
            raise ValidationError("Longitude must be between -180 and 180 degrees.")
        if not -90.0 <= self.latitude <= 90.0:
            raise ValidationError("Latitude must be between -90 and 90 degrees.")


@dataclass(frozen=True, slots=True)
class BoundingBox:
    """A geographic bounding box in Overpass order: south, west, north, east."""

    south: float
    west: float
    north: float
    east: float

    def validate(self) -> None:
        """Validate ranges, ordering, and practical query dimensions."""
        GeoPoint(self.west, self.south).validate()
        GeoPoint(self.east, self.north).validate()
        if self.south >= self.north:
            raise ValidationError("South latitude must be lower than north latitude.")
        if self.west >= self.east:
            raise ValidationError("West longitude must be lower than east longitude.")
        if (self.north - self.south) > 1.0 or (self.east - self.west) > 1.0:
            raise ValidationError(
                "The alpha release limits a single request to one degree per axis. "
                "Use a smaller bounding box for reliable GIS and voxel performance."
            )

    @property
    def center(self) -> GeoPoint:
        """Return the geographic center of the box."""
        return GeoPoint(
            longitude=(self.west + self.east) * 0.5,
            latitude=(self.south + self.north) * 0.5,
        )

    def as_overpass(self) -> str:
        """Serialize the box in the order expected by Overpass QL."""
        return f"{self.south:.8f},{self.west:.8f},{self.north:.8f},{self.east:.8f}"

    def as_overture(self) -> tuple[float, float, float, float]:
        """Return west, south, east, north as expected by Overture clients."""
        return self.west, self.south, self.east, self.north


@dataclass(frozen=True, slots=True)
class GeoFeature:
    """A classified geographic feature independent of the source API."""

    source_id: str
    feature_type: FeatureType
    geometry_type: GeometryType
    tags: Mapping[str, str] = field(default_factory=dict)
    points: tuple[GeoPoint, ...] = field(default_factory=tuple)
    outer_rings: tuple[tuple[GeoPoint, ...], ...] = field(default_factory=tuple)
    inner_rings: tuple[tuple[GeoPoint, ...], ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class BuildingAnalysis:
    """Resolved geometry, appearance, and provenance for one building."""

    source_id: str
    height_m: float
    minimum_height_m: float
    height_source: HeightSource
    footprint_source: FootprintSource
    roof_shape: str
    roof_height_m: float
    roof_source: RoofSource
    facade_source: FacadeSource
    facade_profile: str
    facade_color: str
    roof_color: str
    building_type: str
    confidence: AccuracyConfidence
    confidence_score: float
    source_datasets: str = ""


@dataclass(frozen=True, slots=True)
class BuildingAccuracyRecord:
    """Per-building provenance and editable source geometry metadata."""

    source_id: str
    position: tuple[float, float, float]
    height_m: float
    minimum_height_m: float
    height_source: HeightSource
    footprint_source: FootprintSource
    roof_shape: str
    roof_source: RoofSource
    facade_source: FacadeSource
    facade_profile: str
    building_type: str
    confidence: AccuracyConfidence
    confidence_score: float
    source_datasets: str = ""
    roof_height_m: float = 0.0
    facade_color: str = ""
    roof_color: str = ""
    outer_rings_xy: tuple[tuple[tuple[float, float], ...], ...] = field(
        default_factory=tuple
    )
    inner_rings_xy: tuple[tuple[tuple[float, float], ...], ...] = field(
        default_factory=tuple
    )
    is_building_part: bool = False
    parent_source_id: str = ""


@dataclass(frozen=True, slots=True)
class BuildingAccuracyStatistics:
    """Coverage summary for building geometry and appearance confidence."""

    high_confidence: int = 0
    medium_confidence: int = 0
    low_confidence: int = 0
    very_low_confidence: int = 0
    source_facades: int = 0
    unavailable_facades: int = 0
    procedural_facades: int = 0
    tagged_roofs: int = 0
    inferred_roofs: int = 0
    surveyed_footprints: int = 0
    conflated_footprints: int = 0
    machine_footprints: int = 0


@dataclass(frozen=True, slots=True)
class BuildingStatistics:
    """Coverage and height-quality statistics for the building pipeline."""

    osm_buildings: int = 0
    overture_buildings: int = 0
    overture_parts: int = 0
    merged_duplicates: int = 0
    final_building_features: int = 0
    real_height: int = 0
    real_levels: int = 0
    building_parts: int = 0
    inferred_height: int = 0
    default_height: int = 0
    overture_release: str = ""

    def with_height_counts(
        self,
        counts: Mapping[HeightSource, int],
    ) -> "BuildingStatistics":
        """Return a copy updated with rasterizer height-source counts."""
        return BuildingStatistics(
            osm_buildings=self.osm_buildings,
            overture_buildings=self.overture_buildings,
            overture_parts=self.overture_parts,
            merged_duplicates=self.merged_duplicates,
            final_building_features=self.final_building_features,
            real_height=int(counts.get(HeightSource.REAL_HEIGHT, 0)),
            real_levels=int(counts.get(HeightSource.REAL_LEVELS, 0)),
            building_parts=int(counts.get(HeightSource.BUILDING_PART, 0)),
            inferred_height=int(counts.get(HeightSource.INFERRED, 0)),
            default_height=int(counts.get(HeightSource.DEFAULT, 0)),
            overture_release=self.overture_release,
        )


@dataclass(frozen=True, slots=True)
class GeographicDataset:
    """Normalized GIS features plus source provenance and coverage statistics."""

    features: tuple[GeoFeature, ...]
    building_statistics: BuildingStatistics = field(default_factory=BuildingStatistics)
    attributions: tuple[str, ...] = ("© OpenStreetMap contributors",)
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RasterizationReport:
    """Statistics collected while converting source features to voxels."""

    height_source_counts: Mapping[HeightSource, int] = field(default_factory=dict)
    skipped_small_buildings: int = 0
    skipped_parent_buildings: int = 0
    building_records: tuple[BuildingAccuracyRecord, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class VoxelSettings:
    """Settings controlling rasterization, visual detail, and chunking."""

    voxel_size: float
    vertical_step: float
    chunk_size: int
    default_building_height: float
    level_height: float
    tree_density: float
    building_source: BuildingSource = BuildingSource.HYBRID
    use_building_parts: bool = True
    use_roof_shapes: bool = True
    minimum_building_area: float = 12.0
    fallback_river_width: float = 220.0
    infer_missing_heights: bool = True
    generate_landmark_proxies: bool = True
    generate_approximate_buildings: bool = False
    approximate_building_density: float = 0.35
    include_terrain: bool = True
    include_buildings: bool = True
    include_roads: bool = True
    include_water: bool = True
    include_green: bool = True
    include_bridges: bool = True
    include_trees: bool = True
    quality_preset: QualityPreset = QualityPreset.MEDIUM
    enhanced_materials: bool = False
    geometry_style: GeometryStyle = GeometryStyle.VOXEL_ONLY
    model_style: ModelStyle = ModelStyle.CLASSIC_VOXEL
    material_style: MaterialStyle = MaterialStyle.SIMPLE
    curved_detail_segments: int = 16
    curved_detail_limit: int = 500
    generate_labels: bool = False
    label_mode: LabelMode = LabelMode.METADATA_ONLY
    label_language: LabelLanguage = LabelLanguage.LOCAL
    include_street_labels: bool = True
    include_area_labels: bool = True
    include_landmark_labels: bool = True
    maximum_label_count: int = 250
    show_accuracy_overlay: bool = False
    accuracy_overlay_limit: int = 5000
    generate_facade_detail: bool = True
    use_source_facade_hints: bool = True
    strict_real_facades: bool = True
    excluded_building_source_ids: tuple[str, ...] = field(default_factory=tuple)

    @property
    def horizontal_voxel_size(self) -> float:
        """Return the XY voxel resolution in meters."""
        return self.voxel_size

    @property
    def use_curved_details(self) -> bool:
        """Return whether optional non-voxel landmark details are enabled."""
        return (
            self.geometry_style is GeometryStyle.VOXEL_CURVED
            or self.model_style in {
                ModelStyle.LOW_POLY,
                ModelStyle.REAL,
                ModelStyle.ARCHITECTURAL_MODEL,
            }
        )

    def validate(self) -> None:
        """Validate settings before network or geometry work begins."""
        if not 0.5 <= self.voxel_size <= 100.0:
            raise ValidationError(
                "Horizontal Voxel Size must be between 0.5 and 100 meters."
            )
        if not 0.25 <= self.vertical_step <= 20.0:
            raise ValidationError(
                "Vertical Height Step must be between 0.25 and 20 meters."
            )
        if not 8 <= self.chunk_size <= 256:
            raise ValidationError("Chunk Size must be between 8 and 256 voxels.")
        if not 1.0 <= self.default_building_height <= 1000.0:
            raise ValidationError(
                "Default Building Height must be between 1 and 1000 meters."
            )
        if not 1.0 <= self.level_height <= 20.0:
            raise ValidationError("Level Height must be between 1 and 20 meters.")
        if not 0.0 <= self.tree_density <= 1.0:
            raise ValidationError("Tree Density must be between 0 and 1.")
        if not 1.0 <= self.minimum_building_area <= 10_000.0:
            raise ValidationError(
                "Minimum Building Area must be between 1 and 10,000 m²."
            )
        if not 5.0 <= self.fallback_river_width <= 1000.0:
            raise ValidationError(
                "Minimum Major River Width must be between 5 and 1000 meters."
            )
        if not 0.0 <= self.approximate_building_density <= 1.0:
            raise ValidationError(
                "Approximate Building Density must be between 0 and 1."
            )
        if not 6 <= self.curved_detail_segments <= 64:
            raise ValidationError("Curved Detail Segments must be between 6 and 64.")
        if not 0 <= self.curved_detail_limit <= 10_000:
            raise ValidationError("Curved Detail Limit must be between 0 and 10,000.")
        if not 0 <= self.maximum_label_count <= 10_000:
            raise ValidationError("Maximum Label Count must be between 0 and 10,000.")
        if not 0 <= self.accuracy_overlay_limit <= 50_000:
            raise ValidationError("Accuracy Overlay Limit must be between 0 and 50,000.")


@dataclass(frozen=True, slots=True)
class MapBuildRequest:
    """Complete input required by the map generation use case."""

    project_name: str
    input_mode: InputMode
    area_name: str
    bounding_box: BoundingBox
    voxel: VoxelSettings
    max_voxel_cells: int
    large_area_mode: LargeAreaMode = LargeAreaMode.BLOCK

    def validate(self) -> None:
        """Validate the request without performing remote operations."""
        if not self.project_name.strip():
            raise ValidationError("Project Name cannot be empty.")
        self.voxel.validate()
        if self.max_voxel_cells < 100_000:
            raise ValidationError("Maximum voxel cells must be at least 100,000.")
        if self.input_mode is InputMode.AREA_NAME:
            if len(self.area_name.strip()) < 3:
                raise ValidationError(
                    "Area Name must contain at least three characters."
                )
        else:
            self.bounding_box.validate()


@dataclass(frozen=True, slots=True)
class ProjectStatistics:
    """Summary of source features and generated Blender geometry."""

    source_feature_count: int
    voxel_count: int
    chunk_count: int
    object_count: int
    category_counts: Mapping[FeatureType, int]
    curved_detail_count: int = 0
    label_count: int = 0
    building_statistics: BuildingStatistics = field(default_factory=BuildingStatistics)
    attributions: tuple[str, ...] = ("© OpenStreetMap contributors",)
    tile_count: int = 1
    warnings: tuple[str, ...] = ()
    building_accuracy: BuildingAccuracyStatistics = field(
        default_factory=BuildingAccuracyStatistics
    )
    building_records: tuple[BuildingAccuracyRecord, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class MeshPayload:
    """Raw mesh arrays ready to be written to a Blender mesh data block."""

    name: str
    category: FeatureType
    chunk_x: int
    chunk_y: int
    vertices: Sequence[tuple[float, float, float]]
    faces: Sequence[tuple[int, ...]]
    material_variant: str = ""
    collection_group: str = ""
    display_name: str = ""


@dataclass(frozen=True, slots=True)
class CurvedDetailPayload:
    """Optional curved landmark geometry generated without Blender dependencies."""

    name: str
    source_id: str
    kind: CurvedDetailKind
    category: FeatureType
    vertices: Sequence[tuple[float, float, float]]
    faces: Sequence[tuple[int, ...]]


@dataclass(frozen=True, slots=True)
class LabelPayload:
    """One optional map label and its local-space placement metadata."""

    source_id: str
    kind: LabelKind
    text: str
    local_name: str
    arabic_name: str
    english_name: str
    position: tuple[float, float, float]
    rotation_z: float
    importance: int


@dataclass(frozen=True, slots=True)
class GenerationResult:
    """Resolved bounds and final statistics returned by the generation use case."""

    bounds: BoundingBox
    statistics: ProjectStatistics
