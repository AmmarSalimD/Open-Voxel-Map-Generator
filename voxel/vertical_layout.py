"""Semantic vertical layout for generated voxel categories."""

from __future__ import annotations

from ..domain.enums import FeatureType


class SemanticVerticalLayout:
    """Map integer grid boundaries to category-specific world elevations."""

    GROUND_TOP = 0.35
    ROAD_BOTTOM = -0.18
    ROAD_TOP = 0.0
    WATER_BOTTOM = -0.80
    WATER_TOP = -0.55
    GREEN_BOTTOM = 0.20
    GREEN_TOP = 0.40

    @staticmethod
    def boundary_height(
        category: FeatureType,
        boundary_z: int,
        horizontal_voxel_size: float,
        vertical_step: float,
    ) -> float:
        """Return Blender Z for one category boundary."""
        if category is FeatureType.TERRAIN:
            depth = min(1.25, max(0.60, horizontal_voxel_size * 0.18))
            if boundary_z == 0:
                return -depth
            if boundary_z == 1:
                return SemanticVerticalLayout.GROUND_TOP
            if boundary_z < 0:
                return -depth + boundary_z * depth
            return SemanticVerticalLayout.GROUND_TOP + (boundary_z - 1) * vertical_step

        if category is FeatureType.ROAD:
            return SemanticVerticalLayout._surface_boundary(
                boundary_z,
                lower_layer=0,
                bottom=SemanticVerticalLayout.ROAD_BOTTOM,
                top=SemanticVerticalLayout.ROAD_TOP,
                vertical_step=vertical_step,
            )

        if category is FeatureType.WATER:
            return SemanticVerticalLayout._surface_boundary(
                boundary_z,
                lower_layer=0,
                bottom=SemanticVerticalLayout.WATER_BOTTOM,
                top=SemanticVerticalLayout.WATER_TOP,
                vertical_step=vertical_step,
            )

        if category is FeatureType.GREEN:
            return SemanticVerticalLayout._surface_boundary(
                boundary_z,
                lower_layer=0,
                bottom=SemanticVerticalLayout.GREEN_BOTTOM,
                top=SemanticVerticalLayout.GREEN_TOP,
                vertical_step=vertical_step,
            )

        return SemanticVerticalLayout.GROUND_TOP + (boundary_z - 1) * vertical_step

    @staticmethod
    def cell_interval(
        category: FeatureType,
        cell_z: int,
        horizontal_voxel_size: float,
        vertical_step: float,
    ) -> tuple[float, float]:
        """Return ordered lower and upper world heights for one occupied cell."""
        first = SemanticVerticalLayout.boundary_height(
            category,
            cell_z,
            horizontal_voxel_size,
            vertical_step,
        )
        second = SemanticVerticalLayout.boundary_height(
            category,
            cell_z + 1,
            horizontal_voxel_size,
            vertical_step,
        )
        return min(first, second), max(first, second)

    @staticmethod
    def _surface_boundary(
        boundary_z: int,
        lower_layer: int,
        bottom: float,
        top: float,
        vertical_step: float,
    ) -> float:
        """Map a one-layer semantic surface while remaining safe for outliers."""
        if boundary_z == lower_layer:
            return bottom
        if boundary_z == lower_layer + 1:
            return top
        if boundary_z < lower_layer:
            return bottom - (lower_layer - boundary_z) * vertical_step
        return top + (boundary_z - lower_layer - 1) * vertical_step
