"""Abstract ports used to keep the application layer independent."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable

from .models import (
    BoundingBox,
    GeographicDataset,
    CurvedDetailPayload,
    LabelPayload,
    MeshPayload,
    ProjectStatistics,
    VoxelSettings,
)

ProgressCallback = Callable[[float, str], None]


class Geocoder(ABC):
    """Port for resolving human-readable place names."""

    @abstractmethod
    def resolve(self, area_name: str) -> BoundingBox:
        """Resolve a place name to a WGS84 bounding box."""


class GeographicDataSource(ABC):
    """Port for downloading and classifying geographic features."""

    @abstractmethod
    def fetch_features(self, bounds: BoundingBox) -> GeographicDataset:
        """Download normalized features inside the supplied bounds."""


class SceneRepository(ABC):
    """Port for creating and deleting generated Blender scene data."""

    @abstractmethod
    def replace_project(
        self,
        project_name: str,
        bounds: BoundingBox,
        settings: VoxelSettings,
        meshes: Iterable[MeshPayload],
        curved_details: Iterable[CurvedDetailPayload],
        labels: Iterable[LabelPayload],
        statistics: ProjectStatistics,
        progress: ProgressCallback,
    ) -> int:
        """Replace a generated project and return created object count."""

    @abstractmethod
    def delete_project(self, project_name: str) -> int:
        """Delete all generated data for a project and return object count."""
