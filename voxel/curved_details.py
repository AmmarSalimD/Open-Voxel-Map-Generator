"""Optional curved landmark detail generation independent of Blender APIs."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from math import cos, pi, sin
import re

from ..core.naming import sanitize_name
from ..domain.enums import CurvedDetailKind, FeatureType
from ..domain.models import BuildingAnalysis, CurvedDetailPayload, GeoFeature, GeoPoint, VoxelSettings
from .projection import LocalMetricProjector
from .vertical_layout import SemanticVerticalLayout

_NUMBER_PATTERN = re.compile(r"[-+]?\d+(?:\.\d+)?")


class CurvedDetailBuilder:
    """Create lightweight domes and towers for semantically suitable landmarks."""

    def build(
        self,
        features: Sequence[GeoFeature],
        projector: LocalMetricProjector,
        settings: VoxelSettings,
        project_name: str,
        building_analyses: Mapping[str, BuildingAnalysis] | None = None,
    ) -> tuple[CurvedDetailPayload, ...]:
        """Build optional curved payloads while respecting the configured limit."""
        if not settings.use_curved_details or settings.curved_detail_limit <= 0:
            return ()

        payloads: list[CurvedDetailPayload] = []
        used_sources: set[tuple[str, CurvedDetailKind]] = set()
        for feature in features:
            descriptor = self._descriptor(feature)
            if descriptor is None:
                continue
            kind = descriptor
            key = (feature.source_id, kind)
            if key in used_sources:
                continue
            center = self._feature_center(feature)
            if center is None:
                continue
            metric = projector.project(center)
            radius = self._feature_radius(feature, projector, settings)
            analysis = (building_analyses or {}).get(feature.source_id)
            height = analysis.height_m if analysis is not None else self._feature_height(feature.tags, settings)
            segments = settings.curved_detail_segments
            vertices, faces = self._geometry(
                kind,
                metric.x,
                metric.y,
                radius,
                height,
                segments,
            )
            if not vertices or not faces:
                continue
            payloads.append(
                CurvedDetailPayload(
                    name=(
                        f"OVMG_{sanitize_name(project_name)}_Curved_"
                        f"{kind.value}_{len(payloads):04d}"
                    ),
                    source_id=feature.source_id,
                    kind=kind,
                    category=FeatureType.BUILDING,
                    vertices=vertices,
                    faces=faces,
                )
            )
            used_sources.add(key)
            if len(payloads) >= settings.curved_detail_limit:
                break
        return tuple(payloads)

    @staticmethod
    def _descriptor(feature: GeoFeature) -> CurvedDetailKind | None:
        tags = feature.tags
        man_made = tags.get("man_made", "").casefold()
        historic = tags.get("historic", "").casefold()
        roof_shape = tags.get("roof:shape", "").casefold()
        building = tags.get("building", tags.get("building:part", "")).casefold()
        amenity = tags.get("amenity", "").casefold()
        if man_made == "minaret":
            return CurvedDetailKind.MINARET
        if man_made == "water_tower":
            return CurvedDetailKind.WATER_TOWER
        if man_made == "tower" or historic == "tower":
            return CurvedDetailKind.TOWER
        if roof_shape in {"dome", "onion"}:
            return CurvedDetailKind.DOME
        if amenity == "place_of_worship" or building in {
            "mosque",
            "church",
            "cathedral",
            "synagogue",
            "temple",
        }:
            return CurvedDetailKind.DOME
        return None

    @staticmethod
    def _feature_center(feature: GeoFeature) -> GeoPoint | None:
        if feature.points:
            points = feature.points
        elif feature.outer_rings:
            points = feature.outer_rings[0]
        else:
            return None
        usable = points[:-1] if len(points) > 1 and points[0] == points[-1] else points
        if not usable:
            return None
        return GeoPoint(
            longitude=sum(point.longitude for point in usable) / len(usable),
            latitude=sum(point.latitude for point in usable) / len(usable),
        )

    @staticmethod
    def _feature_radius(
        feature: GeoFeature,
        projector: LocalMetricProjector,
        settings: VoxelSettings,
    ) -> float:
        if feature.outer_rings:
            metric = [projector.project(point) for point in feature.outer_rings[0]]
            if metric:
                width = max(point.x for point in metric) - min(
                    point.x for point in metric
                )
                depth = max(point.y for point in metric) - min(
                    point.y for point in metric
                )
                return max(1.5, min(16.0, min(width, depth) * 0.30))
        tags = feature.tags
        if tags.get("man_made") == "minaret":
            return 1.7
        if tags.get("man_made") == "water_tower":
            return 4.0
        if tags.get("man_made") == "tower":
            return 3.0
        return max(2.5, settings.voxel_size * 1.4)

    @classmethod
    def _feature_height(
        cls,
        tags: Mapping[str, str],
        settings: VoxelSettings,
    ) -> float:
        explicit = cls._tag_number(tags, "height")
        if explicit is not None and explicit > 0.0:
            return explicit
        levels = cls._tag_number(tags, "building:levels")
        if levels is not None and levels > 0.0:
            return levels * settings.level_height
        man_made = tags.get("man_made", "")
        if man_made == "minaret":
            return 36.0
        if man_made == "water_tower":
            return 24.0
        if man_made == "tower" or tags.get("historic") == "tower":
            return 30.0
        if tags.get("amenity") == "place_of_worship":
            return 16.0
        return settings.default_building_height

    @staticmethod
    def _tag_number(tags: Mapping[str, str], key: str) -> float | None:
        value = tags.get(key)
        if value is None:
            return None
        match = _NUMBER_PATTERN.search(value.replace(",", "."))
        if match is None:
            return None
        try:
            return float(match.group(0))
        except ValueError:
            return None

    def _geometry(
        self,
        kind: CurvedDetailKind,
        x: float,
        y: float,
        radius: float,
        height: float,
        segments: int,
    ) -> tuple[list[tuple[float, float, float]], list[tuple[int, ...]]]:
        ground = SemanticVerticalLayout.GROUND_TOP
        if kind is CurvedDetailKind.DOME:
            dome_height = max(2.0, min(radius * 0.85, height * 0.38))
            base = ground + max(2.5, height - dome_height)
            return self._hemisphere(x, y, base, radius, dome_height, segments)
        if kind is CurvedDetailKind.MINARET:
            body_radius = max(0.8, radius)
            cap_height = max(1.8, body_radius * 2.0)
            body_height = max(6.0, height - cap_height)
            return self._combine(
                self._cylinder(
                    x, y, ground, ground + body_height, body_radius, segments
                ),
                self._cone(
                    x,
                    y,
                    ground + body_height,
                    ground + height,
                    body_radius * 1.25,
                    segments,
                ),
            )
        if kind is CurvedDetailKind.WATER_TOWER:
            stem_height = max(5.0, height * 0.68)
            stem_radius = max(0.8, radius * 0.30)
            tank_radius = max(2.0, radius)
            return self._combine(
                self._cylinder(
                    x, y, ground, ground + stem_height, stem_radius, segments
                ),
                self._sphere(
                    x,
                    y,
                    ground + stem_height + tank_radius * 0.65,
                    tank_radius,
                    segments,
                ),
            )
        body_radius = max(1.2, radius)
        cap_height = max(1.5, min(5.0, height * 0.16))
        return self._combine(
            self._cylinder(
                x,
                y,
                ground,
                ground + height - cap_height,
                body_radius,
                segments,
            ),
            self._cone(
                x,
                y,
                ground + height - cap_height,
                ground + height,
                body_radius,
                segments,
            ),
        )

    @staticmethod
    def _combine(
        *parts: tuple[list[tuple[float, float, float]], list[tuple[int, ...]]],
    ) -> tuple[list[tuple[float, float, float]], list[tuple[int, ...]]]:
        vertices: list[tuple[float, float, float]] = []
        faces: list[tuple[int, ...]] = []
        for part_vertices, part_faces in parts:
            offset = len(vertices)
            vertices.extend(part_vertices)
            faces.extend(tuple(index + offset for index in face) for face in part_faces)
        return vertices, faces

    @staticmethod
    def _cylinder(
        x: float,
        y: float,
        z0: float,
        z1: float,
        radius: float,
        segments: int,
    ) -> tuple[list[tuple[float, float, float]], list[tuple[int, ...]]]:
        vertices = []
        for z in (z0, z1):
            for index in range(segments):
                angle = 2.0 * pi * index / segments
                vertices.append((x + radius * cos(angle), y + radius * sin(angle), z))
        faces: list[tuple[int, ...]] = []
        for index in range(segments):
            nxt = (index + 1) % segments
            faces.append((index, nxt, segments + nxt, segments + index))
        faces.append(tuple(reversed(range(segments))))
        faces.append(tuple(range(segments, segments * 2)))
        return vertices, faces

    @staticmethod
    def _cone(
        x: float,
        y: float,
        z0: float,
        z1: float,
        radius: float,
        segments: int,
    ) -> tuple[list[tuple[float, float, float]], list[tuple[int, ...]]]:
        vertices = [
            (
                x + radius * cos(2.0 * pi * index / segments),
                y + radius * sin(2.0 * pi * index / segments),
                z0,
            )
            for index in range(segments)
        ]
        apex = len(vertices)
        vertices.append((x, y, z1))
        faces = [(index, (index + 1) % segments, apex) for index in range(segments)]
        faces.append(tuple(reversed(range(segments))))
        return vertices, faces

    @staticmethod
    def _hemisphere(
        x: float,
        y: float,
        base_z: float,
        radius: float,
        height: float,
        segments: int,
    ) -> tuple[list[tuple[float, float, float]], list[tuple[int, ...]]]:
        rings = max(3, segments // 4)
        vertices: list[tuple[float, float, float]] = []
        for ring in range(rings):
            factor = ring / rings
            theta = factor * pi * 0.5
            ring_radius = radius * cos(theta)
            z = base_z + height * sin(theta)
            for index in range(segments):
                angle = 2.0 * pi * index / segments
                vertices.append(
                    (x + ring_radius * cos(angle), y + ring_radius * sin(angle), z)
                )
        apex = len(vertices)
        vertices.append((x, y, base_z + height))
        faces: list[tuple[int, ...]] = []
        for ring in range(rings - 1):
            start = ring * segments
            next_start = (ring + 1) * segments
            for index in range(segments):
                nxt = (index + 1) % segments
                faces.append(
                    (start + index, start + nxt, next_start + nxt, next_start + index)
                )
        top_start = (rings - 1) * segments
        for index in range(segments):
            faces.append((top_start + index, top_start + (index + 1) % segments, apex))
        return vertices, faces

    @staticmethod
    def _sphere(
        x: float,
        y: float,
        z: float,
        radius: float,
        segments: int,
    ) -> tuple[list[tuple[float, float, float]], list[tuple[int, ...]]]:
        rings = max(4, segments // 2)
        vertices: list[tuple[float, float, float]] = [(x, y, z + radius)]
        for ring in range(1, rings):
            theta = pi * ring / rings
            ring_radius = radius * sin(theta)
            ring_z = z + radius * cos(theta)
            for index in range(segments):
                angle = 2.0 * pi * index / segments
                vertices.append(
                    (x + ring_radius * cos(angle), y + ring_radius * sin(angle), ring_z)
                )
        bottom = len(vertices)
        vertices.append((x, y, z - radius))
        faces: list[tuple[int, ...]] = []
        for index in range(segments):
            faces.append((0, 1 + index, 1 + (index + 1) % segments))
        for ring in range(rings - 2):
            start = 1 + ring * segments
            next_start = start + segments
            for index in range(segments):
                nxt = (index + 1) % segments
                faces.append(
                    (start + index, next_start + index, next_start + nxt, start + nxt)
                )
        last_start = 1 + (rings - 2) * segments
        for index in range(segments):
            faces.append(
                (last_start + index, bottom, last_start + (index + 1) % segments)
            )
        return vertices, faces
