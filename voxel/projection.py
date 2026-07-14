"""Local metric projection and voxel-grid coordinate conversion."""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil, cos, floor, pi

from ..core.constants import EARTH_RADIUS_METERS
from ..domain.models import BoundingBox, GeoPoint


@dataclass(frozen=True, slots=True)
class MetricPoint:
    """A local Cartesian point measured in meters."""

    x: float
    y: float


class LocalMetricProjector:
    """Project small WGS84 areas to an equirectangular local meter system.

    The projection is centered on the requested bounding box and is intended
    for neighborhood/city-scale voxel generation rather than surveying.
    """

    def __init__(
        self,
        bounds: BoundingBox,
        voxel_size: float,
        vertical_step: float = 1.0,
    ) -> None:
        self.bounds = bounds
        self.voxel_size = voxel_size
        self.vertical_step = vertical_step
        center = bounds.center
        self._longitude_origin = center.longitude
        self._latitude_origin = center.latitude
        self._cos_latitude = cos(self._latitude_origin * pi / 180.0)

        south_west = self.project(GeoPoint(bounds.west, bounds.south))
        north_east = self.project(GeoPoint(bounds.east, bounds.north))
        self.min_x = min(south_west.x, north_east.x)
        self.max_x = max(south_west.x, north_east.x)
        self.min_y = min(south_west.y, north_east.y)
        self.max_y = max(south_west.y, north_east.y)
        self.width_cells = max(1, ceil((self.max_x - self.min_x) / voxel_size))
        self.height_cells = max(1, ceil((self.max_y - self.min_y) / voxel_size))

    def project(self, point: GeoPoint) -> MetricPoint:
        """Project a geographic point into the local meter coordinate system."""
        longitude_delta = (point.longitude - self._longitude_origin) * pi / 180.0
        latitude_delta = (point.latitude - self._latitude_origin) * pi / 180.0
        return MetricPoint(
            x=EARTH_RADIUS_METERS * longitude_delta * self._cos_latitude,
            y=EARTH_RADIUS_METERS * latitude_delta,
        )

    def unproject(self, point: MetricPoint) -> GeoPoint:
        """Convert a local metric point back to WGS84 coordinates."""
        longitude = self._longitude_origin + (
            point.x / (EARTH_RADIUS_METERS * max(0.01, self._cos_latitude))
        ) * 180.0 / pi
        latitude = self._latitude_origin + (
            point.y / EARTH_RADIUS_METERS
        ) * 180.0 / pi
        return GeoPoint(longitude=longitude, latitude=latitude)

    def grid_window(
        self,
        start_x: int,
        start_y: int,
        width_cells: int,
        height_cells: int,
    ) -> "LocalMetricProjector":
        """Return an aligned sub-projector sharing this map's world origin."""
        if start_x < 0 or start_y < 0:
            raise ValueError("Grid window start coordinates cannot be negative.")
        if width_cells <= 0 or height_cells <= 0:
            raise ValueError("Grid window dimensions must be positive.")
        if start_x + width_cells > self.width_cells:
            raise ValueError("Grid window exceeds the projector width.")
        if start_y + height_cells > self.height_cells:
            raise ValueError("Grid window exceeds the projector height.")

        projector = self.__class__.__new__(self.__class__)
        projector.voxel_size = self.voxel_size
        projector.vertical_step = self.vertical_step
        projector._longitude_origin = self._longitude_origin
        projector._latitude_origin = self._latitude_origin
        projector._cos_latitude = self._cos_latitude
        projector.min_x = self.min_x + start_x * self.voxel_size
        projector.min_y = self.min_y + start_y * self.voxel_size
        projector.max_x = projector.min_x + width_cells * self.voxel_size
        projector.max_y = projector.min_y + height_cells * self.voxel_size
        projector.width_cells = width_cells
        projector.height_cells = height_cells
        south_west = self.unproject(MetricPoint(projector.min_x, projector.min_y))
        north_east = self.unproject(MetricPoint(projector.max_x, projector.max_y))
        projector.bounds = BoundingBox(
            south=max(self.bounds.south, south_west.latitude),
            west=max(self.bounds.west, south_west.longitude),
            north=min(self.bounds.north, north_east.latitude),
            east=min(self.bounds.east, north_east.longitude),
        )
        return projector

    def grid_float(self, point: GeoPoint) -> tuple[float, float]:
        """Return continuous XY grid coordinates for rasterization."""
        metric = self.project(point)
        return (
            (metric.x - self.min_x) / self.voxel_size,
            (metric.y - self.min_y) / self.voxel_size,
        )

    def grid_cell(self, point: GeoPoint) -> tuple[int, int]:
        """Return the containing integer XY voxel cell."""
        x, y = self.grid_float(point)
        return floor(x), floor(y)

    def contains_cell(self, x: int, y: int) -> bool:
        """Return whether an XY cell is inside the requested grid."""
        return 0 <= x < self.width_cells and 0 <= y < self.height_cells

    def grid_vertex_to_world(
        self,
        x: int,
        y: int,
        z: int,
    ) -> tuple[float, float, float]:
        """Convert an integer voxel boundary to Blender-space meters."""
        return (
            self.min_x + x * self.voxel_size,
            self.min_y + y * self.voxel_size,
            z * self.vertical_step,
        )
