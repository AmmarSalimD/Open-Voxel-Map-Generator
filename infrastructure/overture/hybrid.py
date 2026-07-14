"""Building-source composition and geometry-aware de-duplication."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from math import floor
from typing import Any

from ...core.exceptions import DependencyError
from ...domain.enums import BuildingSource, FeatureType, GeometryType
from ...domain.models import (
    BoundingBox,
    BuildingStatistics,
    GeoFeature,
    GeographicDataset,
)
from ...domain.ports import GeographicDataSource


class HybridGeographicDataSource(GeographicDataSource):
    """Combine OSM thematic data with the selected building source."""

    _BUCKET_DEGREES = 0.001

    def __init__(
        self,
        osm_source: GeographicDataSource,
        overture_source: GeographicDataSource | None,
        building_source: BuildingSource,
        use_building_parts: bool,
    ) -> None:
        self._osm_source = osm_source
        self._overture_source = overture_source
        self._building_source = building_source
        self._use_building_parts = use_building_parts

    def fetch_features(self, bounds: BoundingBox) -> GeographicDataset:
        """Fetch thematic OSM data and merge building geometry deterministically."""
        osm_dataset = self._osm_source.fetch_features(bounds)
        non_buildings = [
            feature
            for feature in osm_dataset.features
            if feature.feature_type is not FeatureType.BUILDING
        ]
        osm_buildings = [
            feature
            for feature in osm_dataset.features
            if feature.feature_type is FeatureType.BUILDING
            and (self._use_building_parts or feature.tags.get("ovmg:is_part") != "yes")
        ]

        if self._building_source is BuildingSource.OSM_ONLY:
            return GeographicDataset(
                features=tuple(non_buildings + osm_buildings),
                building_statistics=BuildingStatistics(
                    osm_buildings=len(osm_buildings),
                    final_building_features=len(osm_buildings),
                ),
                attributions=osm_dataset.attributions,
            )

        if self._overture_source is None:
            raise DependencyError(
                "Overture or Hybrid building mode requires the packaged Overture "
                "data source. Reinstall the complete OVMG package."
            )
        try:
            overture_dataset = self._overture_source.fetch_features(bounds)
        except DependencyError as exc:
            if self._building_source is BuildingSource.OVERTURE_ONLY:
                raise
            warning = (
                "Overture buildings were unavailable, so OVMG continued with "
                f"OpenStreetMap buildings only. {exc}"
            )
            return GeographicDataset(
                features=tuple(non_buildings + osm_buildings),
                building_statistics=BuildingStatistics(
                    osm_buildings=len(osm_buildings),
                    final_building_features=len(osm_buildings),
                ),
                attributions=osm_dataset.attributions,
                warnings=(warning,),
            )
        overture_buildings = list(overture_dataset.features)
        if not self._use_building_parts:
            overture_buildings = [
                feature
                for feature in overture_buildings
                if feature.tags.get("ovmg:is_part") != "yes"
            ]

        if self._building_source is BuildingSource.OVERTURE_ONLY:
            retained_points, point_duplicates = self._retain_uncovered_landmarks(
                osm_buildings,
                overture_buildings,
            )
            final_buildings = overture_buildings + retained_points
            duplicates = (
                len(
                    [
                        f
                        for f in osm_buildings
                        if f.geometry_type is not GeometryType.POINT
                    ]
                )
                + point_duplicates
            )
        else:
            additional_osm, duplicates = self._merge_osm_fallbacks(
                osm_buildings,
                overture_buildings,
            )
            final_buildings = overture_buildings + additional_osm

        attributions = tuple(
            dict.fromkeys(osm_dataset.attributions + overture_dataset.attributions)
        )
        overture_stats = overture_dataset.building_statistics
        return GeographicDataset(
            features=tuple(non_buildings + final_buildings),
            building_statistics=BuildingStatistics(
                osm_buildings=len(osm_buildings),
                overture_buildings=overture_stats.overture_buildings,
                overture_parts=(
                    overture_stats.overture_parts if self._use_building_parts else 0
                ),
                merged_duplicates=duplicates,
                final_building_features=len(final_buildings),
                overture_release=overture_stats.overture_release,
            ),
            attributions=attributions,
            warnings=tuple(
                dict.fromkeys(osm_dataset.warnings + overture_dataset.warnings)
            ),
        )

    def _merge_osm_fallbacks(
        self,
        osm_features: list[GeoFeature],
        overture_features: list[GeoFeature],
    ) -> tuple[list[GeoFeature], int]:
        """Keep OSM geometry not already represented by Overture."""
        source_id_index = self._overture_osm_source_index(overture_features)
        source_ids = set(source_id_index)
        polygon_index = self._build_polygon_index(overture_features)
        retained: list[GeoFeature] = []
        duplicates = 0

        for feature in osm_features:
            if feature.source_id in source_ids:
                index = source_id_index[feature.source_id]
                overture_features[index] = self._fuse_osm_attributes(
                    overture_features[index],
                    feature,
                )
                duplicates += 1
                continue
            if feature.geometry_type is GeometryType.POINT:
                if self._point_is_covered(feature, polygon_index):
                    duplicates += 1
                else:
                    retained.append(feature)
                continue
            matched_index = self._polygon_match_index(feature, polygon_index)
            if matched_index is not None:
                overture_features[matched_index] = self._fuse_osm_attributes(
                    overture_features[matched_index],
                    feature,
                )
                duplicates += 1
            else:
                retained.append(feature)
        return retained, duplicates

    @staticmethod
    def _overture_osm_source_index(features: list[GeoFeature]) -> dict[str, int]:
        """Map normalized OSM ids to their matching Overture feature."""
        result: dict[str, int] = {}
        for index, feature in enumerate(features):
            raw = feature.tags.get("ovmg:osm_ids", "")
            for source_id in raw.split("|"):
                if source_id:
                    result.setdefault(source_id, index)
        return result

    @staticmethod
    def _fuse_osm_attributes(
        overture: GeoFeature,
        osm: GeoFeature,
    ) -> GeoFeature:
        """Keep Overture geometry while restoring authoritative OSM semantics.

        Overture often supplies the more complete footprint, while its source
        lineage points back to an OSM feature carrying manually surveyed height,
        level, roof, material, colour, and landmark tags.  Dropping that duplicate
        also dropped those high-value architectural attributes.
        """
        tags = dict(overture.tags)
        architectural_keys = {
            "building",
            "building:levels",
            "building:min_level",
            "height",
            "min_height",
            "roof:shape",
            "roof:height",
            "roof:levels",
            "roof:direction",
            "roof:orientation",
            "roof:material",
            "roof:colour",
            "roof:color",
            "building:material",
            "building:colour",
            "building:color",
            "facade:material",
            "facade:colour",
            "facade:color",
            "amenity",
            "office",
            "tourism",
            "historic",
            "man_made",
            "name",
            "name:ar",
            "name:en",
        }
        for key in architectural_keys:
            value = osm.tags.get(key)
            if value not in (None, ""):
                tags[key] = value

        datasets = [
            value.strip()
            for value in tags.get("ovmg:source_datasets", "").split(",")
            if value.strip()
        ]
        if "OpenStreetMap" not in datasets:
            datasets.append("OpenStreetMap")
        tags["ovmg:source_datasets"] = ", ".join(datasets)
        tags["ovmg:attribute_fusion"] = "overture_geometry+osm_attributes"
        return GeoFeature(
            source_id=overture.source_id,
            feature_type=overture.feature_type,
            geometry_type=overture.geometry_type,
            tags=tags,
            points=overture.points,
            outer_rings=overture.outer_rings,
            inner_rings=overture.inner_rings,
        )

    def _retain_uncovered_landmarks(
        self,
        osm_features: list[GeoFeature],
        overture_features: list[GeoFeature],
    ) -> tuple[list[GeoFeature], int]:
        """Retain only OSM point landmarks not covered by an Overture footprint."""
        polygon_index = self._build_polygon_index(overture_features)
        retained: list[GeoFeature] = []
        duplicates = 0
        for feature in osm_features:
            if feature.geometry_type is not GeometryType.POINT:
                continue
            if self._point_is_covered(feature, polygon_index):
                duplicates += 1
            else:
                retained.append(feature)
        return retained, duplicates

    @staticmethod
    def _overture_osm_source_ids(features: Iterable[GeoFeature]) -> set[str]:
        source_ids: set[str] = set()
        for feature in features:
            raw = feature.tags.get("ovmg:osm_ids", "")
            source_ids.update(value for value in raw.split("|") if value)
        return source_ids

    def _build_polygon_index(
        self,
        features: Iterable[GeoFeature],
    ) -> tuple[list[Any], dict[tuple[int, int], set[int]], list[int]]:
        shapely = self._load_shapely()
        geometries: list[Any] = []
        feature_indices: list[int] = []
        buckets: dict[tuple[int, int], set[int]] = defaultdict(set)
        for feature_index, feature in enumerate(features):
            if feature.geometry_type is not GeometryType.POLYGON:
                continue
            if feature.tags.get("ovmg:is_part") == "yes":
                continue
            geometry = self._feature_geometry(feature, shapely)
            if geometry is None or geometry.is_empty:
                continue
            index = len(geometries)
            geometries.append(geometry)
            feature_indices.append(feature_index)
            for bucket in self._bbox_buckets(geometry.bounds):
                buckets[bucket].add(index)
        return geometries, dict(buckets), feature_indices

    def _polygon_has_match(
        self,
        feature: GeoFeature,
        index: tuple[list[Any], dict[tuple[int, int], set[int]], list[int]],
    ) -> bool:
        return self._polygon_match_index(feature, index) is not None

    def _polygon_match_index(
        self,
        feature: GeoFeature,
        index: tuple[list[Any], dict[tuple[int, int], set[int]], list[int]],
    ) -> int | None:
        """Return the best matching source-feature index for an OSM footprint."""
        shapely = self._load_shapely()
        geometry = self._feature_geometry(feature, shapely)
        if geometry is None or geometry.is_empty or geometry.area <= 0.0:
            return None
        geometries, buckets, feature_indices = index
        candidate_ids: set[int] = set()
        for bucket in self._bbox_buckets(geometry.bounds):
            candidate_ids.update(buckets.get(bucket, ()))
        best_match: int | None = None
        best_score = 0.0
        for candidate_id in candidate_ids:
            candidate = geometries[candidate_id]
            if not geometry.intersects(candidate):
                continue
            intersection = geometry.intersection(candidate).area
            if intersection <= 0.0:
                continue
            union = geometry.area + candidate.area - intersection
            iou = intersection / union if union > 0.0 else 0.0
            smaller_coverage = intersection / min(geometry.area, candidate.area)
            if iou >= 0.45 or smaller_coverage >= 0.78:
                score = max(iou, smaller_coverage * 0.9)
                if score > best_score:
                    best_score = score
                    best_match = feature_indices[candidate_id]
        return best_match

    def _point_is_covered(
        self,
        feature: GeoFeature,
        index: tuple[list[Any], dict[tuple[int, int], set[int]], list[int]],
    ) -> bool:
        if not feature.points:
            return False
        shapely = self._load_shapely()
        point = shapely.Point(
            feature.points[0].longitude,
            feature.points[0].latitude,
        )
        geometries, buckets, _feature_indices = index
        bucket = self._point_bucket(point.x, point.y)
        for candidate_id in buckets.get(bucket, ()):
            candidate = geometries[candidate_id]
            if candidate.covers(point):
                return True
        return False

    def _feature_geometry(self, feature: GeoFeature, shapely: Any) -> Any | None:
        polygons: list[Any] = []
        inner_geometries = [
            shapely.Polygon([(p.longitude, p.latitude) for p in ring])
            for ring in feature.inner_rings
            if len(ring) >= 4
        ]
        for outer in feature.outer_rings:
            if len(outer) < 4:
                continue
            shell = [(point.longitude, point.latitude) for point in outer]
            shell_polygon = shapely.Polygon(shell)
            holes: list[list[tuple[float, float]]] = []
            for inner in inner_geometries:
                if shell_polygon.covers(inner.representative_point()):
                    holes.append(list(inner.exterior.coords))
            polygon = shapely.Polygon(shell, holes)
            if not polygon.is_valid:
                polygon = shapely.make_valid(polygon)
            if polygon.is_empty:
                continue
            if polygon.geom_type == "Polygon":
                polygons.append(polygon)
            elif polygon.geom_type == "MultiPolygon":
                polygons.extend(polygon.geoms)
        if not polygons:
            return None
        return polygons[0] if len(polygons) == 1 else shapely.MultiPolygon(polygons)

    def _bbox_buckets(
        self,
        bounds: tuple[float, float, float, float],
    ) -> Iterable[tuple[int, int]]:
        min_x, min_y, max_x, max_y = bounds
        x0, y0 = self._point_bucket(min_x, min_y)
        x1, y1 = self._point_bucket(max_x, max_y)
        for x in range(x0, x1 + 1):
            for y in range(y0, y1 + 1):
                yield x, y

    def _point_bucket(self, longitude: float, latitude: float) -> tuple[int, int]:
        return (
            floor(longitude / self._BUCKET_DEGREES),
            floor(latitude / self._BUCKET_DEGREES),
        )

    @staticmethod
    def _load_shapely() -> Any:
        try:
            import shapely
        except ImportError as exc:
            raise DependencyError(
                "Shapely is required for Hybrid building de-duplication. "
                "Reinstall the complete OVMG package."
            ) from exc
        return shapely
