"""Deterministic style profiles that materially change generated geometry."""

from __future__ import annotations

from dataclasses import dataclass

from ..domain.enums import ModelStyle


@dataclass(frozen=True, slots=True)
class StyleProfile:
    """Low-level decisions derived from one high-level city style."""

    direct_buildings: bool
    footprint_simplification_m: float
    minecraft_macro_cells: int
    height_quantization_m: float
    allow_building_parts: bool
    allow_roof_shapes: bool
    allow_curved_details: bool
    facade_detail_strength: float
    road_width_scale: float
    tree_blockiness: float


_STYLE_PROFILES: dict[ModelStyle, StyleProfile] = {
    ModelStyle.CLASSIC_VOXEL: StyleProfile(
        direct_buildings=False,
        footprint_simplification_m=0.0,
        minecraft_macro_cells=1,
        height_quantization_m=0.0,
        allow_building_parts=True,
        allow_roof_shapes=True,
        allow_curved_details=False,
        facade_detail_strength=0.35,
        road_width_scale=1.0,
        tree_blockiness=0.65,
    ),
    ModelStyle.MINECRAFT: StyleProfile(
        direct_buildings=False,
        footprint_simplification_m=0.0,
        minecraft_macro_cells=2,
        height_quantization_m=3.0,
        allow_building_parts=True,
        allow_roof_shapes=False,
        allow_curved_details=False,
        facade_detail_strength=0.0,
        road_width_scale=1.18,
        tree_blockiness=1.0,
    ),
    ModelStyle.LOW_POLY: StyleProfile(
        direct_buildings=True,
        footprint_simplification_m=2.0,
        minecraft_macro_cells=1,
        height_quantization_m=1.0,
        allow_building_parts=True,
        allow_roof_shapes=True,
        allow_curved_details=True,
        facade_detail_strength=0.30,
        road_width_scale=1.0,
        tree_blockiness=0.25,
    ),
    ModelStyle.ARCHITECTURAL_MODEL: StyleProfile(
        direct_buildings=True,
        footprint_simplification_m=0.15,
        minecraft_macro_cells=1,
        height_quantization_m=0.0,
        allow_building_parts=True,
        allow_roof_shapes=True,
        allow_curved_details=True,
        facade_detail_strength=0.15,
        road_width_scale=1.0,
        tree_blockiness=0.10,
    ),
    ModelStyle.REAL: StyleProfile(
        direct_buildings=True,
        footprint_simplification_m=0.20,
        minecraft_macro_cells=1,
        height_quantization_m=0.25,
        allow_building_parts=True,
        allow_roof_shapes=True,
        allow_curved_details=True,
        facade_detail_strength=1.0,
        road_width_scale=1.0,
        tree_blockiness=0.0,
    ),
}


def style_profile(style: ModelStyle) -> StyleProfile:
    """Return the immutable generation profile for ``style``."""
    return _STYLE_PROFILES[style]
