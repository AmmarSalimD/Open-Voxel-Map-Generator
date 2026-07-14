"""Overture Maps building and building-part data source.

The adapter uses the official ``overturemaps`` Python client bundled as wheels
with the Windows Blender extension. Geometry is normalized into the same domain
model used by the OpenStreetMap adapter, keeping the application layer source
agnostic.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
import re
from typing import Any

from ...core.exceptions import DependencyError, RemoteServiceError
from .runtime import probe_overture_runtime
from ...domain.enums import FeatureType, GeometryType
from ...domain.models import (
    BoundingBox,
    BuildingStatistics,
    GeoFeature,
    GeographicDataset,
    GeoPoint,
)
from ...domain.ports import GeographicDataSource

_OSM_SHORT_RECORD_PATTERN = re.compile(r"^([nwr])(\d+)(?:@.*)?$")
_OSM_LONG_RECORD_PATTERN = re.compile(
    r"^(node|way|relation)[/:](\d+)(?:@.*)?$",
    re.IGNORECASE,
)


class OvertureBuildingsDataSource(GeographicDataSource):
    """Read Overture building footprints within a WGS84 bounding box."""

    def __init__(
        self,
        include_parts: bool,
        release: str = "",
        connect_timeout: int = 15,
        request_timeout: int = 180,
        max_features: int = 250_000,
    ) -> None:
        self._include_parts = include_parts
        self._release = release.strip()
        self._connect_timeout = connect_timeout
        self._request_timeout = request_timeout
        self._max_features = max_features

    def fetch_features(self, bounds: BoundingBox) -> GeographicDataset:
        """Download building and optional building-part records from Overture."""
        overturemaps, shapely = self._load_dependencies()
        try:
            release = self._release or overturemaps.core.get_latest_release()
            features = self._read_type(
                overturemaps,
                shapely,
                bounds,
                "building",
                release,
            )
            parts: list[GeoFeature] = []
            if self._include_parts:
                parts = self._read_type(
                    overturemaps,
                    shapely,
                    bounds,
                    "building_part",
                    release,
                )
        except DependencyError:
            raise
        except Exception as exc:
            raise RemoteServiceError(
                "Overture building download failed. Check Online Access, the "
                "internet connection, and the selected area. "
                f"Technical detail: {exc}"
            ) from exc

        combined = features + parts
        if len(combined) > self._max_features:
            raise RemoteServiceError(
                f"Overture returned {len(combined):,} building features, above "
                f"the configured safety limit of {self._max_features:,}. "
                "Use a smaller area or increase the Overture feature limit in "
                "the add-on preferences."
            )
        return GeographicDataset(
            features=tuple(combined),
            building_statistics=BuildingStatistics(
                overture_buildings=len(features),
                overture_parts=len(parts),
                final_building_features=len(combined),
                overture_release=release,
            ),
            attributions=(
                "© Overture Maps Foundation",
                "© OpenStreetMap contributors",
            ),
        )

    def _read_type(
        self,
        overturemaps: Any,
        shapely: Any,
        bounds: BoundingBox,
        overture_type: str,
        release: str,
    ) -> list[GeoFeature]:
        """Stream one Overture type and normalize records as domain features."""
        reader = overturemaps.record_batch_reader(
            overture_type,
            bbox=bounds.as_overture(),
            release=release,
            connect_timeout=self._connect_timeout,
            request_timeout=self._request_timeout,
            stac=True,
        )
        if reader is None:
            return []

        features: list[GeoFeature] = []
        for batch in reader:
            if batch.num_rows == 0:
                continue
            for row in batch.to_pylist():
                feature = self._row_to_feature(row, overture_type, shapely)
                if feature is not None:
                    features.append(feature)
                    if len(features) > self._max_features:
                        return features
        return features

    def _row_to_feature(
        self,
        row: Mapping[str, Any],
        overture_type: str,
        shapely: Any,
    ) -> GeoFeature | None:
        if row.get("is_underground") is True:
            return None
        geometry_bytes = row.get("geometry")
        if not geometry_bytes:
            return None
        try:
            geometry = shapely.from_wkb(bytes(geometry_bytes))
            if geometry is None or geometry.is_empty:
                return None
            if not geometry.is_valid:
                geometry = shapely.make_valid(geometry)
            polygons = list(self._iter_polygons(geometry))
        except Exception:
            return None
        if not polygons:
            return None

        outer_rings: list[tuple[GeoPoint, ...]] = []
        inner_rings: list[tuple[GeoPoint, ...]] = []
        for polygon in polygons:
            outer = self._coordinates_to_ring(polygon.exterior.coords)
            if len(outer) >= 4:
                outer_rings.append(outer)
            for interior in polygon.interiors:
                ring = self._coordinates_to_ring(interior.coords)
                if len(ring) >= 4:
                    inner_rings.append(ring)
        if not outer_rings:
            return None

        feature_id = str(row.get("id") or "unknown")
        tags = self._build_tags(row, overture_type)
        return GeoFeature(
            source_id=f"overture/{overture_type}/{feature_id}",
            feature_type=FeatureType.BUILDING,
            geometry_type=GeometryType.POLYGON,
            tags=tags,
            outer_rings=tuple(outer_rings),
            inner_rings=tuple(inner_rings),
        )

    @staticmethod
    def _iter_polygons(geometry: Any) -> Iterable[Any]:
        geom_type = getattr(geometry, "geom_type", "")
        if geom_type == "Polygon":
            yield geometry
            return
        if geom_type == "MultiPolygon":
            yield from geometry.geoms
            return
        if geom_type == "GeometryCollection":
            for child in geometry.geoms:
                yield from OvertureBuildingsDataSource._iter_polygons(child)

    @staticmethod
    def _coordinates_to_ring(coordinates: Iterable[Any]) -> tuple[GeoPoint, ...]:
        points: list[GeoPoint] = []
        for coordinate in coordinates:
            if len(coordinate) < 2:
                continue
            point = GeoPoint(float(coordinate[0]), float(coordinate[1]))
            try:
                point.validate()
            except Exception:
                continue
            points.append(point)
        if points and points[0] != points[-1]:
            points.append(points[0])
        return tuple(points)

    def _build_tags(
        self,
        row: Mapping[str, Any],
        overture_type: str,
    ) -> dict[str, str]:
        tags: dict[str, str] = {
            "ovmg:source": "overture",
            "ovmg:overture_type": overture_type,
            "building": str(row.get("subtype") or row.get("class") or "yes"),
        }
        if overture_type == "building_part":
            tags["building:part"] = "yes"
            tags["ovmg:is_part"] = "yes"
            if row.get("building_id") is not None:
                tags["ovmg:building_id"] = str(row["building_id"])
        elif row.get("has_parts") is True:
            tags["ovmg:has_parts"] = "yes"

        self._copy_number(row, tags, "height", "height")
        self._copy_number(row, tags, "num_floors", "building:levels")
        self._copy_number(row, tags, "min_height", "min_height")
        self._copy_number(row, tags, "min_floor", "building:min_level")
        self._copy_number(row, tags, "roof_height", "roof:height")
        self._copy_number(row, tags, "roof_direction", "roof:direction")
        self._copy_text(row, tags, "roof_shape", "roof:shape")
        self._copy_text(row, tags, "roof_orientation", "roof:orientation")
        self._copy_text(row, tags, "facade_color", "building:colour")
        self._copy_text(row, tags, "facade_material", "building:material")
        self._copy_text(row, tags, "roof_color", "roof:colour")
        self._copy_text(row, tags, "roof_material", "roof:material")
        self._copy_text(row, tags, "subtype", "ovmg:subtype")
        self._copy_text(row, tags, "class", "ovmg:class")

        name = self._primary_name(row.get("names"))
        if name:
            tags["name"] = name

        normalized_ids: list[str] = []
        datasets: list[str] = []
        sources = row.get("sources") or []
        if isinstance(sources, list):
            for source in sources:
                if not isinstance(source, Mapping):
                    continue
                dataset = source.get("dataset")
                if dataset:
                    datasets.append(str(dataset))
                record_id = source.get("record_id")
                normalized = self._normalize_osm_record_id(record_id)
                if normalized:
                    normalized_ids.append(normalized)
        if datasets:
            tags["ovmg:source_datasets"] = "|".join(sorted(set(datasets)))
        if normalized_ids:
            tags["ovmg:osm_ids"] = "|".join(sorted(set(normalized_ids)))
        return tags

    @staticmethod
    def _copy_number(
        row: Mapping[str, Any],
        tags: dict[str, str],
        source_key: str,
        target_key: str,
    ) -> None:
        value = row.get(source_key)
        if value is None:
            return
        try:
            tags[target_key] = f"{float(value):g}"
        except (TypeError, ValueError):
            return

    @staticmethod
    def _copy_text(
        row: Mapping[str, Any],
        tags: dict[str, str],
        source_key: str,
        target_key: str,
    ) -> None:
        value = row.get(source_key)
        if value not in {None, ""}:
            tags[target_key] = str(value)

    @staticmethod
    def _primary_name(raw_names: Any) -> str:
        if not isinstance(raw_names, Mapping):
            return ""
        primary = raw_names.get("primary")
        if isinstance(primary, str):
            return primary
        if isinstance(primary, Mapping):
            for key in ("ar", "en", "local"):
                value = primary.get(key)
                if isinstance(value, str) and value:
                    return value
            for value in primary.values():
                if isinstance(value, str) and value:
                    return value
        common = raw_names.get("common")
        if isinstance(common, Mapping):
            for value in common.values():
                if isinstance(value, str) and value:
                    return value
        return ""

    @staticmethod
    def _normalize_osm_record_id(record_id: Any) -> str:
        if not record_id:
            return ""
        text = str(record_id).strip()
        short_match = _OSM_SHORT_RECORD_PATTERN.match(text)
        if short_match is not None:
            prefix, identifier = short_match.groups()
            element_type = {"n": "node", "w": "way", "r": "relation"}[prefix]
            return f"{element_type}/{identifier}"
        long_match = _OSM_LONG_RECORD_PATTERN.match(text)
        if long_match is None:
            return ""
        element_type, identifier = long_match.groups()
        return f"{element_type.casefold()}/{identifier}"

    @staticmethod
    def _load_dependencies() -> tuple[Any, Any]:
        status = probe_overture_runtime()
        if not status.available:
            raise DependencyError(
                "The packaged Overture runtime could not be loaded. "
                f"{status.summary} Open Advanced Options and copy Runtime "
                "Diagnostics for the exact module path and error."
            )
        return status.overturemaps, status.shapely
