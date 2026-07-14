"""Overpass API adapter and OSM JSON parser."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from ...core.exceptions import RemoteServiceError
from ...domain.enums import FeatureType, GeometryType
from ...domain.models import (
    BoundingBox,
    BuildingStatistics,
    GeoFeature,
    GeographicDataset,
    GeoPoint,
)
from ...domain.ports import GeographicDataSource
from ..network.http_client import JsonHttpClient
from .classifier import OsmFeatureClassifier
from .query_builder import OverpassQueryBuilder


class OverpassDataSource(GeographicDataSource):
    """Download and normalize OSM features through an Overpass endpoint."""

    def __init__(
        self,
        endpoint: str,
        http_client: JsonHttpClient,
        classifier: OsmFeatureClassifier,
        timeout_seconds: int = 180,
        fallback_endpoints: tuple[str, ...] = (),
    ) -> None:
        self._endpoint = endpoint
        self._endpoints = tuple(
            dict.fromkeys(
                value.strip().rstrip("/")
                for value in (endpoint, *fallback_endpoints)
                if value.strip()
            )
        )
        self._http_client = http_client
        self._classifier = classifier
        self._timeout_seconds = timeout_seconds

    def fetch_features(self, bounds: BoundingBox) -> GeographicDataset:
        """Fetch a bounded OSM dataset and return classified domain features."""
        query = OverpassQueryBuilder.build(bounds, self._timeout_seconds)
        payload: Any | None = None
        selected_endpoint = self._endpoint.rstrip("/")
        failures: list[str] = []
        for endpoint in self._endpoints:
            try:
                payload = self._http_client.post_form_json(
                    endpoint,
                    form={"data": query},
                )
                selected_endpoint = endpoint
                break
            except RemoteServiceError as exc:
                failures.append(f"{endpoint}: {exc}")
        if payload is None:
            raise RemoteServiceError(
                "All configured OpenStreetMap data servers were unavailable. "
                "Please retry later or choose a smaller area. "
                + " | ".join(failures)
            )
        if not isinstance(payload, dict) or not isinstance(
            payload.get("elements"), list
        ):
            raise RemoteServiceError("Overpass returned an unexpected JSON structure.")

        features: list[GeoFeature] = []
        for element in payload["elements"]:
            if isinstance(element, dict):
                features.extend(self._parse_element(element))
        osm_buildings = sum(
            1
            for feature in features
            if feature.feature_type is FeatureType.BUILDING
            and feature.geometry_type is not GeometryType.POINT
        )
        warnings = ()
        if selected_endpoint != self._endpoint.rstrip("/"):
            warnings = (
                "The primary OpenStreetMap server timed out; OVMG completed the "
                "request through an automatic backup server.",
            )
        return GeographicDataset(
            features=tuple(features),
            building_statistics=BuildingStatistics(
                osm_buildings=osm_buildings,
                final_building_features=osm_buildings,
            ),
            attributions=("© OpenStreetMap contributors",),
            warnings=warnings,
        )

    def _parse_element(self, element: Mapping[str, Any]) -> list[GeoFeature]:
        tags = self._clean_tags(element.get("tags"))
        tags["ovmg:source"] = "osm"
        if "building:part" in tags:
            tags["ovmg:is_part"] = "yes"
        category = self._classifier.classify(tags)
        if category is None:
            return []

        element_type = str(element.get("type", "unknown"))
        element_id = str(element.get("id", "0"))
        source_id = f"{element_type}/{element_id}"

        if element_type == "node":
            point = self._point_from_mapping(element)
            if point is None:
                return []
            return [
                GeoFeature(
                    source_id=source_id,
                    feature_type=category,
                    geometry_type=GeometryType.POINT,
                    tags=tags,
                    points=(point,),
                )
            ]

        if element_type == "way":
            points = self._geometry_points(element.get("geometry"))
            return self._features_from_way(source_id, category, tags, points)

        if element_type == "relation":
            return self._features_from_relation(source_id, category, tags, element)

        return []

    def _features_from_way(
        self,
        source_id: str,
        category: FeatureType,
        tags: Mapping[str, str],
        points: tuple[GeoPoint, ...],
    ) -> list[GeoFeature]:
        if len(points) < 2:
            return []
        area_category = self._is_area_feature(
            category,
            tags,
            geometry_closed=points[0] == points[-1],
        )
        if area_category and len(points) >= 3:
            ring = self._ensure_closed(points)
            return [
                GeoFeature(
                    source_id=source_id,
                    feature_type=category,
                    geometry_type=GeometryType.POLYGON,
                    tags=tags,
                    outer_rings=(ring,),
                )
            ]
        return [
            GeoFeature(
                source_id=source_id,
                feature_type=category,
                geometry_type=GeometryType.LINE,
                tags=tags,
                points=points,
            )
        ]

    def _features_from_relation(
        self,
        source_id: str,
        category: FeatureType,
        tags: Mapping[str, str],
        element: Mapping[str, Any],
    ) -> list[GeoFeature]:
        members = element.get("members")
        if not isinstance(members, list):
            return []

        if self._is_area_feature(
            category,
            tags,
            geometry_closed=False,
        ):
            outer_paths: list[tuple[GeoPoint, ...]] = []
            inner_paths: list[tuple[GeoPoint, ...]] = []
            for member in members:
                if not isinstance(member, dict):
                    continue
                points = self._geometry_points(member.get("geometry"))
                if len(points) < 2:
                    continue
                if member.get("role") == "inner":
                    inner_paths.append(points)
                else:
                    outer_paths.append(points)
            outer = self._assemble_rings(outer_paths)
            inner = self._assemble_rings(inner_paths)
            if not outer:
                return []
            return [
                GeoFeature(
                    source_id=source_id,
                    feature_type=category,
                    geometry_type=GeometryType.POLYGON,
                    tags=tags,
                    outer_rings=tuple(outer),
                    inner_rings=tuple(inner),
                )
            ]

        features: list[GeoFeature] = []
        for index, member in enumerate(members):
            if not isinstance(member, dict):
                continue
            points = self._geometry_points(member.get("geometry"))
            if len(points) < 2:
                continue
            features.append(
                GeoFeature(
                    source_id=f"{source_id}/member/{index}",
                    feature_type=category,
                    geometry_type=GeometryType.LINE,
                    tags=tags,
                    points=points,
                )
            )
        return features

    @staticmethod
    def _clean_tags(raw_tags: Any) -> dict[str, str]:
        if not isinstance(raw_tags, dict):
            return {}
        return {str(key): str(value) for key, value in raw_tags.items()}

    @staticmethod
    def _geometry_points(raw_geometry: Any) -> tuple[GeoPoint, ...]:
        if not isinstance(raw_geometry, Iterable):
            return ()
        points: list[GeoPoint] = []
        for item in raw_geometry:
            if not isinstance(item, Mapping):
                continue
            point = OverpassDataSource._point_from_mapping(item)
            if point is not None:
                points.append(point)
        return tuple(points)

    @staticmethod
    def _point_from_mapping(item: Mapping[str, Any]) -> GeoPoint | None:
        try:
            latitude = float(item["lat"])
            longitude = float(item["lon"])
        except (KeyError, TypeError, ValueError):
            return None
        point = GeoPoint(longitude=longitude, latitude=latitude)
        point.validate()
        return point

    @classmethod
    def _assemble_rings(
        cls,
        paths: list[tuple[GeoPoint, ...]],
    ) -> list[tuple[GeoPoint, ...]]:
        """Join relation member paths into closed multipolygon rings."""
        remaining = [list(path) for path in paths if len(path) >= 2]
        rings: list[tuple[GeoPoint, ...]] = []

        while remaining:
            ring = remaining.pop(0)
            made_progress = True
            while not cls._points_match(ring[0], ring[-1]) and made_progress:
                made_progress = False
                for index, candidate in enumerate(remaining):
                    if cls._points_match(ring[-1], candidate[0]):
                        ring.extend(candidate[1:])
                    elif cls._points_match(ring[-1], candidate[-1]):
                        ring.extend(reversed(candidate[:-1]))
                    elif cls._points_match(ring[0], candidate[-1]):
                        ring = candidate[:-1] + ring
                    elif cls._points_match(ring[0], candidate[0]):
                        ring = list(reversed(candidate[1:])) + ring
                    else:
                        continue
                    remaining.pop(index)
                    made_progress = True
                    break

            if len(ring) >= 4 and cls._points_match(ring[0], ring[-1]):
                ring[-1] = ring[0]
                rings.append(tuple(ring))
        return rings

    @staticmethod
    def _points_match(first: GeoPoint, second: GeoPoint) -> bool:
        """Match relation endpoints with sub-centimeter coordinate tolerance."""
        tolerance = 1e-8
        return (
            abs(first.longitude - second.longitude) <= tolerance
            and abs(first.latitude - second.latitude) <= tolerance
        )

    @staticmethod
    def _is_area_feature(
        category: FeatureType,
        tags: Mapping[str, str],
        geometry_closed: bool,
    ) -> bool:
        """Determine whether an OSM feature represents an area or centerline."""
        if category in {FeatureType.BUILDING, FeatureType.GREEN}:
            return True
        if category is FeatureType.TERRAIN:
            return tags.get("landuse") in {
                "residential",
                "commercial",
                "retail",
                "mixed_use",
            }
        if category is not FeatureType.WATER:
            return False
        return (
            geometry_closed
            or tags.get("type") == "multipolygon"
            or tags.get("area") == "yes"
            or tags.get("natural") == "water"
            or tags.get("landuse") in {"reservoir", "basin"}
            or tags.get("waterway") in {"riverbank", "dock"}
        )

    @staticmethod
    def _ensure_closed(points: tuple[GeoPoint, ...]) -> tuple[GeoPoint, ...]:
        if points[0] == points[-1]:
            return points
        return points + (points[0],)
