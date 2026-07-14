"""Domain-specific exceptions with user-facing messages."""

from __future__ import annotations


class OVMGError(RuntimeError):
    """Base exception for recoverable Open Voxel Map Generator failures."""


class ValidationError(OVMGError):
    """Raised when settings or geographic input are invalid."""


class NetworkAccessError(OVMGError):
    """Raised when Blender or the operating environment blocks networking."""


class RemoteServiceError(OVMGError):
    """Raised when a remote GIS service returns an invalid response."""


class GeocodingError(OVMGError):
    """Raised when a place name cannot be resolved to a bounding box."""


class DataLimitError(OVMGError):
    """Raised when requested voxel dimensions exceed configured safety limits."""


class DependencyError(OVMGError):
    """Raised when an optional packaged GIS dependency is unavailable."""
