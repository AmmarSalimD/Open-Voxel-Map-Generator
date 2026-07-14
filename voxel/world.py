"""Sparse voxel world with category priority and chunk indexing."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable

from ..core.exceptions import DataLimitError
from ..domain.enums import FeatureType

VoxelCell = tuple[int, int, int]
ChunkKey = tuple[int, int]


class VoxelWorld:
    """Store occupied cells sparsely and prevent same-cell category overlap."""

    PRIORITY: dict[FeatureType, int] = {
        FeatureType.TERRAIN: 0,
        FeatureType.GREEN: 10,
        FeatureType.WATER: 20,
        FeatureType.ROAD: 30,
        FeatureType.BRIDGE: 40,
        FeatureType.TREE: 50,
        FeatureType.BUILDING: 60,
    }

    def __init__(self, chunk_size: int, max_cells: int) -> None:
        self.chunk_size = chunk_size
        self.max_cells = max_cells
        self._cells: dict[FeatureType, set[VoxelCell]] = {
            category: set() for category in FeatureType
        }
        self._owners: dict[VoxelCell, FeatureType] = {}

    def add(self, category: FeatureType, cell: VoxelCell) -> None:
        """Add one cell, replacing a lower-priority category at that location."""
        existing = self._owners.get(cell)
        if existing is category:
            return
        if existing is not None:
            if self.PRIORITY[existing] > self.PRIORITY[category]:
                return
            self._cells[existing].discard(cell)
        self._cells[category].add(cell)
        self._owners[cell] = category
        if len(self._owners) > self.max_cells:
            raise DataLimitError(
                f"Generated voxel count exceeded the configured limit of "
                f"{self.max_cells:,}. For a complete district, use Medium quality "
                "or increase Horizontal Voxel Size. For maximum detail, generate "
                "a smaller Bounding Box. Raising the limit can consume several "
                "gigabytes of RAM and is not recommended as the first solution."
            )

    def add_many(self, category: FeatureType, cells: Iterable[VoxelCell]) -> None:
        """Add multiple cells using the same overlap rules."""
        for cell in cells:
            self.add(category, cell)

    def cells(self, category: FeatureType) -> frozenset[VoxelCell]:
        """Return an immutable view of cells for a category."""
        return frozenset(self._cells[category])

    def mutable_cells(self, category: FeatureType) -> set[VoxelCell]:
        """Return the internal category set for performance-sensitive readers."""
        return self._cells[category]

    def category_counts(self) -> dict[FeatureType, int]:
        """Return voxel totals by semantic category."""
        return {category: len(cells) for category, cells in self._cells.items()}

    @property
    def voxel_count(self) -> int:
        """Return the number of occupied cells across all categories."""
        return len(self._owners)

    def is_occupied(self, cell: VoxelCell) -> bool:
        """Return whether any semantic category occupies the cell."""
        return cell in self._owners

    def category_at(self, cell: VoxelCell) -> FeatureType | None:
        """Return the semantic owner of a cell, or ``None`` when it is empty."""
        return self._owners.get(cell)

    def chunks_for(self, category: FeatureType) -> dict[ChunkKey, set[VoxelCell]]:
        """Group one category by XY chunk while retaining full Z coordinates."""
        chunks: dict[ChunkKey, set[VoxelCell]] = defaultdict(set)
        for cell in self._cells[category]:
            x, y, _ = cell
            chunks[(x // self.chunk_size, y // self.chunk_size)].add(cell)
        return dict(chunks)
