"""Building height, roof, facade, and provenance analysis.

The analyzer never claims survey accuracy when source data is absent. It first
uses direct OSM/Overture attributes, then learns conservative type-specific
height medians from the selected area, and finally applies deterministic local
profiles. Every result includes provenance and an explicit confidence score.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from hashlib import blake2b
from math import floor
from statistics import median
import re

from ..domain.enums import (
    AccuracyConfidence,
    FacadeSource,
    FeatureType,
    FootprintSource,
    GeometryType,
    HeightSource,
    ModelStyle,
    RoofSource,
)
from ..domain.models import (
    BuildingAccuracyRecord,
    BuildingAccuracyStatistics,
    BuildingAnalysis,
    GeoFeature,
    GeoPoint,
    VoxelSettings,
)
from .projection import LocalMetricProjector
from .style_profiles import style_profile

_NUMBER_PATTERN = re.compile(r"[-+]?\d+(?:\.\d+)?")
_HEX_PATTERN = re.compile(r"^#?([0-9a-fA-F]{6})$")


class BuildingAnalyzer:
    """Resolve building attributes and expose their confidence transparently."""

    _PROFILE_COLORS: dict[str, str] = {
        "residential_plaster": "#c7b18d",
        "residential_brick": "#a46f4f",
        "commercial_glass": "#6e8790",
        "institutional_stone": "#d2c7ae",
        "industrial_concrete": "#8b8b83",
        "worship_stone": "#d8cfb4",
        "historic_brick": "#9c684a",
        "metal_tower": "#777b7d",
        "generic_plaster": "#bca989",
    }

    _MATERIAL_ALIASES: dict[str, str] = {
        "brick": "residential_brick",
        "bricks": "residential_brick",
        "masonry": "residential_brick",
        "stone": "institutional_stone",
        "limestone": "institutional_stone",
        "marble": "institutional_stone",
        "glass": "commercial_glass",
        "metal": "metal_tower",
        "steel": "metal_tower",
        "concrete": "industrial_concrete",
        "cement_block": "industrial_concrete",
        "plaster": "residential_plaster",
        "stucco": "residential_plaster",
    }

    def analyze(
        self,
        features: Sequence[GeoFeature],
        projector: LocalMetricProjector,
        settings: VoxelSettings,
    ) -> tuple[dict[str, BuildingAnalysis], tuple[BuildingAccuracyRecord, ...], BuildingAccuracyStatistics]:
        """Analyze all source buildings using local known-height context."""
        buildings = [
            feature
            for feature in features
            if feature.feature_type is FeatureType.BUILDING
        ]
        samples: dict[str, list[float]] = defaultdict(list)
        global_samples: list[float] = []
        for feature in buildings:
            sample = self._source_height(feature.tags, settings)
            if sample is None:
                continue
            height, _source = sample
            if 1.0 <= height <= 1000.0:
                samples[self._building_type(feature.tags)].append(height)
                global_samples.append(height)

        type_medians = {
            key: median(values)
            for key, values in samples.items()
            if values
        }
        global_median = median(global_samples) if global_samples else settings.default_building_height

        analyses: dict[str, BuildingAnalysis] = {}
        records: list[BuildingAccuracyRecord] = []
        for feature in buildings:
            area_m2 = self.feature_area_m2(projector, feature)
            analysis = self._analyze_one(
                feature,
                area_m2,
                type_medians,
                global_median,
                settings,
            )
            analyses[feature.source_id] = analysis
            center = self.feature_center(feature)
            if center is None:
                continue
            metric = projector.project(center)
            outer_rings, inner_rings = self._project_footprint(feature, projector)
            records.append(
                BuildingAccuracyRecord(
                    source_id=feature.source_id,
                    position=(metric.x, metric.y, analysis.minimum_height_m),
                    height_m=analysis.height_m,
                    minimum_height_m=analysis.minimum_height_m,
                    height_source=analysis.height_source,
                    footprint_source=analysis.footprint_source,
                    roof_shape=analysis.roof_shape,
                    roof_source=analysis.roof_source,
                    facade_source=analysis.facade_source,
                    facade_profile=analysis.facade_profile,
                    building_type=analysis.building_type,
                    confidence=analysis.confidence,
                    confidence_score=analysis.confidence_score,
                    source_datasets=analysis.source_datasets,
                    roof_height_m=analysis.roof_height_m,
                    facade_color=analysis.facade_color,
                    roof_color=analysis.roof_color,
                    outer_rings_xy=outer_rings,
                    inner_rings_xy=inner_rings,
                    is_building_part=feature.tags.get("ovmg:is_part") == "yes",
                    parent_source_id=feature.tags.get("ovmg:building_id", ""),
                )
            )
        return analyses, tuple(records), self.summarize(records)


    @staticmethod
    def _project_footprint(
        feature: GeoFeature,
        projector: LocalMetricProjector,
    ) -> tuple[
        tuple[tuple[tuple[float, float], ...], ...],
        tuple[tuple[tuple[float, float], ...], ...],
    ]:
        """Project source footprint rings into editable Blender-local meters."""
        if feature.geometry_type is GeometryType.POINT and feature.points:
            center = projector.project(feature.points[0])
            radius = max(2.0, projector.voxel_size * 1.5)
            ring = (
                (center.x - radius, center.y - radius),
                (center.x + radius, center.y - radius),
                (center.x + radius, center.y + radius),
                (center.x - radius, center.y + radius),
                (center.x - radius, center.y - radius),
            )
            return (ring,), ()

        def project_ring(ring: Sequence[GeoPoint]) -> tuple[tuple[float, float], ...]:
            coordinates = tuple(
                (projector.project(point).x, projector.project(point).y)
                for point in ring
            )
            if len(coordinates) >= 3 and coordinates[0] != coordinates[-1]:
                coordinates = (*coordinates, coordinates[0])
            return coordinates

        outer = tuple(
            projected
            for ring in feature.outer_rings
            if len(ring) >= 3
            for projected in (project_ring(ring),)
            if len(projected) >= 4
        )
        inner = tuple(
            projected
            for ring in feature.inner_rings
            if len(ring) >= 3
            for projected in (project_ring(ring),)
            if len(projected) >= 4
        )
        return outer, inner

    def _analyze_one(
        self,
        feature: GeoFeature,
        area_m2: float,
        type_medians: Mapping[str, float],
        global_median: float,
        settings: VoxelSettings,
    ) -> BuildingAnalysis:
        tags = feature.tags
        building_type = self._building_type(tags)
        source_height = self._source_height(tags, settings)
        if source_height is not None:
            height, height_source = source_height
        elif settings.infer_missing_heights:
            height = self._infer_height(
                feature,
                building_type,
                area_m2,
                type_medians,
                global_median,
                settings,
            )
            height_source = (
                HeightSource.BUILDING_PART
                if tags.get("ovmg:is_part") == "yes"
                else HeightSource.INFERRED
            )
        else:
            height = settings.default_building_height
            height_source = (
                HeightSource.BUILDING_PART
                if tags.get("ovmg:is_part") == "yes"
                else HeightSource.DEFAULT
            )

        minimum_height = self._number(tags, "min_height")
        if minimum_height is None:
            minimum_floor = self._number(tags, "building:min_level") or 0.0
            minimum_height = max(0.0, minimum_floor * self._level_height(building_type, settings))

        profile = style_profile(settings.model_style)
        should_quantize_height = (
            profile.height_quantization_m > 0.0
            # Visual styles may simplify geometry, but must never rewrite a
            # surveyed height or a height derived from mapped floor counts.
            and source_height is None
        )
        if should_quantize_height:
            step = profile.height_quantization_m
            height = max(step, round(height / step) * step)
            minimum_height = max(0.0, round(minimum_height / step) * step)

        roof_shape, roof_source = self._roof_shape(tags, building_type, settings)
        roof_height = self._roof_height(
            tags,
            roof_shape,
            area_m2,
            height,
            settings,
        )
        facade_profile, facade_source, facade_color = self._facade(
            tags,
            building_type,
            settings,
            feature.source_id,
        )
        roof_color = self._normalize_color(
            tags.get("roof:colour") or tags.get("roof:color") or ""
        ) or self._darken_hex(facade_color)
        footprint_source = self._footprint_source(feature)
        score = self._confidence_score(
            height_source,
            footprint_source,
            roof_source,
            facade_source,
        )
        confidence = self._confidence(score)
        return BuildingAnalysis(
            source_id=feature.source_id,
            height_m=max(settings.vertical_step, height),
            minimum_height_m=minimum_height,
            height_source=height_source,
            footprint_source=footprint_source,
            roof_shape=roof_shape,
            roof_height_m=roof_height,
            roof_source=roof_source,
            facade_source=facade_source,
            facade_profile=facade_profile,
            facade_color=facade_color,
            roof_color=roof_color,
            building_type=building_type,
            confidence=confidence,
            confidence_score=score,
            source_datasets=tags.get("ovmg:source_datasets", ""),
        )

    def _source_height(
        self,
        tags: Mapping[str, str],
        settings: VoxelSettings,
    ) -> tuple[float, HeightSource] | None:
        is_part = tags.get("ovmg:is_part") == "yes"
        explicit = self._number(tags, "height")
        if explicit is not None and explicit > 0.0:
            return explicit, HeightSource.BUILDING_PART if is_part else HeightSource.REAL_HEIGHT
        levels = self._number(tags, "building:levels")
        if levels is None:
            levels = self._number(tags, "levels")
        if levels is not None and levels > 0.0:
            building_type = self._building_type(tags)
            roof_levels = max(0.0, self._number(tags, "roof:levels") or 0.0)
            height = levels * self._level_height(building_type, settings)
            if roof_levels:
                height += roof_levels * settings.level_height * 0.65
            return height, HeightSource.BUILDING_PART if is_part else HeightSource.REAL_LEVELS
        return None

    def _infer_height(
        self,
        feature: GeoFeature,
        building_type: str,
        area_m2: float,
        type_medians: Mapping[str, float],
        global_median: float,
        settings: VoxelSettings,
    ) -> float:
        tags = feature.tags
        unit = self._stable_unit(feature.source_id)
        man_made = tags.get("man_made", "").casefold()
        historic = tags.get("historic", "").casefold()
        amenity = tags.get("amenity", "").casefold()
        if man_made == "minaret":
            return 32.0 + unit * 14.0
        if man_made in {"tower", "water_tower"} or historic == "tower":
            return 24.0 + unit * 24.0
        if historic in {"monument", "memorial"}:
            return 10.0 + unit * 14.0
        if building_type == "worship":
            return 12.0 + unit * 8.0
        if amenity in {"hospital", "university", "college"}:
            return 12.0 + unit * 10.0

        if building_type in type_medians:
            baseline = type_medians[building_type]
        else:
            levels = self._profile_levels(building_type, area_m2, unit)
            baseline = levels * self._level_height(building_type, settings)
            if building_type == "generic" and global_median > 0.0:
                baseline = 0.65 * baseline + 0.35 * global_median
        area_factor = 1.0
        if area_m2 >= 4000.0:
            area_factor = 1.18
        elif area_m2 >= 1500.0:
            area_factor = 1.10
        elif area_m2 < 80.0:
            area_factor = 0.88
        variation = 0.94 + unit * 0.12
        return max(settings.level_height, baseline * area_factor * variation)

    @staticmethod
    def _profile_levels(building_type: str, area_m2: float, unit: float) -> int:
        ranges = {
            "house": (1, 3),
            "residential": (2, 4),
            "apartments": (3, 7),
            "commercial": (2, 6),
            "office": (3, 8),
            "hotel": (3, 8),
            "institutional": (2, 5),
            "industrial": (1, 3),
            "warehouse": (1, 2),
            "garage": (1, 1),
            "worship": (2, 4),
            "historic": (2, 5),
            "tower": (7, 14),
            "generic": (2, 4),
        }
        minimum, maximum = ranges.get(building_type, ranges["generic"])
        if area_m2 >= 2500.0 and building_type not in {"industrial", "warehouse", "worship"}:
            maximum += 2
        elif area_m2 >= 1000.0 and building_type not in {"warehouse"}:
            maximum += 1
        return min(maximum, minimum + floor(unit * (maximum - minimum + 1)))

    def _roof_shape(
        self,
        tags: Mapping[str, str],
        building_type: str,
        settings: VoxelSettings,
    ) -> tuple[str, RoofSource]:
        profile = style_profile(settings.model_style)
        if not settings.use_roof_shapes or not profile.allow_roof_shapes:
            return "flat", RoofSource.FLAT_DEFAULT
        tagged = (tags.get("roof:shape") or "").casefold().strip()
        if tagged and tagged not in {"yes", "no", "unknown"}:
            return tagged, RoofSource.SOURCE_TAG
        if self._number(tags, "roof:height") is not None or self._number(tags, "roof:levels") is not None:
            return "gabled", RoofSource.SOURCE_HEIGHT
        if settings.model_style in {ModelStyle.REAL, ModelStyle.ARCHITECTURAL_MODEL}:
            if building_type == "worship":
                return "dome", RoofSource.INFERRED
            if building_type in {"industrial", "warehouse"}:
                return "gabled", RoofSource.INFERRED
        if settings.model_style is ModelStyle.LOW_POLY and building_type in {"industrial", "warehouse"}:
            return "gabled", RoofSource.INFERRED
        return "flat", RoofSource.FLAT_DEFAULT

    def _roof_height(
        self,
        tags: Mapping[str, str],
        roof_shape: str,
        area_m2: float,
        total_height: float,
        settings: VoxelSettings,
    ) -> float:
        if roof_shape in {"", "flat", "none"}:
            return 0.0
        explicit = self._number(tags, "roof:height")
        if explicit is not None and explicit > 0.0:
            return min(explicit, max(0.0, total_height - settings.vertical_step))
        levels = self._number(tags, "roof:levels")
        if levels is not None and levels > 0.0:
            estimate = levels * settings.level_height
        else:
            span = max(settings.voxel_size, area_m2 ** 0.5)
            estimate = span * (0.34 if roof_shape in {"dome", "onion", "cone"} else 0.18)
        return min(max(settings.vertical_step, estimate), max(0.0, total_height * 0.38))

    def _facade(
        self,
        tags: Mapping[str, str],
        building_type: str,
        settings: VoxelSettings,
        source_id: str = "",
    ) -> tuple[str, FacadeSource, str]:
        material = (
            tags.get("facade:material")
            or tags.get("building:material")
            or tags.get("material")
            or ""
        ).casefold().strip()
        color = self._normalize_color(
            tags.get("facade:colour")
            or tags.get("facade:color")
            or tags.get("building:colour")
            or tags.get("building:color")
            or ""
        )
        profile = self._MATERIAL_ALIASES.get(material) or self._profile_for_type(building_type)
        if settings.use_source_facade_hints and material:
            source = FacadeSource.SOURCE_MATERIAL
        elif settings.use_source_facade_hints and color:
            source = FacadeSource.SOURCE_COLOR
        elif settings.strict_real_facades:
            # Strict mode never presents invented facade rhythm or colour as
            # source-derived reality.  Keep a neutral, visibly unverified shell.
            source = FacadeSource.NO_SOURCE_DATA
        elif settings.material_style.value == "REALISTIC":
            source = FacadeSource.PROCEDURAL_PROFILE
        else:
            source = FacadeSource.SIMPLE_CATEGORY
        if color:
            resolved_color = color
        elif settings.strict_real_facades:
            resolved_color = "#9b9b96"
        elif settings.material_style.value == "REALISTIC":
            resolved_color = self._procedural_facade_color(
                self._PROFILE_COLORS[profile],
                source_id,
            )
        else:
            resolved_color = self._PROFILE_COLORS[profile]
        return profile, source, resolved_color

    @staticmethod
    def _procedural_facade_color(base: str, source_id: str) -> str:
        """Return a stable, restrained palette variation for untagged facades."""
        digest = blake2b(source_id.encode("utf-8"), digest_size=2).digest()
        value_shift = (-10, -5, 0, 5, 10)[digest[0] % 5]
        warmth_shift = (-4, 0, 4)[digest[1] % 3]
        raw = base.lstrip("#")
        red = min(255, max(0, int(raw[0:2], 16) + value_shift + warmth_shift))
        green = min(255, max(0, int(raw[2:4], 16) + value_shift))
        blue = min(255, max(0, int(raw[4:6], 16) + value_shift - warmth_shift))
        return f"#{red:02x}{green:02x}{blue:02x}"

    @staticmethod
    def _profile_for_type(building_type: str) -> str:
        return {
            "house": "residential_plaster",
            "residential": "residential_plaster",
            "apartments": "residential_brick",
            "commercial": "commercial_glass",
            "office": "commercial_glass",
            "hotel": "commercial_glass",
            "institutional": "institutional_stone",
            "industrial": "industrial_concrete",
            "warehouse": "industrial_concrete",
            "garage": "industrial_concrete",
            "worship": "worship_stone",
            "historic": "historic_brick",
            "tower": "metal_tower",
            "generic": "generic_plaster",
        }.get(building_type, "generic_plaster")

    @staticmethod
    def _building_type(tags: Mapping[str, str]) -> str:
        building = (tags.get("building") or tags.get("building:part") or "").casefold()
        amenity = (tags.get("amenity") or "").casefold()
        tourism = (tags.get("tourism") or "").casefold()
        man_made = (tags.get("man_made") or "").casefold()
        historic = (tags.get("historic") or "").casefold()
        if man_made in {"tower", "minaret", "water_tower"} or building in {
            "tower", "skyscraper"
        }:
            return "tower"
        if historic:
            return "historic"
        if amenity == "place_of_worship" or building in {
            "mosque", "church", "cathedral", "synagogue", "temple"
        }:
            return "worship"
        if amenity in {
            "school", "college", "university", "hospital", "clinic", "police",
            "fire_station", "townhall", "courthouse", "library", "community_centre",
        }:
            return "institutional"
        if tourism == "hotel" or building == "hotel":
            return "hotel"
        if building in {"house", "detached", "semidetached_house", "terrace"}:
            return "house"
        if building in {"apartments", "dormitory"}:
            return "apartments"
        if building in {"residential", "bungalow"}:
            return "residential"
        if building in {"commercial", "retail", "supermarket"}:
            return "commercial"
        if building in {"office", "civic", "government"} or tags.get("office"):
            return "office"
        if building in {"industrial", "factory"}:
            return "industrial"
        if building in {"warehouse", "hangar"}:
            return "warehouse"
        if building in {"garage", "garages", "shed", "carport"}:
            return "garage"
        return "generic"

    @staticmethod
    def _level_height(building_type: str, settings: VoxelSettings) -> float:
        base = settings.level_height
        return {
            "commercial": max(base, 3.5),
            "office": max(base, 3.35),
            "hotel": max(base, 3.2),
            "institutional": max(base, 3.6),
            "industrial": max(base, 4.2),
            "warehouse": max(base, 4.8),
            "worship": max(base, 4.0),
            "garage": max(base, 2.7),
        }.get(building_type, base)

    @staticmethod
    def _footprint_source(feature: GeoFeature) -> FootprintSource:
        if feature.geometry_type is GeometryType.POINT:
            return FootprintSource.LANDMARK_PROXY
        if feature.tags.get("ovmg:approximate") == "yes":
            return FootprintSource.APPROXIMATE
        source = feature.tags.get("ovmg:source", "").casefold()
        datasets = feature.tags.get("ovmg:source_datasets", "").casefold()
        if source == "osm" or feature.source_id.startswith(("way/", "relation/", "node/")):
            return FootprintSource.OSM_SURVEYED
        if source == "overture" or feature.source_id.startswith("overture/"):
            if "openstreetmap" in datasets or "esri" in datasets:
                return FootprintSource.OVERTURE_CONFLATED
            if any(token in datasets for token in ("microsoft", "google", "open buildings", "ml")):
                return FootprintSource.MACHINE_EXTRACTED
            return FootprintSource.OVERTURE_CONFLATED
        return FootprintSource.UNKNOWN

    @staticmethod
    def _confidence_score(
        height: HeightSource,
        footprint: FootprintSource,
        roof: RoofSource,
        facade: FacadeSource,
    ) -> float:
        height_score = {
            HeightSource.REAL_HEIGHT: 1.0,
            HeightSource.BUILDING_PART: 0.92,
            HeightSource.REAL_LEVELS: 0.82,
            HeightSource.INFERRED: 0.45,
            HeightSource.DEFAULT: 0.18,
        }[height]
        footprint_score = {
            FootprintSource.OSM_SURVEYED: 0.92,
            FootprintSource.OVERTURE_CONFLATED: 0.86,
            FootprintSource.MACHINE_EXTRACTED: 0.62,
            FootprintSource.LANDMARK_PROXY: 0.28,
            FootprintSource.APPROXIMATE: 0.15,
            FootprintSource.UNKNOWN: 0.35,
        }[footprint]
        roof_score = {
            RoofSource.SOURCE_TAG: 1.0,
            RoofSource.SOURCE_HEIGHT: 0.76,
            RoofSource.INFERRED: 0.45,
            RoofSource.FLAT_DEFAULT: 0.30,
        }[roof]
        facade_score = {
            FacadeSource.SOURCE_MATERIAL: 1.0,
            FacadeSource.SOURCE_COLOR: 0.82,
            FacadeSource.NO_SOURCE_DATA: 0.0,
            FacadeSource.PROCEDURAL_PROFILE: 0.45,
            FacadeSource.SIMPLE_CATEGORY: 0.20,
        }[facade]
        return round(
            0.42 * height_score
            + 0.30 * footprint_score
            + 0.14 * roof_score
            + 0.14 * facade_score,
            3,
        )

    @staticmethod
    def _confidence(score: float) -> AccuracyConfidence:
        if score >= 0.80:
            return AccuracyConfidence.HIGH
        if score >= 0.60:
            return AccuracyConfidence.MEDIUM
        if score >= 0.40:
            return AccuracyConfidence.LOW
        return AccuracyConfidence.VERY_LOW

    @staticmethod
    def summarize(records: Sequence[BuildingAccuracyRecord]) -> BuildingAccuracyStatistics:
        """Aggregate inspector records into a compact coverage summary."""
        confidence = defaultdict(int)
        source_facades = unavailable_facades = procedural_facades = tagged_roofs = inferred_roofs = 0
        footprint = defaultdict(int)
        for record in records:
            confidence[record.confidence] += 1
            if record.facade_source in {FacadeSource.SOURCE_MATERIAL, FacadeSource.SOURCE_COLOR}:
                source_facades += 1
            elif record.facade_source is FacadeSource.NO_SOURCE_DATA:
                unavailable_facades += 1
            else:
                procedural_facades += 1
            if record.roof_source in {RoofSource.SOURCE_TAG, RoofSource.SOURCE_HEIGHT}:
                tagged_roofs += 1
            else:
                inferred_roofs += 1
            footprint[record.footprint_source] += 1
        return BuildingAccuracyStatistics(
            high_confidence=confidence[AccuracyConfidence.HIGH],
            medium_confidence=confidence[AccuracyConfidence.MEDIUM],
            low_confidence=confidence[AccuracyConfidence.LOW],
            very_low_confidence=confidence[AccuracyConfidence.VERY_LOW],
            source_facades=source_facades,
            unavailable_facades=unavailable_facades,
            procedural_facades=procedural_facades,
            tagged_roofs=tagged_roofs,
            inferred_roofs=inferred_roofs,
            surveyed_footprints=footprint[FootprintSource.OSM_SURVEYED],
            conflated_footprints=footprint[FootprintSource.OVERTURE_CONFLATED],
            machine_footprints=footprint[FootprintSource.MACHINE_EXTRACTED],
        )

    @staticmethod
    def feature_center(feature: GeoFeature) -> GeoPoint | None:
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
    def feature_area_m2(projector: LocalMetricProjector, feature: GeoFeature) -> float:
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

        return max(
            0.0,
            sum(ring_area(ring) for ring in feature.outer_rings)
            - sum(ring_area(ring) for ring in feature.inner_rings),
        )

    @staticmethod
    def _number(tags: Mapping[str, str], key: str) -> float | None:
        value = tags.get(key)
        if value is None:
            return None
        match = _NUMBER_PATTERN.search(str(value).replace(",", "."))
        if match is None:
            return None
        try:
            return float(match.group(0))
        except ValueError:
            return None

    @staticmethod
    def _stable_unit(source_id: str) -> float:
        seed = int.from_bytes(blake2b(source_id.encode("utf-8"), digest_size=4).digest(), "big")
        return seed / 0xFFFFFFFF

    @staticmethod
    def _normalize_color(value: str) -> str:
        value = value.strip()
        match = _HEX_PATTERN.match(value)
        if match:
            return f"#{match.group(1).lower()}"
        names = {
            "white": "#e8e4dc",
            "beige": "#c7b18d",
            "cream": "#ded2ae",
            "brown": "#8f684b",
            "red": "#a75c4a",
            "grey": "#8b8b83",
            "gray": "#8b8b83",
            "black": "#333333",
            "blue": "#6e8790",
            "green": "#78906d",
            "yellow": "#c6ad63",
        }
        return names.get(value.casefold(), "")

    @staticmethod
    def _darken_hex(value: str) -> str:
        if not value.startswith("#") or len(value) != 7:
            return "#6f6252"
        red = int(value[1:3], 16)
        green = int(value[3:5], 16)
        blue = int(value[5:7], 16)
        return f"#{int(red * 0.68):02x}{int(green * 0.68):02x}{int(blue * 0.68):02x}"
