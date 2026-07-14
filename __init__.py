"""Open Voxel Map Generator Blender extension entry point."""

from __future__ import annotations


def register() -> None:
    """Register the extension classes and scene properties."""
    from .presentation.registration import register_addon

    register_addon()


def unregister() -> None:
    """Unregister the extension classes and scene properties."""
    from .presentation.registration import unregister_addon

    unregister_addon()
