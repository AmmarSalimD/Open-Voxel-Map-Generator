"""Geographic area measurements and preflight load estimation."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from math import ceil, cos, pi

from ...core.constants import EARTH_RADIUS_METERS
from ...domain.models import BoundingBox


class AreaLoadLevel(str, Enum):
    """Coarse load categories shown before map generation."""

    SAFE = "SAFE"
    HEAVY = "HEAVY"
    TOO_LARGE = "TOO_LARGE"


@dataclass(frozen=True, slots=True)
class AreaMetrics:
    """Measured geographic dimensions and an estimated terrain-cell budget."""

    width_meters: float
    height_meters: float
    area_square_km: float
    estimated_surface_cells: int
    estimated_generation_cells: int
    load_ratio: float
    load_level: AreaLoadLevel

    @property
    def width_km(self) -> float:
        """Return east-west span in kilometers."""
        return self.width_meters / 1_000.0

    @property
    def height_km(self) -> float:
        """Return north-south span in kilometers."""
        return self.height_meters / 1_000.0


class AreaMetricsCalculator:
    """Calculate stable neighborhood-scale measurements in WGS84."""

    @staticmethod
    def calculate(
        bounds: BoundingBox,
        voxel_size: float,
        max_voxel_cells: int,
        generation_multiplier: float = 1.0,
    ) -> AreaMetrics:
        """Measure bounds and classify the estimated generation load.

        ``generation_multiplier`` reserves space for buildings, trees, roofs,
        and vertical layers that are not represented by the base XY grid.
        """
        bounds.validate()
        safe_voxel_size = max(0.01, float(voxel_size))
        safe_limit = max(1, int(max_voxel_cells))

        center_latitude = bounds.center.latitude * pi / 180.0
        latitude_span = abs(bounds.north - bounds.south) * pi / 180.0
        longitude_span = abs(bounds.east - bounds.west) * pi / 180.0
        height = EARTH_RADIUS_METERS * latitude_span
        width = (
            EARTH_RADIUS_METERS
            * longitude_span
            * max(0.01, abs(cos(center_latitude)))
        )
        width_cells = max(1, ceil(width / safe_voxel_size))
        height_cells = max(1, ceil(height / safe_voxel_size))
        surface_cells = width_cells * height_cells
        generation_cells = ceil(
            surface_cells * max(1.0, float(generation_multiplier))
        )
        ratio = generation_cells / safe_limit

        if ratio <= 0.45:
            level = AreaLoadLevel.SAFE
        elif ratio <= 0.85:
            level = AreaLoadLevel.HEAVY
        else:
            level = AreaLoadLevel.TOO_LARGE

        return AreaMetrics(
            width_meters=width,
            height_meters=height,
            area_square_km=(width * height) / 1_000_000.0,
            estimated_surface_cells=surface_cells,
            estimated_generation_cells=generation_cells,
            load_ratio=ratio,
            load_level=level,
        )


def quality_generation_multiplier(quality_name: str) -> float:
    """Return a conservative whole-map multiplier for preflight checks."""
    return {
        "LOW": 1.5,
        "MEDIUM": 2.5,
        "HIGH": 4.0,
    }.get(str(quality_name).upper(), 4.0)
