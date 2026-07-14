"""Direct low-poly and real-data building mesh generation.

Classic Voxel and Minecraft continue through the sparse voxel rasterizer. Low
Poly and Real styles instead preserve source footprint coordinates and batch
extruded buildings by chunk and facade profile, producing a visibly different
geometry language without creating one Blender object per building.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from math import floor
from typing import Any

from ..core.naming import sanitize_name
from ..domain.enums import FeatureType, GeometryType, ModelStyle
from ..domain.models import BuildingAnalysis, GeoFeature, MeshPayload, VoxelSettings
from .projection import LocalMetricProjector
from .style_profiles import style_profile


@dataclass(slots=True)
class _MeshAccumulator:
    vertices: list[tuple[float, float, float]] = field(default_factory=list)
    faces: list[tuple[int, ...]] = field(default_factory=list)

    def append(
        self,
        vertices: Sequence[tuple[float, float, float]],
        faces: Sequence[tuple[int, ...]],
    ) -> None:
        offset = len(self.vertices)
        self.vertices.extend(vertices)
        self.faces.extend(tuple(index + offset for index in face) for face in faces)


class DirectBuildingMeshBuilder:
    """Build batched footprint-preserving meshes for Low Poly and Real styles."""

    def build(
        self,
        features: Sequence[GeoFeature],
        analyses: Mapping[str, BuildingAnalysis],
        projector: LocalMetricProjector,
        settings: VoxelSettings,
        project_name: str,
    ) -> list[MeshPayload]:
        profile = style_profile(settings.model_style)
        if not settings.include_buildings or not profile.direct_buildings:
            return []
        shapely = self._load_shapely()
        part_parents = {
            feature.tags.get("ovmg:building_id", "")
            for feature in features
            if feature.feature_type is FeatureType.BUILDING
            and feature.tags.get("ovmg:is_part") == "yes"
        }
        part_parents.discard("")

        batches: dict[tuple[int, int, str], _MeshAccumulator] = defaultdict(_MeshAccumulator)
        for feature in features:
            if feature.feature_type is not FeatureType.BUILDING:
                continue
            analysis = analyses.get(feature.source_id)
            if analysis is None:
                continue
            effective_parts = settings.use_building_parts and profile.allow_building_parts
            if feature.tags.get("ovmg:is_part") == "yes" and not effective_parts:
                continue
            if (
                effective_parts
                and feature.tags.get("ovmg:has_parts") == "yes"
                and feature.source_id.rsplit("/", 1)[-1] in part_parents
            ):
                continue
            polygons = self._feature_polygons(feature, projector, settings, shapely)
            if not polygons:
                continue
            center = self._feature_center_xy(polygons)
            chunk_span = settings.chunk_size * settings.voxel_size
            chunk_x = floor((center[0] - projector.min_x) / max(0.001, chunk_span))
            chunk_y = floor((center[1] - projector.min_y) / max(0.001, chunk_span))
            variant = self._material_variant(analysis, settings)
            for polygon in polygons:
                vertices, faces = self._extrude_polygon(
                    polygon,
                    analysis,
                    settings,
                    shapely,
                )
                if faces:
                    batches[(chunk_x, chunk_y, variant)].append(vertices, faces)

        payloads: list[MeshPayload] = []
        style_name = settings.model_style.value.title().replace("_", "")
        for index, ((chunk_x, chunk_y, variant), batch) in enumerate(sorted(batches.items())):
            safe_variant = sanitize_name(variant, fallback="Building")[:42]
            payloads.append(
                MeshPayload(
                    name=(
                        f"OVMG_{sanitize_name(project_name)}_{style_name}_Buildings_"
                        f"{chunk_x}_{chunk_y}_{safe_variant}_{index:04d}"
                    ),
                    category=FeatureType.BUILDING,
                    chunk_x=chunk_x,
                    chunk_y=chunk_y,
                    vertices=batch.vertices,
                    faces=batch.faces,
                    material_variant=variant,
                    collection_group=f"{style_name}_Buildings",
                    display_name=f"{style_name} buildings",
                )
            )
        return payloads

    def _feature_polygons(
        self,
        feature: GeoFeature,
        projector: LocalMetricProjector,
        settings: VoxelSettings,
        shapely: Any,
    ) -> list[Any]:
        if feature.geometry_type is GeometryType.POINT and feature.points:
            center = projector.project(feature.points[0])
            radius = max(2.0, settings.voxel_size * 1.5)
            return [shapely.box(center.x - radius, center.y - radius, center.x + radius, center.y + radius)]
        if feature.geometry_type is not GeometryType.POLYGON:
            return []
        inners = [
            [(projector.project(point).x, projector.project(point).y) for point in ring]
            for ring in feature.inner_rings
            if len(ring) >= 4
        ]
        polygons: list[Any] = []
        for outer in feature.outer_rings:
            if len(outer) < 4:
                continue
            shell = [(projector.project(point).x, projector.project(point).y) for point in outer]
            shell_polygon = shapely.Polygon(shell)
            holes = [ring for ring in inners if shell_polygon.covers(shapely.Point(ring[0]))]
            polygon = shapely.Polygon(shell, holes)
            if not polygon.is_valid:
                polygon = shapely.make_valid(polygon)
            for item in self._iter_polygons(polygon):
                if item.area < settings.minimum_building_area:
                    continue
                tolerance = style_profile(settings.model_style).footprint_simplification_m
                if tolerance > 0.0:
                    item = item.simplify(tolerance, preserve_topology=True)
                if not item.is_empty and item.area >= settings.minimum_building_area:
                    polygons.append(item)
        return polygons

    def _extrude_polygon(
        self,
        polygon: Any,
        analysis: BuildingAnalysis,
        settings: VoxelSettings,
        shapely: Any,
    ) -> tuple[list[tuple[float, float, float]], list[tuple[int, ...]]]:
        bottom = analysis.minimum_height_m
        profile = style_profile(settings.model_style)
        roof_height = (
            analysis.roof_height_m
            if settings.use_roof_shapes and profile.allow_roof_shapes
            else 0.0
        )
        wall_top = max(bottom + settings.vertical_step, analysis.height_m - roof_height)
        vertices: list[tuple[float, float, float]] = []
        faces: list[tuple[int, ...]] = []

        rings = [polygon.exterior, *polygon.interiors]
        for ring_index, ring in enumerate(rings):
            coordinates = list(ring.coords)
            if len(coordinates) < 4:
                continue
            coordinates = coordinates[:-1]
            base_start = len(vertices)
            vertices.extend((float(x), float(y), bottom) for x, y in coordinates)
            top_start = len(vertices)
            vertices.extend((float(x), float(y), wall_top) for x, y in coordinates)
            for index in range(len(coordinates)):
                nxt = (index + 1) % len(coordinates)
                face = (
                    base_start + index,
                    base_start + nxt,
                    top_start + nxt,
                    top_start + index,
                )
                faces.append(tuple(reversed(face)) if ring_index else face)

        triangles = self._inside_triangles(polygon, shapely)
        for triangle in triangles:
            coordinates = list(triangle.exterior.coords)[:3]
            bottom_indices = []
            top_indices = []
            for x, y in coordinates:
                bottom_indices.append(len(vertices))
                vertices.append((float(x), float(y), bottom))
                top_indices.append(len(vertices))
                vertices.append(
                    (
                        float(x),
                        float(y),
                        self._roof_z(float(x), float(y), polygon.bounds, wall_top, roof_height, analysis.roof_shape),
                    )
                )
            faces.append(tuple(reversed(bottom_indices)))
            faces.append(tuple(top_indices))
        return vertices, faces

    @staticmethod
    def _inside_triangles(polygon: Any, shapely: Any) -> list[Any]:
        triangles = []
        for triangle in shapely.ops.triangulate(polygon):
            clipped = triangle.intersection(polygon)
            for candidate in DirectBuildingMeshBuilder._iter_polygons(clipped):
                if candidate.area <= 1e-5:
                    continue
                coords = list(candidate.exterior.coords)
                if len(coords) == 4:
                    triangles.append(candidate)
                else:
                    triangles.extend(
                        item
                        for item in shapely.ops.triangulate(candidate)
                        if candidate.covers(item.representative_point())
                    )
        return triangles

    @staticmethod
    def _roof_z(
        x: float,
        y: float,
        bounds: tuple[float, float, float, float],
        base: float,
        height: float,
        shape: str,
    ) -> float:
        if height <= 0.0 or shape in {"", "flat", "none", "dome", "onion", "cone"}:
            return base
        min_x, min_y, max_x, max_y = bounds
        width = max(0.001, max_x - min_x)
        depth = max(0.001, max_y - min_y)
        nx = (x - min_x) / width
        ny = (y - min_y) / depth
        if shape == "skillion":
            return base + height * nx
        if shape in {"gabled", "gambrel", "saltbox"}:
            cross = ny if width >= depth else nx
            ridge = max(0.0, 1.0 - abs(cross - 0.5) * 2.0)
            if shape == "gambrel":
                ridge = min(1.0, ridge * 1.35)
            if shape == "saltbox":
                ridge = max(0.0, 1.0 - abs(cross - 0.42) * 1.72)
            return base + height * ridge
        edge = min(nx, 1.0 - nx, ny, 1.0 - ny)
        normalized = min(1.0, max(0.0, edge * 2.0))
        return base + height * normalized

    @staticmethod
    def _feature_center_xy(polygons: Sequence[Any]) -> tuple[float, float]:
        area = sum(max(0.0, polygon.area) for polygon in polygons)
        if area <= 0.0:
            centroid = polygons[0].centroid
            return float(centroid.x), float(centroid.y)
        x = sum(polygon.centroid.x * polygon.area for polygon in polygons) / area
        y = sum(polygon.centroid.y * polygon.area for polygon in polygons) / area
        return float(x), float(y)

    @staticmethod
    def _material_variant(analysis: BuildingAnalysis, settings: VoxelSettings) -> str:
        color = analysis.facade_color.lstrip("#")
        roof = analysis.roof_color.lstrip("#")
        if settings.model_style is ModelStyle.ARCHITECTURAL_MODEL:
            return "architectural|building"
        detail = (
            "facade"
            if settings.generate_facade_detail
            and settings.model_style is ModelStyle.REAL
            and not settings.strict_real_facades
            else "plain"
        )
        return (
            f"building|{analysis.facade_profile}|{color}|{roof}|{detail}|"
            f"{analysis.confidence.value.lower()}"
        )

    @staticmethod
    def _iter_polygons(geometry: Any):
        geom_type = getattr(geometry, "geom_type", "")
        if geom_type == "Polygon":
            yield geometry
        elif geom_type in {"MultiPolygon", "GeometryCollection"}:
            for child in geometry.geoms:
                yield from DirectBuildingMeshBuilder._iter_polygons(child)

    @staticmethod
    def _load_shapely() -> Any:
        import shapely
        import shapely.ops

        return shapely
