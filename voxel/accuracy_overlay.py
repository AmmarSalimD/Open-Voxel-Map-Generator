"""Optional building-confidence overlay geometry."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from math import floor
from typing import Any

from ..core.naming import sanitize_name
from ..domain.enums import FeatureType, GeometryType
from ..domain.models import BuildingAnalysis, GeoFeature, MeshPayload, VoxelSettings
from .projection import LocalMetricProjector


@dataclass(slots=True)
class _Accumulator:
    vertices: list[tuple[float, float, float]] = field(default_factory=list)
    faces: list[tuple[int, ...]] = field(default_factory=list)

    def add_triangle(self, points: Sequence[tuple[float, float, float]]) -> None:
        offset = len(self.vertices)
        self.vertices.extend(points)
        self.faces.append(tuple(range(offset, offset + len(points))))


class AccuracyOverlayBuilder:
    """Build thin, toggleable roof plates colored by confidence level."""

    def build(
        self,
        features: Sequence[GeoFeature],
        analyses: Mapping[str, BuildingAnalysis],
        projector: LocalMetricProjector,
        settings: VoxelSettings,
        project_name: str,
    ) -> list[MeshPayload]:
        if not settings.show_accuracy_overlay or settings.accuracy_overlay_limit <= 0:
            return []
        shapely = self._load_shapely()
        batches: dict[tuple[int, int, str], _Accumulator] = defaultdict(_Accumulator)
        count = 0
        chunk_span = settings.chunk_size * settings.voxel_size
        for feature in features:
            if count >= settings.accuracy_overlay_limit:
                break
            analysis = analyses.get(feature.source_id)
            if analysis is None or feature.geometry_type is not GeometryType.POLYGON:
                continue
            polygons = self._polygons(feature, projector, shapely)
            if not polygons:
                continue
            z = max(analysis.height_m, analysis.minimum_height_m) + 0.12
            for polygon in polygons:
                centroid = polygon.centroid
                chunk_x = floor((centroid.x - projector.min_x) / max(0.001, chunk_span))
                chunk_y = floor((centroid.y - projector.min_y) / max(0.001, chunk_span))
                key = (chunk_x, chunk_y, f"accuracy|{analysis.confidence.value.lower()}")
                for triangle in shapely.ops.triangulate(polygon):
                    if not polygon.covers(triangle.representative_point()):
                        continue
                    coords = list(triangle.exterior.coords)[:3]
                    batches[key].add_triangle([(float(x), float(y), z) for x, y in coords])
            count += 1

        payloads: list[MeshPayload] = []
        for index, ((chunk_x, chunk_y, variant), batch) in enumerate(sorted(batches.items())):
            payloads.append(
                MeshPayload(
                    name=(
                        f"OVMG_{sanitize_name(project_name)}_Accuracy_"
                        f"{chunk_x}_{chunk_y}_{index:04d}"
                    ),
                    category=FeatureType.BUILDING,
                    chunk_x=chunk_x,
                    chunk_y=chunk_y,
                    vertices=batch.vertices,
                    faces=batch.faces,
                    material_variant=variant,
                    collection_group="Building_Accuracy_Overlay",
                    display_name="Building accuracy overlay",
                )
            )
        return payloads

    @staticmethod
    def _polygons(feature: GeoFeature, projector: LocalMetricProjector, shapely: Any) -> list[Any]:
        polygons = []
        inner_rings = [
            [(projector.project(point).x, projector.project(point).y) for point in ring]
            for ring in feature.inner_rings
            if len(ring) >= 4
        ]
        for outer in feature.outer_rings:
            if len(outer) < 4:
                continue
            shell = [(projector.project(point).x, projector.project(point).y) for point in outer]
            shell_polygon = shapely.Polygon(shell)
            holes = [ring for ring in inner_rings if shell_polygon.covers(shapely.Point(ring[0]))]
            polygon = shapely.Polygon(shell, holes)
            if not polygon.is_valid:
                polygon = shapely.make_valid(polygon)
            if polygon.geom_type == "Polygon":
                polygons.append(polygon)
            elif polygon.geom_type == "MultiPolygon":
                polygons.extend(polygon.geoms)
        return polygons

    @staticmethod
    def _load_shapely() -> Any:
        import shapely
        import shapely.ops

        return shapely
