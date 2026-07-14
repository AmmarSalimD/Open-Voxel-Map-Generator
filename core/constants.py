"""Shared constants for Open Voxel Map Generator."""

from __future__ import annotations

ADDON_ID = "open_voxel_map_generator"
ADDON_NAME = "Open Voxel Map Generator"
ADDON_VERSION = "2.0.4"
PANEL_CATEGORY = "Voxel Maps"
PROJECT_PREFIX = "OVMG"
ROOT_COLLECTION_PREFIX = "OVMG_Project"
GENERATED_COLLECTION_NAME = "OVMG_Generated"
METADATA_OBJECT_NAME = "OVMG_Metadata"
DEFAULT_OVERPASS_ENDPOINT = "https://overpass-api.de/api/interpreter"
DEFAULT_NOMINATIM_ENDPOINT = "https://nominatim.openstreetmap.org/search"
DEFAULT_USER_AGENT = "OpenVoxelMapGenerator/2.0.4"
EARTH_RADIUS_METERS = 6_378_137.0

# Resolve the complete package name for both legacy add-on installation and
# Blender's extension namespace, for example:
# bl_ext.user_default.open_voxel_map_generator
_PACKAGE_PARTS = __package__.split(".")
_ADDON_INDEX = _PACKAGE_PARTS.index(ADDON_ID)
ADDON_PACKAGE = ".".join(_PACKAGE_PARTS[: _ADDON_INDEX + 1])
