"""Optional map-label extraction and local-space placement."""

from __future__ import annotations

from math import atan2, hypot
from collections.abc import Mapping, Sequence
import unicodedata

from ..domain.enums import FeatureType, GeometryType, LabelKind, LabelLanguage
from ..domain.models import (
    BoundingBox,
    GeoFeature,
    GeoPoint,
    LabelPayload,
    VoxelSettings,
)
from .projection import LocalMetricProjector
from .vertical_layout import SemanticVerticalLayout


class MapLabelBuilder:
    """Extract deduplicated street, area, and landmark label metadata."""

    _ROAD_IMPORTANCE: dict[str, int] = {
        "motorway": 95,
        "trunk": 90,
        "primary": 85,
        "secondary": 75,
        "tertiary": 65,
        "residential": 45,
        "living_street": 40,
        "unclassified": 35,
        "service": 25,
        "pedestrian": 20,
        "footway": 10,
        "path": 8,
    }

    def build(
        self,
        features: Sequence[GeoFeature],
        bounds: BoundingBox,
        projector: LocalMetricProjector,
        settings: VoxelSettings,
        area_name: str,
    ) -> tuple[LabelPayload, ...]:
        """Return labels ordered by importance and capped by user settings."""
        if not settings.generate_labels or settings.maximum_label_count <= 0:
            return ()

        labels: list[LabelPayload] = []
        if settings.include_area_labels:
            labels.append(
                self._area_label(bounds, projector, settings, area_name.strip())
            )

        if settings.include_street_labels:
            labels.extend(self._street_labels(features, projector, settings))
        if settings.include_landmark_labels:
            labels.extend(self._landmark_labels(features, projector, settings))

        labels.sort(key=lambda item: (-item.importance, item.text.casefold()))
        return tuple(labels[: settings.maximum_label_count])

    def _area_label(
        self,
        bounds: BoundingBox,
        projector: LocalMetricProjector,
        settings: VoxelSettings,
        area_name: str,
    ) -> LabelPayload:
        center = projector.project(bounds.center)
        text = area_name or "Generated Map"
        return LabelPayload(
            source_id="ovmg/area",
            kind=LabelKind.AREA,
            text=text,
            local_name=text,
            arabic_name="",
            english_name=text,
            position=(center.x, center.y, SemanticVerticalLayout.GROUND_TOP + 8.0),
            rotation_z=0.0,
            importance=100,
        )

    def _street_labels(
        self,
        features: Sequence[GeoFeature],
        projector: LocalMetricProjector,
        settings: VoxelSettings,
    ) -> list[LabelPayload]:
        best_by_name: dict[str, tuple[float, LabelPayload]] = {}
        for feature in features:
            if feature.feature_type is not FeatureType.ROAD:
                continue
            if (
                feature.geometry_type is not GeometryType.LINE
                or len(feature.points) < 2
            ):
                continue
            names = self._names(feature.tags)
            text = self._select_text(names, settings.label_language)
            if not text:
                continue
            metric = [projector.project(point) for point in feature.points]
            length = sum(
                hypot(second.x - first.x, second.y - first.y)
                for first, second in zip(metric, metric[1:])
            )
            if length < max(20.0, settings.voxel_size * 4.0):
                continue
            midpoint_index = max(0, min(len(metric) - 2, (len(metric) - 1) // 2))
            first = metric[midpoint_index]
            second = metric[midpoint_index + 1]
            position = (
                (first.x + second.x) * 0.5,
                (first.y + second.y) * 0.5,
                SemanticVerticalLayout.ROAD_TOP + 0.12,
            )
            importance = self._ROAD_IMPORTANCE.get(
                feature.tags.get("highway", ""),
                30,
            )
            payload = LabelPayload(
                source_id=feature.source_id,
                kind=LabelKind.STREET,
                text=text,
                local_name=names[0],
                arabic_name=names[1],
                english_name=names[2],
                position=position,
                rotation_z=atan2(second.y - first.y, second.x - first.x),
                importance=importance,
            )
            key = (names[0] or names[1] or names[2] or text).casefold().strip()
            previous = best_by_name.get(key)
            if previous is None or length > previous[0]:
                best_by_name[key] = (length, payload)
        return [value[1] for value in best_by_name.values()]

    def _landmark_labels(
        self,
        features: Sequence[GeoFeature],
        projector: LocalMetricProjector,
        settings: VoxelSettings,
    ) -> list[LabelPayload]:
        labels: list[LabelPayload] = []
        seen: set[str] = set()
        for feature in features:
            if feature.feature_type is not FeatureType.BUILDING:
                continue
            names = self._names(feature.tags)
            text = self._select_text(names, settings.label_language)
            if not text:
                continue
            key = text.casefold().strip()
            if key in seen:
                continue
            if not self._is_landmark(feature.tags):
                continue
            center = self._feature_center(feature)
            if center is None:
                continue
            metric = projector.project(center)
            labels.append(
                LabelPayload(
                    source_id=feature.source_id,
                    kind=LabelKind.LANDMARK,
                    text=text,
                    local_name=names[0],
                    arabic_name=names[1],
                    english_name=names[2],
                    position=(
                        metric.x,
                        metric.y,
                        SemanticVerticalLayout.GROUND_TOP + 5.0,
                    ),
                    rotation_z=0.0,
                    importance=80,
                )
            )
            seen.add(key)
        return labels

    @staticmethod
    def _names(tags: Mapping[str, str]) -> tuple[str, str, str]:
        local = tags.get("name", "").strip()
        arabic = tags.get("name:ar", "").strip()
        english = tags.get("name:en", "").strip()
        return local, arabic, english

    @staticmethod
    def _select_text(
        names: tuple[str, str, str],
        language: LabelLanguage,
    ) -> str:
        local, arabic, english = names
        if language is LabelLanguage.ARABIC:
            return arabic or local or english
        if language is LabelLanguage.ENGLISH:
            if english:
                return english
            fallback = local or arabic
            if any(unicodedata.bidirectional(char) in {"R", "AL", "AN"} for char in fallback):
                return ""
            return fallback
        if language is LabelLanguage.BILINGUAL:
            first = arabic or local
            second = english if english and english != first else ""
            return f"{first}\n{second}".strip() if first or second else ""
        return local or arabic or english

    @staticmethod
    def _is_landmark(tags: Mapping[str, str]) -> bool:
        return bool(
            tags.get("amenity")
            in {
                "place_of_worship",
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
                "marketplace",
            }
            or tags.get("tourism")
            or tags.get("historic")
            or tags.get("man_made") in {"tower", "minaret", "water_tower"}
            or tags.get("office") == "government"
        )

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
