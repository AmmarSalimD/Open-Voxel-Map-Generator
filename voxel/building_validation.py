"""Always-on safety validation for generated building geometry.

The public UI intentionally exposes only a small number of high-level choices.
This module enforces conservative footprint rules internally so malformed source
polygons cannot become kilometer-wide slabs. Height and floor metadata are resolved
separately by the source-first building analyzer.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Sequence

from ..domain.enums import FeatureType, GeometryType
from ..domain.models import (
    BuildingStatistics,
    GeoFeature,
    GeographicDataset,
    VoxelSettings,
)
from .building_analysis import BuildingAnalyzer
from .projection import LocalMetricProjector


@dataclass(frozen=True, slots=True)
class BuildingValidationReport:
    """Counts collected while rejecting unsafe source footprints."""

    accepted_buildings: int = 0
    rejected_invalid: int = 0
    rejected_oversized: int = 0
    rejected_outside_area: int = 0
    rejected_optional: int = 0

    @property
    def rejected_total(self) -> int:
        """Return the total number of rejected building features."""
        return (
            self.rejected_invalid
            + self.rejected_oversized
            + self.rejected_outside_area
            + self.rejected_optional
        )


class BuildingSafetyValidator:
    """Filter unsafe footprints before analysis, rasterization, and meshing."""

    _GENERAL_MAX_AREA_M2 = 120_000.0
    _LARGE_USE_MAX_AREA_M2 = 320_000.0
    _GENERAL_MAX_SPAN_M = 500.0
    _LARGE_USE_MAX_SPAN_M = 900.0
    _GENERAL_MAX_ASPECT = 20.0
    _LARGE_USE_MAX_ASPECT = 35.0
    _CENTER_MARGIN_M = 75.0
    _LARGE_BUILDING_TAGS = {
        "industrial",
        "warehouse",
        "hangar",
        "stadium",
        "sports_hall",
        "train_station",
        "transportation",
        "terminal",
        "hospital",
        "university",
        "college",
        "civic",
        "government",
        "retail",
        "commercial",
    }

    def filter_dataset(
        self,
        dataset: GeographicDataset,
        projector: LocalMetricProjector,
        settings: VoxelSettings,
    ) -> tuple[GeographicDataset, BuildingValidationReport]:
        """Return a dataset with malformed or implausible buildings removed."""
        retained: list[GeoFeature] = []
        accepted = invalid = oversized = outside = optional = 0

        for feature in dataset.features:
            if feature.feature_type is not FeatureType.BUILDING:
                retained.append(feature)
                continue

            if feature.tags.get("ovmg:approximate") == "yes":
                if settings.generate_approximate_buildings:
                    retained.append(feature)
                    accepted += 1
                else:
                    optional += 1
                continue

            if feature.geometry_type is GeometryType.POINT:
                if settings.generate_landmark_proxies and feature.points:
                    retained.append(feature)
                    accepted += 1
                else:
                    optional += 1
                continue

            if feature.geometry_type is not GeometryType.POLYGON:
                invalid += 1
                continue

            metrics = self._metrics(feature, projector)
            if metrics is None:
                invalid += 1
                continue
            area_m2, min_x, min_y, max_x, max_y = metrics
            width = max_x - min_x
            depth = max_y - min_y
            span = max(width, depth)
            short_side = max(0.01, min(width, depth))
            aspect = span / short_side

            minimum_area = (
                4.0 if self._is_landmark(feature) else settings.minimum_building_area
            )
            if area_m2 < minimum_area:
                invalid += 1
                continue

            if not self._center_is_near_map(min_x, min_y, max_x, max_y, projector):
                outside += 1
                continue

            large_use = self._is_large_use(feature)
            max_area = (
                self._LARGE_USE_MAX_AREA_M2
                if large_use
                else self._GENERAL_MAX_AREA_M2
            )
            max_span = (
                self._LARGE_USE_MAX_SPAN_M
                if large_use
                else self._GENERAL_MAX_SPAN_M
            )
            max_aspect = (
                self._LARGE_USE_MAX_ASPECT
                if large_use
                else self._GENERAL_MAX_ASPECT
            )

            # A single source polygon must not dominate the chosen map area.
            map_area = max(
                1.0,
                (projector.max_x - projector.min_x)
                * (projector.max_y - projector.min_y),
            )
            relative_limit = map_area * (0.18 if large_use else 0.08)
            minimum_area_allowance = 80_000.0 if large_use else 20_000.0
            effective_max_area = min(
                max_area,
                max(minimum_area_allowance, relative_limit),
            )
            map_span_limit = max(
                projector.max_x - projector.min_x,
                projector.max_y - projector.min_y,
            ) * (0.90 if large_use else 0.72)
            minimum_span_allowance = 400.0 if large_use else 180.0
            effective_max_span = min(
                max_span,
                max(minimum_span_allowance, map_span_limit),
            )

            if (
                area_m2 > effective_max_area
                or span > effective_max_span
                or aspect > max_aspect
            ):
                oversized += 1
                continue

            retained.append(feature)
            accepted += 1

        report = BuildingValidationReport(
            accepted_buildings=accepted,
            rejected_invalid=invalid,
            rejected_oversized=oversized,
            rejected_outside_area=outside,
            rejected_optional=optional,
        )
        warnings = list(dataset.warnings)
        unsafe_total = invalid + oversized + outside
        if unsafe_total:
            warnings.append(
                "Automatic building safety checks skipped "
                f"{unsafe_total:,} unsafe feature(s): {oversized:,} oversized, "
                f"{invalid:,} invalid, and {outside:,} outside the selected area."
            )

        original = dataset.building_statistics
        accepted_parts = sum(
            1
            for feature in retained
            if feature.feature_type is FeatureType.BUILDING
            and feature.tags.get("ovmg:is_part") == "yes"
        )
        statistics = BuildingStatistics(
            osm_buildings=original.osm_buildings,
            overture_buildings=original.overture_buildings,
            overture_parts=min(original.overture_parts, accepted_parts),
            merged_duplicates=original.merged_duplicates,
            final_building_features=accepted,
            real_height=original.real_height,
            real_levels=original.real_levels,
            building_parts=original.building_parts,
            inferred_height=original.inferred_height,
            default_height=original.default_height,
            overture_release=original.overture_release,
        )
        return (
            GeographicDataset(
                features=tuple(retained),
                building_statistics=statistics,
                attributions=dataset.attributions,
                warnings=tuple(dict.fromkeys(warnings)),
            ),
            report,
        )

    @staticmethod
    def _metrics(
        feature: GeoFeature,
        projector: LocalMetricProjector,
    ) -> tuple[float, float, float, float, float] | None:
        """Return area and projected bounds, rejecting non-finite coordinates."""
        points = [
            projector.project(point)
            for ring in feature.outer_rings
            for point in ring
        ]
        if len(points) < 4:
            return None
        coordinates = [(point.x, point.y) for point in points]
        if not all(isfinite(value) for coordinate in coordinates for value in coordinate):
            return None
        area_m2 = BuildingAnalyzer.feature_area_m2(projector, feature)
        if not isfinite(area_m2) or area_m2 <= 0.0:
            return None
        xs = [coordinate[0] for coordinate in coordinates]
        ys = [coordinate[1] for coordinate in coordinates]
        return area_m2, min(xs), min(ys), max(xs), max(ys)

    def _center_is_near_map(
        self,
        min_x: float,
        min_y: float,
        max_x: float,
        max_y: float,
        projector: LocalMetricProjector,
    ) -> bool:
        center_x = (min_x + max_x) * 0.5
        center_y = (min_y + max_y) * 0.5
        return (
            projector.min_x - self._CENTER_MARGIN_M
            <= center_x
            <= projector.max_x + self._CENTER_MARGIN_M
            and projector.min_y - self._CENTER_MARGIN_M
            <= center_y
            <= projector.max_y + self._CENTER_MARGIN_M
        )

    @staticmethod
    def _is_landmark(feature: GeoFeature) -> bool:
        tags = feature.tags
        man_made = (tags.get("man_made") or "").casefold()
        amenity = (tags.get("amenity") or "").casefold()
        historic = (tags.get("historic") or "").casefold()
        building = (tags.get("building") or "").casefold()
        return (
            man_made in {"tower", "minaret", "water_tower"}
            or amenity == "place_of_worship"
            or bool(historic)
            or building in {
                "mosque",
                "church",
                "cathedral",
                "synagogue",
                "temple",
                "tower",
            }
        )

    def _is_large_use(self, feature: GeoFeature) -> bool:
        tags = feature.tags
        values: Sequence[str] = (
            tags.get("building", ""),
            tags.get("building:part", ""),
            tags.get("amenity", ""),
            tags.get("landuse", ""),
            tags.get("ovmg:subtype", ""),
            tags.get("ovmg:class", ""),
        )
        return any(value.casefold() in self._LARGE_BUILDING_TAGS for value in values)
