"""Greedy face meshing for sparse voxel chunks."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

from ..core.naming import chunk_object_name
from ..domain.enums import FeatureType
from ..domain.models import MeshPayload
from ..domain.ports import ProgressCallback
from .projection import LocalMetricProjector
from .vertical_layout import SemanticVerticalLayout
from .world import VoxelCell, VoxelWorld

GridVertex = tuple[int, int, int]
MaskCell = tuple[int, int]


@dataclass(frozen=True, slots=True)
class _FacePlane:
    """A face orientation and integer boundary slice."""

    axis: str
    sign: int
    slice_coordinate: int


class GreedyChunkMesher:
    """Merge adjacent exposed voxel faces into larger quads per XY chunk."""

    _SURFACE_CATEGORIES = {
        FeatureType.TERRAIN,
        FeatureType.ROAD,
        FeatureType.WATER,
        FeatureType.GREEN,
    }

    _DIRECTIONS = (
        ("X", 1, (1, 0, 0)),
        ("X", -1, (-1, 0, 0)),
        ("Y", 1, (0, 1, 0)),
        ("Y", -1, (0, -1, 0)),
        ("Z", 1, (0, 0, 1)),
        ("Z", -1, (0, 0, -1)),
    )

    def build(
        self,
        world: VoxelWorld,
        projector: LocalMetricProjector,
        progress: ProgressCallback,
        project_name: str = "",
    ) -> list[MeshPayload]:
        """Create one optimized mesh payload per category and non-empty chunk."""
        chunk_jobs: list[tuple[FeatureType, int, int, set[VoxelCell]]] = []
        for category in FeatureType:
            for (chunk_x, chunk_y), cells in sorted(world.chunks_for(category).items()):
                if cells:
                    chunk_jobs.append((category, chunk_x, chunk_y, cells))

        payloads: list[MeshPayload] = []
        for index, (category, chunk_x, chunk_y, chunk_cells) in enumerate(chunk_jobs):
            if category in self._SURFACE_CATEGORIES:
                vertices, faces = self._mesh_surface_chunk(
                    category,
                    chunk_cells,
                    world,
                    projector,
                )
            else:
                vertices, faces = self._mesh_chunk(
                    category,
                    chunk_cells,
                    world,
                    projector,
                )
            if faces:
                payloads.append(
                    MeshPayload(
                        name=chunk_object_name(
                            (
                                f"{project_name}_{category.value}"
                                if project_name
                                else category.value
                            ),
                            chunk_x,
                            chunk_y,
                        ),
                        category=category,
                        chunk_x=chunk_x,
                        chunk_y=chunk_y,
                        vertices=vertices,
                        faces=faces,
                    )
                )
            ratio = (index + 1) / max(1, len(chunk_jobs))
            progress(ratio, f"Meshing {category.value} chunk {chunk_x}, {chunk_y}")
        return payloads

    def _mesh_surface_chunk(
        self,
        category: FeatureType,
        chunk_cells: set[VoxelCell],
        world: VoxelWorld,
        projector: LocalMetricProjector,
    ) -> tuple[list[tuple[float, float, float]], list[tuple[int, int, int, int]]]:
        """Mesh thin surface layers with visible curb and river-bank steps."""
        surface_cells = {(x, y) for x, y, z in chunk_cells if z == 0}
        if not surface_cells:
            return [], []

        lower, upper = SemanticVerticalLayout.cell_interval(
            category,
            0,
            projector.voxel_size,
            projector.vertical_step,
        )
        vertices: list[tuple[float, float, float]] = []
        faces: list[tuple[int, int, int, int]] = []
        vertex_indices: dict[tuple[float, float, float], int] = {}

        def add_quad(
            quad: tuple[
                tuple[float, float, float],
                tuple[float, float, float],
                tuple[float, float, float],
                tuple[float, float, float],
            ],
        ) -> None:
            face: list[int] = []
            for vertex in quad:
                index = vertex_indices.get(vertex)
                if index is None:
                    index = len(vertices)
                    vertices.append(vertex)
                    vertex_indices[vertex] = index
                face.append(index)
            faces.append(tuple(face))

        top_cells = {
            (x, y) for x, y in surface_cells if not world.is_occupied((x, y, 1))
        }
        for x, y, width, height in self._greedy_rectangles(top_cells):
            x0, y0 = self._world_xy(projector, x, y)
            x1, y1 = self._world_xy(projector, x + width, y + height)
            add_quad(
                (
                    (x0, y0, upper),
                    (x1, y0, upper),
                    (x1, y1, upper),
                    (x0, y1, upper),
                )
            )

        for x, y, width, height in self._greedy_rectangles(surface_cells):
            x0, y0 = self._world_xy(projector, x, y)
            x1, y1 = self._world_xy(projector, x + width, y + height)
            add_quad(
                (
                    (x0, y0, lower),
                    (x0, y1, lower),
                    (x1, y1, lower),
                    (x1, y0, lower),
                )
            )

        side_segments: dict[
            tuple[str, int, int, float, float],
            set[int],
        ] = defaultdict(set)
        directions = (
            ("X", 1, 1, 0),
            ("X", -1, -1, 0),
            ("Y", 1, 0, 1),
            ("Y", -1, 0, -1),
        )
        for x, y in surface_cells:
            for axis, sign, delta_x, delta_y in directions:
                neighbor = world.category_at((x + delta_x, y + delta_y, 0))
                exposed = self._upper_exposed_interval(
                    category,
                    neighbor,
                    projector.voxel_size,
                    projector.vertical_step,
                )
                if exposed is None:
                    continue
                exposed_lower, exposed_upper = exposed
                if axis == "X":
                    slice_coordinate = x + 1 if sign > 0 else x
                    run_coordinate = y
                else:
                    slice_coordinate = y + 1 if sign > 0 else y
                    run_coordinate = x
                key = (
                    axis,
                    sign,
                    slice_coordinate,
                    round(exposed_lower, 6),
                    round(exposed_upper, 6),
                )
                side_segments[key].add(run_coordinate)

        for key, coordinates in sorted(side_segments.items()):
            axis, sign, slice_coordinate, side_lower, side_upper = key
            for run_start, run_length in self._contiguous_runs(coordinates):
                add_quad(
                    self._surface_side_quad(
                        projector,
                        axis,
                        sign,
                        slice_coordinate,
                        run_start,
                        run_length,
                        side_lower,
                        side_upper,
                    )
                )
        return vertices, faces

    @staticmethod
    def _world_xy(
        projector: LocalMetricProjector,
        x: int,
        y: int,
    ) -> tuple[float, float]:
        world_x, world_y, _ = projector.grid_vertex_to_world(x, y, 0)
        return world_x, world_y

    @staticmethod
    def _upper_exposed_interval(
        category: FeatureType,
        neighbor: FeatureType | None,
        horizontal_voxel_size: float,
        vertical_step: float,
    ) -> tuple[float, float] | None:
        lower, upper = SemanticVerticalLayout.cell_interval(
            category,
            0,
            horizontal_voxel_size,
            vertical_step,
        )
        if neighbor is None:
            return lower, upper
        neighbor_lower, neighbor_upper = SemanticVerticalLayout.cell_interval(
            neighbor,
            0,
            horizontal_voxel_size,
            vertical_step,
        )
        del neighbor_lower
        if upper <= neighbor_upper + 1e-9:
            return None
        exposed_lower = max(lower, neighbor_upper)
        if upper <= exposed_lower + 1e-9:
            return None
        return exposed_lower, upper

    @staticmethod
    def _contiguous_runs(coordinates: set[int]) -> Iterable[tuple[int, int]]:
        ordered = sorted(coordinates)
        if not ordered:
            return
        start = previous = ordered[0]
        for coordinate in ordered[1:]:
            if coordinate == previous + 1:
                previous = coordinate
                continue
            yield start, previous - start + 1
            start = previous = coordinate
        yield start, previous - start + 1

    @staticmethod
    def _surface_side_quad(
        projector: LocalMetricProjector,
        axis: str,
        sign: int,
        slice_coordinate: int,
        run_start: int,
        run_length: int,
        lower: float,
        upper: float,
    ) -> tuple[
        tuple[float, float, float],
        tuple[float, float, float],
        tuple[float, float, float],
        tuple[float, float, float],
    ]:
        if axis == "X":
            world_x, y0 = GreedyChunkMesher._world_xy(
                projector,
                slice_coordinate,
                run_start,
            )
            _, y1 = GreedyChunkMesher._world_xy(
                projector,
                slice_coordinate,
                run_start + run_length,
            )
            if sign > 0:
                return (
                    (world_x, y0, lower),
                    (world_x, y1, lower),
                    (world_x, y1, upper),
                    (world_x, y0, upper),
                )
            return (
                (world_x, y0, lower),
                (world_x, y0, upper),
                (world_x, y1, upper),
                (world_x, y1, lower),
            )

        x0, world_y = GreedyChunkMesher._world_xy(
            projector,
            run_start,
            slice_coordinate,
        )
        x1, _ = GreedyChunkMesher._world_xy(
            projector,
            run_start + run_length,
            slice_coordinate,
        )
        if sign > 0:
            return (
                (x0, world_y, lower),
                (x0, world_y, upper),
                (x1, world_y, upper),
                (x1, world_y, lower),
            )
        return (
            (x0, world_y, lower),
            (x1, world_y, lower),
            (x1, world_y, upper),
            (x0, world_y, upper),
        )

    def _mesh_chunk(
        self,
        category: FeatureType,
        chunk_cells: set[VoxelCell],
        world: VoxelWorld,
        projector: LocalMetricProjector,
    ) -> tuple[list[tuple[float, float, float]], list[tuple[int, int, int, int]]]:
        masks: dict[_FacePlane, set[MaskCell]] = defaultdict(set)
        for x, y, z in chunk_cells:
            for axis, sign, delta in self._DIRECTIONS:
                neighbor = (x + delta[0], y + delta[1], z + delta[2])
                if world.is_occupied(neighbor):
                    continue
                plane, mask_cell = self._face_to_plane(axis, sign, x, y, z)
                masks[plane].add(mask_cell)

        grid_vertices: list[GridVertex] = []
        faces: list[tuple[int, int, int, int]] = []
        vertex_indices: dict[GridVertex, int] = {}

        for plane in sorted(
            masks,
            key=lambda item: (item.axis, item.sign, item.slice_coordinate),
        ):
            for u, v, width, height in self._greedy_rectangles(masks[plane]):
                quad = self._quad_vertices(plane, u, v, width, height)
                face_indices: list[int] = []
                for vertex in quad:
                    index = vertex_indices.get(vertex)
                    if index is None:
                        index = len(grid_vertices)
                        grid_vertices.append(vertex)
                        vertex_indices[vertex] = index
                    face_indices.append(index)
                faces.append(tuple(face_indices))

        vertices = []
        for x, y, z in grid_vertices:
            world_x, world_y, _ = projector.grid_vertex_to_world(x, y, z)
            world_z = SemanticVerticalLayout.boundary_height(
                category,
                z,
                projector.voxel_size,
                projector.vertical_step,
            )
            vertices.append((world_x, world_y, world_z))
        return vertices, faces

    @staticmethod
    def _face_to_plane(
        axis: str,
        sign: int,
        x: int,
        y: int,
        z: int,
    ) -> tuple[_FacePlane, MaskCell]:
        if axis == "X":
            slice_coordinate = x + 1 if sign > 0 else x
            return _FacePlane(axis, sign, slice_coordinate), (y, z)
        if axis == "Y":
            slice_coordinate = y + 1 if sign > 0 else y
            return _FacePlane(axis, sign, slice_coordinate), (x, z)
        slice_coordinate = z + 1 if sign > 0 else z
        return _FacePlane(axis, sign, slice_coordinate), (x, y)

    @staticmethod
    def _greedy_rectangles(
        mask: set[MaskCell],
    ) -> Iterable[tuple[int, int, int, int]]:
        remaining = set(mask)
        while remaining:
            start_u, start_v = min(remaining, key=lambda item: (item[1], item[0]))
            width = 1
            while (start_u + width, start_v) in remaining:
                width += 1

            height = 1
            while all(
                (start_u + offset, start_v + height) in remaining
                for offset in range(width)
            ):
                height += 1

            for u in range(start_u, start_u + width):
                for v in range(start_v, start_v + height):
                    remaining.remove((u, v))
            yield start_u, start_v, width, height

    @staticmethod
    def _quad_vertices(
        plane: _FacePlane,
        u: int,
        v: int,
        width: int,
        height: int,
    ) -> tuple[GridVertex, GridVertex, GridVertex, GridVertex]:
        s = plane.slice_coordinate
        if plane.axis == "X" and plane.sign > 0:
            return (
                (s, u, v),
                (s, u + width, v),
                (s, u + width, v + height),
                (s, u, v + height),
            )
        if plane.axis == "X":
            return (
                (s, u, v),
                (s, u, v + height),
                (s, u + width, v + height),
                (s, u + width, v),
            )
        if plane.axis == "Y" and plane.sign > 0:
            return (
                (u, s, v),
                (u, s, v + height),
                (u + width, s, v + height),
                (u + width, s, v),
            )
        if plane.axis == "Y":
            return (
                (u, s, v),
                (u + width, s, v),
                (u + width, s, v + height),
                (u, s, v + height),
            )
        if plane.sign > 0:
            return (
                (u, v, s),
                (u + width, v, s),
                (u + width, v + height, s),
                (u, v + height, s),
            )
        return (
            (u, v, s),
            (u, v + height, s),
            (u + width, v + height, s),
            (u + width, v, s),
        )
