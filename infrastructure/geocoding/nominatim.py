"""Nominatim geocoder adapter for OpenStreetMap place-name resolution."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from difflib import SequenceMatcher
from math import cos, pi
import re
from typing import Any

from ...core.constants import EARTH_RADIUS_METERS
from ...core.exceptions import GeocodingError, RemoteServiceError
from ...domain.models import BoundingBox
from ...domain.ports import Geocoder
from ..network.http_client import JsonHttpClient

_TOKEN_PATTERN = re.compile(r"[\w]+", re.UNICODE)


class NominatimGeocoder(Geocoder):
    """Resolve place names while avoiding tiny POI-style search results.

    Nominatim can return a building, road, or point before the intended
    neighborhood. The geocoder therefore requests multiple candidates, scores
    them by semantic type and geographic usefulness, and expands point-like
    results to a practical neighborhood-sized extent.
    """

    _cache: dict[str, BoundingBox] = {}
    _candidate_limit = 10
    _minimum_useful_span_meters = 750.0
    _fallback_radius_meters = 2_000.0

    _preferred_types: dict[str, float] = {
        "administrative": 420.0,
        "district": 410.0,
        "borough": 400.0,
        "suburb": 390.0,
        "quarter": 380.0,
        "neighbourhood": 370.0,
        "neighborhood": 370.0,
        "city": 330.0,
        "town": 320.0,
        "village": 300.0,
        "municipality": 300.0,
        "locality": 250.0,
        "residential": 230.0,
    }

    def __init__(self, endpoint: str, http_client: JsonHttpClient) -> None:
        self._endpoint = endpoint.rstrip("/")
        self._http_client = http_client

    def resolve(self, area_name: str) -> BoundingBox:
        """Resolve the best geographic candidate to a useful bounding box."""
        normalized_query = area_name.strip()
        cache_key = normalized_query.casefold()
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        payload = self._http_client.get_json(
            self._endpoint,
            query={
                "q": normalized_query,
                "format": "jsonv2",
                "limit": str(self._candidate_limit),
                "addressdetails": "1",
                "namedetails": "1",
                "extratags": "1",
                "accept-language": "en-US,en",
            },
        )
        if not isinstance(payload, list):
            raise RemoteServiceError(
                "Nominatim returned an unexpected response format."
            )
        candidates = [item for item in payload if isinstance(item, Mapping)]
        if not candidates:
            raise GeocodingError(
                f'No geographic result was found for "{normalized_query}".'
            )

        selected = max(
            candidates,
            key=lambda candidate: self._candidate_score(
                normalized_query,
                candidate,
            ),
        )
        bounds = self._bounds_from_candidate(selected)
        bounds = self._ensure_useful_extent(bounds, selected)
        bounds.validate()
        self._cache[cache_key] = bounds
        return bounds

    def _candidate_score(
        self,
        query: str,
        candidate: Mapping[str, Any],
    ) -> float:
        """Score one result for neighborhood or administrative map generation."""
        category = str(candidate.get("category", candidate.get("class", ""))).casefold()
        candidate_type = str(
            candidate.get("addresstype", candidate.get("type", ""))
        ).casefold()
        osm_type = str(candidate.get("osm_type", "")).casefold()

        name = self._candidate_name(candidate)
        target = query.split(",", 1)[0].strip()
        name_similarity = SequenceMatcher(
            None,
            self._normalize_text(target),
            self._normalize_text(name),
        ).ratio()

        score = name_similarity * 500.0
        score += self._preferred_types.get(candidate_type, 0.0)
        if category == "boundary":
            score += 360.0
        elif category == "place":
            score += 300.0
        elif category in {"highway", "building", "amenity", "shop"}:
            score -= 350.0
        if osm_type == "relation":
            score += 220.0
        elif osm_type == "node":
            score -= 40.0

        try:
            importance = float(candidate.get("importance", 0.0))
        except (TypeError, ValueError):
            importance = 0.0
        score += max(0.0, min(1.0, importance)) * 100.0

        try:
            place_rank = int(candidate.get("place_rank", 30))
        except (TypeError, ValueError):
            place_rank = 30
        if 12 <= place_rank <= 24:
            score += 120.0
        elif place_rank >= 28:
            score -= 100.0

        try:
            bounds = self._bounds_from_candidate(candidate)
            width_m, height_m = self._bounds_size_meters(bounds)
            smaller_span = min(width_m, height_m)
            area_square_km = (width_m * height_m) / 1_000_000.0
            if smaller_span >= self._minimum_useful_span_meters:
                score += 240.0
            elif smaller_span < 100.0:
                score -= 180.0
            if 0.25 <= area_square_km <= 2_500.0:
                score += 160.0
        except GeocodingError:
            score -= 120.0

        return score

    @staticmethod
    def _candidate_name(candidate: Mapping[str, Any]) -> str:
        direct_name = candidate.get("name")
        if isinstance(direct_name, str) and direct_name.strip():
            return direct_name
        namedetails = candidate.get("namedetails")
        if isinstance(namedetails, Mapping):
            for key in ("name", "name:en", "name:ar"):
                value = namedetails.get(key)
                if isinstance(value, str) and value.strip():
                    return value
        return str(candidate.get("display_name", "")).split(",", 1)[0]

    @staticmethod
    def _normalize_text(value: str) -> str:
        return " ".join(_TOKEN_PATTERN.findall(value.casefold()))

    @staticmethod
    def _bounds_from_candidate(candidate: Mapping[str, Any]) -> BoundingBox:
        raw_box = candidate.get("boundingbox")
        if isinstance(raw_box, Sequence) and not isinstance(raw_box, str):
            if len(raw_box) == 4:
                try:
                    return BoundingBox(
                        south=float(raw_box[0]),
                        north=float(raw_box[1]),
                        west=float(raw_box[2]),
                        east=float(raw_box[3]),
                    )
                except (TypeError, ValueError):
                    pass

        try:
            latitude = float(candidate["lat"])
            longitude = float(candidate["lon"])
        except (KeyError, TypeError, ValueError) as exc:
            raise GeocodingError(
                "The geocoding result contains neither valid bounds nor a center."
            ) from exc
        epsilon = 0.000001
        return BoundingBox(
            south=latitude - epsilon,
            north=latitude + epsilon,
            west=longitude - epsilon,
            east=longitude + epsilon,
        )

    def _ensure_useful_extent(
        self,
        bounds: BoundingBox,
        candidate: Mapping[str, Any],
    ) -> BoundingBox:
        """Expand point-like results to a usable neighborhood query extent."""
        width_m, height_m = self._bounds_size_meters(bounds)
        if (
            width_m >= self._minimum_useful_span_meters
            and height_m >= self._minimum_useful_span_meters
        ):
            return bounds

        try:
            latitude = float(candidate.get("lat", bounds.center.latitude))
            longitude = float(candidate.get("lon", bounds.center.longitude))
        except (TypeError, ValueError):
            latitude = bounds.center.latitude
            longitude = bounds.center.longitude

        latitude_delta = self._fallback_radius_meters / EARTH_RADIUS_METERS * 180.0 / pi
        cosine = max(0.01, abs(cos(latitude * pi / 180.0)))
        longitude_delta = latitude_delta / cosine
        return BoundingBox(
            south=max(-90.0, latitude - latitude_delta),
            north=min(90.0, latitude + latitude_delta),
            west=max(-180.0, longitude - longitude_delta),
            east=min(180.0, longitude + longitude_delta),
        )

    @staticmethod
    def _bounds_size_meters(bounds: BoundingBox) -> tuple[float, float]:
        center_latitude_radians = bounds.center.latitude * pi / 180.0
        latitude_span_radians = (bounds.north - bounds.south) * pi / 180.0
        longitude_span_radians = (bounds.east - bounds.west) * pi / 180.0
        height = EARTH_RADIUS_METERS * abs(latitude_span_radians)
        width = (
            EARTH_RADIUS_METERS
            * abs(longitude_span_radians)
            * max(0.01, abs(cos(center_latitude_radians)))
        )
        return width, height
