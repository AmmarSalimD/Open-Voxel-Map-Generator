"""Stable naming utilities for Blender data blocks."""

from __future__ import annotations

import re
import unicodedata

from .constants import PROJECT_PREFIX, ROOT_COLLECTION_PREFIX

_INVALID_NAME_CHARS = re.compile(r"[^\w\-]+", flags=re.UNICODE)
_MULTI_UNDERSCORE = re.compile(r"_+")


def sanitize_name(value: str, fallback: str = "Map") -> str:
    """Return a compact Blender-safe identifier while preserving Unicode.

    Args:
        value: Raw user-facing name.
        fallback: Name used when the input is empty after normalization.

    Returns:
        A normalized identifier containing letters, numbers, underscores,
        and hyphens.
    """
    normalized = unicodedata.normalize("NFKC", value).strip()
    normalized = _INVALID_NAME_CHARS.sub("_", normalized)
    normalized = _MULTI_UNDERSCORE.sub("_", normalized).strip("_")
    return normalized or fallback


def project_collection_name(project_name: str) -> str:
    """Build the root collection name for a generated project."""
    return f"{ROOT_COLLECTION_PREFIX}_{sanitize_name(project_name)}"


def category_collection_name(category_name: str) -> str:
    """Build a stable category collection name."""
    return f"{PROJECT_PREFIX}_{sanitize_name(category_name)}"


def chunk_object_name(category_name: str, chunk_x: int, chunk_y: int) -> str:
    """Build a stable mesh object name for a category and XY chunk."""
    return (
        f"{PROJECT_PREFIX}_{sanitize_name(category_name)}_"
        f"C{chunk_x:+05d}_{chunk_y:+05d}"
    )


def label_metadata_name(project_name: str) -> str:
    """Build the Blender text-data name storing optional label metadata."""
    return f"{PROJECT_PREFIX}_Labels_{sanitize_name(project_name)}"


def building_metadata_name(project_name: str) -> str:
    """Build the Blender text-data name storing building accuracy metadata."""
    return f"{PROJECT_PREFIX}_BuildingAccuracy_{sanitize_name(project_name)}"


def building_corrections_name(project_name: str) -> str:
    """Build the Blender text-data name storing persistent building corrections."""
    return f"{PROJECT_PREFIX}_BuildingCorrections_{sanitize_name(project_name)}"
