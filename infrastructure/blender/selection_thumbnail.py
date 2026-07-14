"""Crash-safe temporary thumbnail support for pending map selections.

The browser sends an encoded JPEG or PNG preview. This module deliberately stages
that preview as a normal temporary file first. Blender image/texture datablocks are
not created from an application timer, and no popup is invoked from a timer.
Instead, a custom UI preview is loaded only after the user explicitly presses the
Review button from a valid Blender UI context.
"""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
import uuid
from typing import Any

import bpy
import bpy.utils.previews

_PREVIEW_KEY = "pending_area"
_PREVIEW_COLLECTION_NAME = "OVMG_PENDING_AREA_PREVIEWS"
_preview_collections: dict[str, Any] = {}


class BlenderSelectionThumbnailService:
    """Stage and display one pending browser-map thumbnail safely."""

    @classmethod
    def stage(
        cls,
        settings: object,
        image_bytes: bytes | None,
        mime_type: str,
    ) -> str:
        """Write browser image bytes to a temporary file without touching bpy data.

        This method is safe to call from a Blender application timer because it
        performs filesystem operations only. It returns the staged file path, or an
        empty string when no thumbnail was supplied.
        """
        cls.remove(settings)
        if not image_bytes:
            return ""

        suffix = ".png" if mime_type == "image/png" else ".jpg"
        path = Path(tempfile.gettempdir()) / (
            f"ovmg_area_preview_{uuid.uuid4().hex}{suffix}"
        )
        path.write_bytes(image_bytes)
        settings.pending_preview_path = str(path)
        return str(path)

    @classmethod
    def prepare_for_ui(cls, settings: object) -> int:
        """Load the staged image as a custom preview in an explicit UI context.

        Returns a Blender icon id. A zero value means that no usable preview could
        be loaded; callers should still show numeric bounds and allow confirmation.
        """
        path = cls._path(settings)
        if path is None:
            return 0

        collection = cls._collection()
        try:
            if _PREVIEW_KEY in collection:
                collection.clear()
            preview = collection.load(_PREVIEW_KEY, str(path), "IMAGE", force_reload=True)
            return int(preview.icon_id)
        except (KeyError, OSError, RuntimeError, ValueError):
            return 0

    @classmethod
    def loaded_icon_id(cls) -> int:
        """Return the already loaded icon id without loading or mutating UI data."""
        collection = _preview_collections.get(_PREVIEW_COLLECTION_NAME)
        if collection is None or _PREVIEW_KEY not in collection:
            return 0
        try:
            return int(collection[_PREVIEW_KEY].icon_id)
        except (KeyError, ReferenceError, RuntimeError):
            return 0

    @classmethod
    def open_external(cls, settings: object) -> bool:
        """Open the staged preview in the operating system's default viewer."""
        path = cls._path(settings)
        if path is None:
            return False
        try:
            os.startfile(str(path))  # type: ignore[attr-defined]
            return True
        except (AttributeError, OSError):
            return False

    @classmethod
    def remove(cls, settings: object) -> None:
        """Remove staged preview resources when no dialog is using them."""
        collection = _preview_collections.get(_PREVIEW_COLLECTION_NAME)
        if collection is not None and _PREVIEW_KEY in collection:
            try:
                collection.clear()
            except RuntimeError:
                pass

        path = cls._path(settings)
        if path is not None:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        if hasattr(settings, "pending_preview_path"):
            settings.pending_preview_path = ""

    @classmethod
    def shutdown(cls) -> None:
        """Release all custom-preview collections during add-on unregistration."""
        for collection in list(_preview_collections.values()):
            try:
                bpy.utils.previews.remove(collection)
            except (ReferenceError, RuntimeError):
                pass
        _preview_collections.clear()

    @staticmethod
    def _path(settings: object) -> Path | None:
        raw = str(getattr(settings, "pending_preview_path", "")).strip()
        if not raw:
            return None
        path = Path(raw)
        return path if path.is_file() else None

    @staticmethod
    def _collection() -> Any:
        collection = _preview_collections.get(_PREVIEW_COLLECTION_NAME)
        if collection is None:
            collection = bpy.utils.previews.new()
            _preview_collections[_PREVIEW_COLLECTION_NAME] = collection
        return collection
