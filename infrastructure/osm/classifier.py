"""OpenStreetMap tag classification rules."""

from __future__ import annotations

from collections.abc import Mapping

from ...domain.enums import FeatureType


class OsmFeatureClassifier:
    """Classify OSM tags into semantic categories used by OVMG."""

    _GREEN_LANDUSE = {
        "grass",
        "forest",
        "meadow",
        "recreation_ground",
        "village_green",
    }
    _GREEN_LEISURE = {"park", "garden", "recreation_ground"}
    _GREEN_NATURAL = {"wood", "scrub", "grassland"}
    _WATER_LANDUSE = {"reservoir", "basin"}
    _URBAN_LANDUSE = {"residential", "commercial", "retail", "mixed_use"}
    _INSTITUTIONAL_AMENITIES = {
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
        "community_centre",
        "marketplace",
    }
    _LANDMARK_TOURISM = {"museum", "hotel", "attraction"}

    def classify(self, tags: Mapping[str, str]) -> FeatureType | None:
        """Return the highest-priority category for a tag mapping.

        ``FeatureType.TERRAIN`` is also used as an input-only urban-zone marker
        for residential recovery. Those source polygons are not rendered as a
        second terrain layer.
        """
        if tags.get("natural") == "tree":
            return FeatureType.TREE
        if (
            ("building" in tags and tags.get("building") != "no")
            or ("building:part" in tags and tags.get("building:part") != "no")
            or tags.get("type") == "building"
            or tags.get("amenity") == "place_of_worship"
            or tags.get("amenity") in self._INSTITUTIONAL_AMENITIES
            or tags.get("tourism") in self._LANDMARK_TOURISM
            or tags.get("office") == "government"
            or tags.get("man_made") in {"tower", "minaret", "water_tower"}
            or tags.get("historic")
            in {"monument", "memorial", "castle", "fort", "tower", "ruins"}
        ):
            return FeatureType.BUILDING
        if tags.get("bridge") not in {None, "no"} or tags.get("man_made") == "bridge":
            return FeatureType.BRIDGE
        if "highway" in tags:
            return FeatureType.ROAD
        if (
            tags.get("natural") == "water"
            or "waterway" in tags
            or tags.get("water") == "river"
            or tags.get("landuse") in self._WATER_LANDUSE
        ):
            return FeatureType.WATER
        if (
            tags.get("landuse") in self._GREEN_LANDUSE
            or tags.get("leisure") in self._GREEN_LEISURE
            or tags.get("natural") in self._GREEN_NATURAL
        ):
            return FeatureType.GREEN
        if tags.get("landuse") in self._URBAN_LANDUSE:
            return FeatureType.TERRAIN
        return None
