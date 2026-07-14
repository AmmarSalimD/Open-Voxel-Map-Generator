"""Blender property groups and add-on preferences."""

from __future__ import annotations

import bpy
from bpy.props import (
    BoolProperty,
    EnumProperty,
    FloatProperty,
    FloatVectorProperty,
    IntProperty,
    StringProperty,
)

from ..core.constants import (
    ADDON_ID,
    ADDON_NAME,
    ADDON_PACKAGE,
    DEFAULT_NOMINATIM_ENDPOINT,
    DEFAULT_OVERPASS_ENDPOINT,
    DEFAULT_USER_AGENT,
)
from ..infrastructure.map_selector.history import AreaHistoryStore

_PREVIOUS_AREA_ITEMS_CACHE: list[tuple[str, str, str]] = []

_QUALITY_PRESETS: dict[str, dict[str, object]] = {
    "LOW": {
        "voxel_size": 5.0,
        "vertical_step": 2.0,
        "chunk_size": 64,
        "tree_density": 0.08,
        "minimum_building_area": 25.0,
        "use_building_parts": True,
        "use_roof_shapes": False,
        "curved_detail_segments": 8,
        "maximum_label_count": 100,
    },
    "MEDIUM": {
        "voxel_size": 2.5,
        "vertical_step": 1.0,
        "chunk_size": 64,
        "tree_density": 0.20,
        "minimum_building_area": 15.0,
        "use_building_parts": True,
        "use_roof_shapes": True,
        "curved_detail_segments": 16,
        "maximum_label_count": 250,
    },
    "HIGH": {
        # Maximum-accuracy preset for carefully selected districts. The review
        # step blocks oversized areas before generation.
        "voxel_size": 1.5,
        "vertical_step": 0.5,
        "chunk_size": 64,
        "tree_density": 0.30,
        "minimum_building_area": 6.0,
        "use_building_parts": True,
        "use_roof_shapes": True,
        "curved_detail_segments": 24,
        "maximum_label_count": 500,
    },
}


def _apply_quality_preset(settings: object, _context: bpy.types.Context) -> None:
    """Apply one simple quality preset to the underlying advanced controls."""
    preset = str(settings.quality_preset)
    values = _QUALITY_PRESETS.get(preset)
    if values is None:
        return
    settings["_ovmg_quality_update"] = True
    try:
        for key, value in values.items():
            setattr(settings, key, value)
    finally:
        if "_ovmg_quality_update" in settings:
            del settings["_ovmg_quality_update"]
    # Quality controls resolution; the selected style owns geometry rules such
    # as building parts, curved details, and Minecraft roof simplification.
    _apply_model_style(settings, _context)


def _mark_quality_custom(settings: object, _context: bpy.types.Context) -> None:
    """Keep quality controlled by the three public presets.

    Legacy low-level properties retain update callbacks for file compatibility,
    but they no longer introduce a confusing fourth Custom choice.
    """
    del settings, _context


def _preferences(context: bpy.types.Context | None) -> object | None:
    """Return this extension's preferences for dynamic enum callbacks."""
    if context is None:
        return None
    addon = context.preferences.addons.get(ADDON_PACKAGE)
    if addon is None:
        addon = context.preferences.addons.get(ADDON_ID)
    return addon.preferences if addon is not None else None


def _previous_area_items(
    _settings: object,
    context: bpy.types.Context | None,
) -> list[tuple[str, str, str]]:
    """Build the Previous Area menu from persistent visual selections."""
    preferences = _preferences(context)
    global _PREVIOUS_AREA_ITEMS_CACHE
    if preferences is None:
        _PREVIOUS_AREA_ITEMS_CACHE = [
            ("NONE", "No Previous Areas", "Select a new area on the map first")
        ]
        return _PREVIOUS_AREA_ITEMS_CACHE
    entries = AreaHistoryStore.load(preferences)
    if not entries:
        _PREVIOUS_AREA_ITEMS_CACHE = [
            ("NONE", "No Previous Areas", "Select a new area on the map first")
        ]
        return _PREVIOUS_AREA_ITEMS_CACHE
    _PREVIOUS_AREA_ITEMS_CACHE = [
        (
            entry.key,
            entry.name,
            (
                f"{entry.south:.5f}, {entry.west:.5f} → "
                f"{entry.north:.5f}, {entry.east:.5f}"
            ),
        )
        for entry in entries
    ]
    return _PREVIOUS_AREA_ITEMS_CACHE


def _apply_model_style(settings: object, _context: bpy.types.Context) -> None:
    """Map one user-friendly style to the detailed generation controls."""
    style = str(settings.model_style)
    settings["_ovmg_style_update"] = True
    try:
        if style == "MINECRAFT":
            settings.geometry_style = "VOXEL_CURVED"
            settings.use_building_parts = True
            settings.use_roof_shapes = False
            settings.generate_facade_detail = True
            settings.curved_detail_segments = 8
        elif style == "LOW_POLY":
            settings.geometry_style = "VOXEL_CURVED"
            settings.use_building_parts = True
            settings.use_roof_shapes = True
            settings.generate_facade_detail = True
            settings.curved_detail_segments = 8
        elif style == "ARCHITECTURAL_MODEL":
            settings.geometry_style = "VOXEL_CURVED"
            settings.use_building_parts = True
            settings.use_roof_shapes = True
            settings.generate_facade_detail = True
            settings.curved_detail_segments = 20
        elif style == "REAL":
            settings.geometry_style = "VOXEL_CURVED"
            settings.use_building_parts = True
            settings.use_roof_shapes = True
            settings.generate_facade_detail = True
            settings.curved_detail_segments = 24
        else:
            settings.geometry_style = "VOXEL_CURVED"
            settings.use_building_parts = True
            settings.use_roof_shapes = True
            settings.generate_facade_detail = False
            settings.curved_detail_segments = 24
    finally:
        if "_ovmg_style_update" in settings:
            del settings["_ovmg_style_update"]


def _apply_material_style(settings: object, _context: bpy.types.Context) -> None:
    """Keep the legacy enhanced-material flag synchronized."""
    settings.enhanced_materials = str(settings.material_style) == "REALISTIC"


def restore_recommended_generation_defaults(settings: object) -> None:
    """Restore safe generation defaults without clearing the chosen area or map.

    This intentionally preserves the project name, confirmed geographic bounds,
    wizard state, generated scene objects, and building corrections.
    """
    settings.large_area_mode = "BLOCK"
    settings.model_style = "REAL"
    settings.material_style = "REALISTIC"
    settings.enhanced_materials = True
    settings.geometry_style = "VOXEL_CURVED"

    # Internal fields are synchronized for compatibility with older project
    # files; normal generation derives them from the three public presets.
    settings.voxel_size = 1.5
    settings.vertical_step = 0.5
    settings.chunk_size = 64
    settings.tree_density = 0.30

    settings.building_source = "HYBRID"
    settings.default_building_height = 9.0
    settings.level_height = 3.0
    settings.use_building_parts = True
    settings.use_roof_shapes = True
    settings.infer_missing_heights = True
    settings.minimum_building_area = 6.0
    settings.generate_approximate_buildings = False
    settings.approximate_building_density = 0.35
    # Recommended mode favors real mapped geometry. Landmark-only points and
    # procedural infill remain opt-in because they can create false blocks.
    settings.generate_landmark_proxies = False
    settings.fallback_river_width = 220.0
    settings.curved_detail_segments = 24
    settings.curved_detail_limit = 350
    settings.generate_facade_detail = True
    settings.use_source_facade_hints = True
    settings.strict_real_facades = True
    settings.show_accuracy_overlay = False
    settings.accuracy_overlay_limit = 5000
    settings.show_advanced_options = False

    settings.generate_labels = False
    settings.label_mode = "METADATA_ONLY"
    settings.label_language = "ENGLISH"
    settings.include_street_labels = True
    settings.include_area_labels = True
    settings.include_landmark_labels = True
    settings.maximum_label_count = 250

    settings.include_terrain = True
    settings.include_buildings = True
    settings.include_roads = True
    settings.include_water = True
    settings.include_green = True
    settings.include_bridges = True
    settings.include_trees = True

    settings.export_format = "GLB"
    settings.export_include_materials = True
    settings.export_include_labels = False
    settings.export_apply_transforms = True
    settings.export_buildings = True
    settings.export_roads = True
    settings.export_bridges = True
    settings.export_terrain = True
    settings.export_water = True
    settings.export_green = True
    settings.export_trees = True

    # Keep the three visible controls synchronized with the most accurate
    # supported defaults. Their callbacks reapply the corresponding internal
    # geometry and material rules in a deterministic order.
    settings.quality_preset = "HIGH"
    settings.model_style = "REAL"
    settings.material_style = "REALISTIC"



def _apply_building_editor_preset(settings: object, _context: bpy.types.Context) -> None:
    """Fill the compact building editor with practical one-click defaults."""
    preset = str(settings.building_editor_add_preset)
    if preset == "CUSTOM":
        return
    values: dict[str, object] = {
        "SMALL_HOUSE": {
            "user_building_shape": "BOX",
            "user_building_roof_shape": "GABLED",
            "user_building_width": 10.0,
            "user_building_depth": 12.0,
            "user_building_height": 6.0,
            "user_building_roof_height": 2.0,
            "user_building_facade_profile": "residential_brick",
            "user_building_name": "Small House",
        },
        "RESIDENTIAL": {
            "user_building_shape": "BOX",
            "user_building_roof_shape": "FLAT",
            "user_building_width": 18.0,
            "user_building_depth": 16.0,
            "user_building_height": 12.0,
            "user_building_roof_height": 0.0,
            "user_building_facade_profile": "generic_plaster",
            "user_building_name": "Residential Building",
        },
        "COMMERCIAL": {
            "user_building_shape": "BOX",
            "user_building_roof_shape": "FLAT",
            "user_building_width": 24.0,
            "user_building_depth": 18.0,
            "user_building_height": 15.0,
            "user_building_roof_height": 0.0,
            "user_building_facade_profile": "commercial_glass",
            "user_building_name": "Commercial Building",
        },
        "MOSQUE": {
            "user_building_shape": "BOX",
            "user_building_roof_shape": "DOME",
            "user_building_width": 24.0,
            "user_building_depth": 28.0,
            "user_building_height": 12.0,
            "user_building_roof_height": 7.0,
            "user_building_facade_profile": "worship_stone",
            "user_building_name": "Mosque",
        },
        "TOWER": {
            "user_building_shape": "CYLINDER",
            "user_building_roof_shape": "FLAT",
            "user_building_width": 14.0,
            "user_building_depth": 14.0,
            "user_building_height": 35.0,
            "user_building_roof_height": 0.0,
            "user_building_facade_profile": "metal_tower",
            "user_building_name": "Tower",
        },
    }.get(preset, {})
    for key, value in values.items():
        setattr(settings, key, value)


class OVMG_AddonPreferences(bpy.types.AddonPreferences):
    """Persistent endpoints and safety limits for network map generation."""

    bl_idname = ADDON_PACKAGE

    overpass_endpoint: StringProperty(
        name="Overpass Endpoint",
        description="HTTPS endpoint used to download OpenStreetMap geometry",
        default=DEFAULT_OVERPASS_ENDPOINT,
    )
    nominatim_endpoint: StringProperty(
        name="Nominatim Endpoint",
        description="HTTPS endpoint used to resolve an area name",
        default=DEFAULT_NOMINATIM_ENDPOINT,
    )
    overture_release: StringProperty(
        name="Overture Release",
        description=(
            "Optional release such as 2026-06-17.0; leave empty to resolve the "
            "latest release through the official Overture STAC catalog"
        ),
        default="",
    )
    user_agent: StringProperty(
        name="HTTP User Agent",
        description="Identifier sent to GIS services",
        default=DEFAULT_USER_AGENT,
    )
    network_timeout: IntProperty(
        name="Network Timeout",
        description="Maximum duration of one remote request in seconds",
        default=240,
        min=15,
        max=1200,
    )
    network_retries: IntProperty(
        name="Network Retries",
        description="Number of retries after a temporary network failure",
        default=2,
        min=0,
        max=5,
    )
    max_voxel_cells: IntProperty(
        name="Maximum Voxel Cells",
        description=(
            "Safety limit before greedy meshing; detailed city maps need a "
            "higher limit than coarse maps"
        ),
        default=12_000_000,
        min=100_000,
        max=100_000_000,
    )
    max_overture_features: IntProperty(
        name="Maximum Overture Features",
        description="Safety limit for downloaded buildings and building parts",
        default=250_000,
        min=1_000,
        max=2_000_000,
    )
    saved_area_history_json: StringProperty(
        name="Saved Area History",
        description="Internal JSON history of confirmed visual map rectangles",
        default="",
        options={"HIDDEN"},
    )
    saved_area_limit: IntProperty(
        name="Saved Area Limit",
        description="Maximum number of previous visual selections to retain",
        default=12,
        min=1,
        max=50,
    )

    def draw(self, _context: bpy.types.Context) -> None:
        """Draw extension preferences."""
        layout = self.layout
        layout.label(text=f"{ADDON_NAME} — Service and Safety Settings")
        network_box = layout.box()
        network_box.label(text="GIS Services", icon="URL")
        network_box.prop(self, "overpass_endpoint")
        network_box.prop(self, "nominatim_endpoint")
        network_box.prop(self, "overture_release")
        network_box.prop(self, "user_agent")
        row = network_box.row(align=True)
        row.prop(self, "network_timeout")
        row.prop(self, "network_retries")
        safety_box = layout.box()
        safety_box.label(text="Memory and Download Safety", icon="MEMORY")
        safety_box.prop(self, "max_voxel_cells")
        safety_box.prop(self, "max_overture_features")
        history_box = layout.box()
        history_box.label(text="Previous Area History", icon="TIME")
        history_box.prop(self, "saved_area_limit")
        history_box.label(text="Confirmed map rectangles are saved automatically.")


class OVMG_SceneSettings(bpy.types.PropertyGroup):
    """Per-scene controls exposed in the simplified Voxel Maps sidebar."""

    project_name: StringProperty(
        name="Project Name",
        description="Name used for generated collections and metadata",
        default="Voxel_Map_Project",
    )
    wizard_step: IntProperty(
        name="Setup Step",
        description="Current step in the guided map-generation workflow",
        default=1,
        min=1,
        max=8,
    )
    settings_schema_version: IntProperty(
        name="Settings Schema Version",
        description="Internal version used to migrate safe defaults",
        default=186,
        options={"HIDDEN"},
    )
    generated_map_ready: BoolProperty(
        name="Map Ready",
        description="A generated project is ready for editing or export",
        default=False,
    )
    generation_progress: FloatProperty(
        name="Generation Progress",
        description="Current map-generation completion ratio",
        default=0.0,
        min=0.0,
        max=1.0,
        subtype="FACTOR",
        options={"SKIP_SAVE"},
    )
    area_choice: EnumProperty(
        name="Area Choice",
        description="Select a new rectangle or reuse a prior confirmed rectangle",
        items=(
            (
                "NEW",
                "Choose on Map",
                "Open the visual selector and draw a new rectangle",
            ),
            (
                "PREVIOUS",
                "Use Previous Area",
                "Reuse one of your automatically saved visual selections",
            ),
        ),
        default="NEW",
    )
    previous_area_key: EnumProperty(
        name="Previous Area",
        description="A previously confirmed map rectangle",
        items=_previous_area_items,
    )
    input_mode: EnumProperty(
        name="Area Input",
        description="Choose a place name or explicit geographic bounds",
        items=(
            (
                "VISUAL_MAP",
                "Visual Map",
                "Draw and edit the exact generation rectangle in a browser map",
            ),
            ("AREA_NAME", "Search Area", "Resolve a place name with Nominatim"),
            ("BOUNDING_BOX", "Bounding Box", "Use south, west, north, east"),
        ),
        default="VISUAL_MAP",
    )
    area_name: StringProperty(
        name="Area Name",
        description="Optional place hint used only to center the selector map",
        default="Baghdad, Iraq",
    )
    visual_area_selected: BoolProperty(
        name="Visual Area Selected",
        description="The browser selector has returned explicit geographic bounds",
        default=False,
    )
    visual_selection_pending: BoolProperty(
        name="Area Confirmation Pending",
        description="A browser selection is waiting for confirmation in Blender",
        default=False,
        options={"SKIP_SAVE"},
    )
    pending_area_name: StringProperty(
        name="Pending Area Name",
        default="",
        options={"SKIP_SAVE"},
    )
    pending_south: FloatProperty(
        name="Pending South", default=0.0, precision=7, options={"SKIP_SAVE"}
    )
    pending_west: FloatProperty(
        name="Pending West", default=0.0, precision=7, options={"SKIP_SAVE"}
    )
    pending_north: FloatProperty(
        name="Pending North", default=0.0, precision=7, options={"SKIP_SAVE"}
    )
    pending_east: FloatProperty(
        name="Pending East", default=0.0, precision=7, options={"SKIP_SAVE"}
    )
    pending_preview_path: StringProperty(
        name="Pending Preview Path",
        description="Temporary browser-map preview staged for explicit UI review",
        default="",
        options={"HIDDEN", "SKIP_SAVE"},
    )
    large_area_mode: EnumProperty(
        name="Oversized Area",
        description="Block oversized requests or split them into aligned map tiles",
        items=(
            (
                "BLOCK",
                "Require Smaller Area",
                "Stop before generation when one safe voxel budget is exceeded",
            ),
            (
                "SPLIT_TILES",
                "Split Into Map Tiles",
                "Generate aligned sub-areas and assemble them in one project",
            ),
        ),
        default="BLOCK",
    )
    south: FloatProperty(
        name="South", default=33.3400, min=-90.0, max=90.0, precision=7
    )
    west: FloatProperty(
        name="West", default=44.3350, min=-180.0, max=180.0, precision=7
    )
    north: FloatProperty(
        name="North", default=33.3900, min=-90.0, max=90.0, precision=7
    )
    east: FloatProperty(
        name="East", default=44.4050, min=-180.0, max=180.0, precision=7
    )

    model_style: EnumProperty(
        name="3D Style",
        description="High-level visual treatment of the generated city",
        items=(
            (
                "CLASSIC_VOXEL",
                "Classic Voxel",
                "Accurate geographic forms expressed as optimized voxel blocks",
            ),
            (
                "MINECRAFT",
                "Minecraft Style",
                "Coarse macro-block footprints, quantized heights, flat roofs, and no curves",
            ),
            (
                "LOW_POLY",
                "Low Poly",
                "Direct simplified polygon footprints with low-segment roofs and landmarks",
            ),
            (
                "REAL",
                "Real",
                "Preserve source footprints and parts with source-aware heights, roofs, and facades",
            ),
            (
                "ARCHITECTURAL_MODEL",
                "Architectural Model",
                "Clean sand-colored urban model with light roads, blue water, green parks, and source-accurate geometry",
            ),
        ),
        default="REAL",
        update=_apply_model_style,
    )
    material_style: EnumProperty(
        name="Materials",
        description="Choose lightweight colors or procedural PBR materials",
        items=(
            (
                "SIMPLE",
                "Simple",
                "Fast category colors with compact export-friendly materials",
            ),
            (
                "REALISTIC",
                "Realistic",
                "Procedural PBR roughness, relief, water, and surface variation",
            ),
        ),
        default="REALISTIC",
        update=_apply_material_style,
    )

    quality_preset: EnumProperty(
        name="Quality",
        description="Choose a simple performance/detail preset",
        items=(
            ("LOW", "Low", "Fast generation for large maps and previews"),
            ("MEDIUM", "Medium", "Balanced detail and performance"),
            ("HIGH", "High", "Maximum detail for smaller selected areas"),
        ),
        default="HIGH",
        update=_apply_quality_preset,
    )
    enhanced_materials: BoolProperty(
        name="Enhanced Materials",
        description="Use optional procedural material variation and improved water",
        default=True,
    )
    geometry_style: EnumProperty(
        name="Geometry Style",
        description="Choose pure voxels or optional curved landmark details",
        items=(
            (
                "VOXEL_ONLY",
                "Voxel Only",
                "Keep every generated element in a strict block style",
            ),
            (
                "VOXEL_CURVED",
                "Voxel + Curved Details",
                "Add lightweight domes, minarets, towers, and water tanks",
            ),
        ),
        default="VOXEL_CURVED",
    )
    generate_labels: BoolProperty(
        name="Map Labels",
        description="Optionally extract street, area, and landmark names",
        default=False,
    )
    show_advanced_options: BoolProperty(
        name="Advanced Options",
        description="Show detailed GIS, voxel, layer, label, and export controls",
        default=False,
    )

    voxel_size: FloatProperty(
        name="Horizontal Voxel Size",
        description="Building-footprint and map resolution in XY meters",
        default=1.5,
        min=0.5,
        max=100.0,
        soft_min=1.0,
        soft_max=10.0,
        unit="LENGTH",
        update=_mark_quality_custom,
    )
    vertical_step: FloatProperty(
        name="Vertical Height Step",
        description="Independent vertical voxel step used for building heights",
        default=0.5,
        min=0.25,
        max=20.0,
        soft_min=0.5,
        soft_max=3.0,
        unit="LENGTH",
        update=_mark_quality_custom,
    )
    chunk_size: IntProperty(
        name="Chunk Size",
        description="Chunk width and depth measured in horizontal voxel cells",
        default=64,
        min=8,
        max=256,
        update=_mark_quality_custom,
    )
    default_building_height: FloatProperty(
        name="Default Building Height",
        description="Used only when no real or inferred height is available",
        default=9.0,
        min=1.0,
        max=1000.0,
        unit="LENGTH",
    )
    level_height: FloatProperty(
        name="Level Height",
        description="Meters per reported building floor",
        default=3.0,
        min=1.0,
        max=20.0,
        unit="LENGTH",
    )
    tree_density: FloatProperty(
        name="Tree Density",
        description="Deterministic density of generated trees in green areas",
        default=0.30,
        min=0.0,
        max=1.0,
        subtype="FACTOR",
        update=_mark_quality_custom,
    )

    building_source: EnumProperty(
        name="Building Source",
        description="Choose the source used for building footprints",
        items=(
            (
                "HYBRID",
                "Hybrid — Recommended",
                "Overture footprints plus unmatched OSM buildings and landmarks",
            ),
            (
                "OVERTURE_ONLY",
                "Overture Only",
                "Use Overture buildings while retaining OSM map layers",
            ),
            ("OSM_ONLY", "OSM Only", "Use only OpenStreetMap building geometry"),
        ),
        default="HYBRID",
    )
    use_building_parts: BoolProperty(
        name="Use Building Parts",
        description="Internal style-controlled building parts setting",
        default=False,
        update=_mark_quality_custom,
    )
    use_roof_shapes: BoolProperty(
        name="Use Roof Shapes",
        description="Generate stepped voxel roofs when metadata is available",
        default=True,
        update=_mark_quality_custom,
    )
    infer_missing_heights: BoolProperty(
        name="Infer Missing Heights",
        description="Infer deterministic heights when source data is absent",
        default=True,
    )
    minimum_building_area: FloatProperty(
        name="Minimum Building Area",
        description="Internal quality-controlled footprint threshold",
        default=15.0,
        min=1.0,
        max=10_000.0,
        unit="AREA",
        update=_mark_quality_custom,
    )
    generate_approximate_buildings: BoolProperty(
        name="Approximate Missing Buildings",
        description="Generate procedural street-side massing when explicitly enabled",
        default=False,
    )
    approximate_building_density: FloatProperty(
        name="Approximate Building Density",
        description="Density of optional procedural missing-building massing",
        default=0.35,
        min=0.0,
        max=1.0,
        subtype="FACTOR",
    )
    generate_landmark_proxies: BoolProperty(
        name="Landmark Proxies",
        description="Internal compatibility setting; disabled in safe generation",
        default=False,
    )
    fallback_river_width: FloatProperty(
        name="Minimum Major River Width",
        description="Minimum width enforced for major river centerlines",
        default=220.0,
        min=5.0,
        max=1000.0,
        soft_min=20.0,
        soft_max=300.0,
        unit="LENGTH",
    )
    curved_detail_segments: IntProperty(
        name="Curved Detail Segments",
        description="Angular resolution for optional domes and towers",
        default=16,
        min=6,
        max=64,
        update=_mark_quality_custom,
    )
    curved_detail_limit: IntProperty(
        name="Curved Detail Limit",
        description="Maximum optional curved landmark objects",
        default=500,
        min=0,
        max=10_000,
    )
    generate_facade_detail: BoolProperty(
        name="Generate Facade Detail",
        description=(
            "Use source-aware procedural facade profiles and floor/window rhythm "
            "for Real-style buildings"
        ),
        default=True,
    )
    use_source_facade_hints: BoolProperty(
        name="Use Source Facade Hints",
        description="Use mapped facade material and colour tags when available",
        default=True,
    )
    strict_real_facades: BoolProperty(
        name="Strict Real Facades",
        description=(
            "Show facade detail only when material or colour is present in the "
            "source data; unverified buildings remain neutral and are labelled "
            "as having no facade data"
        ),
        default=True,
    )
    show_accuracy_overlay: BoolProperty(
        name="Building Accuracy Overlay",
        description=(
            "Create a toggleable roof overlay: green/blue/yellow/red for decreasing "
            "building-data confidence"
        ),
        default=False,
    )
    accuracy_overlay_limit: IntProperty(
        name="Accuracy Overlay Limit",
        description="Maximum buildings represented in the optional confidence overlay",
        default=5000,
        min=0,
        max=50_000,
    )

    label_mode: EnumProperty(
        name="Label Mode",
        description="Store lightweight metadata or also show Blender text objects",
        items=(
            (
                "METADATA_ONLY",
                "Metadata Only",
                "Best for Unity; exports a lightweight JSON sidecar",
            ),
            (
                "BLENDER_TEXT",
                "Blender Text",
                "Also create optional text objects in the Blender scene",
            ),
        ),
        default="METADATA_ONLY",
    )
    label_language: EnumProperty(
        name="Label Language",
        items=(
            ("LOCAL", "Local", "Use the primary local OSM name"),
            ("ARABIC", "Arabic", "Prefer name:ar"),
            ("ENGLISH", "English", "Prefer name:en"),
            ("BILINGUAL", "Arabic + English", "Use two lines when available"),
        ),
        default="ENGLISH",
    )
    include_street_labels: BoolProperty(name="Street Labels", default=True)
    include_area_labels: BoolProperty(name="Area Labels", default=True)
    include_landmark_labels: BoolProperty(name="Landmark Labels", default=True)
    maximum_label_count: IntProperty(
        name="Maximum Labels",
        description="Limit labels to protect Blender and game performance",
        default=250,
        min=0,
        max=10_000,
        update=_mark_quality_custom,
    )

    include_terrain: BoolProperty(name="Terrain", default=True)
    include_buildings: BoolProperty(name="Buildings", default=True)
    include_roads: BoolProperty(name="Roads", default=True)
    include_water: BoolProperty(name="Water", default=True)
    include_green: BoolProperty(name="Green Areas", default=True)
    include_bridges: BoolProperty(name="Bridges", default=True)
    include_trees: BoolProperty(name="Trees", default=True)

    export_format: EnumProperty(
        name="Format",
        description="Model format used by the simplified exporter",
        items=(
            ("GLB", "GLB", "Compact binary glTF; recommended for most workflows"),
            ("FBX", "FBX", "Common Unity and DCC interchange format"),
            ("OBJ", "OBJ", "Simple static mesh interchange"),
            ("USD", "USD", "Professional scene interchange"),
            ("BLEND", "BLEND", "Save a copy of the current Blender project"),
        ),
        default="GLB",
    )
    export_buildings: BoolProperty(name="Buildings", default=True)
    export_roads: BoolProperty(name="Roads", default=True)
    export_bridges: BoolProperty(name="Bridges", default=True)
    export_terrain: BoolProperty(name="Terrain", default=True)
    export_water: BoolProperty(name="Water", default=True)
    export_green: BoolProperty(name="Green Areas", default=True)
    export_trees: BoolProperty(name="Trees", default=True)
    export_include_materials: BoolProperty(
        name="Include Materials",
        description="Export material assignments and supported shader data",
        default=True,
    )
    export_include_labels: BoolProperty(
        name="Include Labels",
        description="Write generated labels to a JSON sidecar for Unity or other apps",
        default=False,
    )
    export_apply_transforms: BoolProperty(
        name="Apply Transforms",
        description="Apply supported transform conversion during export",
        default=True,
    )


    building_editor_mode: EnumProperty(
        name="Editor Mode",
        description="Show only quick controls or expose the complete correction toolkit",
        items=(
            ("SIMPLE", "Simple", "Fast selection and the most common building changes"),
            ("ADVANCED", "Advanced", "Show custom footprints, source tools, and maintenance actions"),
        ),
        default="SIMPLE",
    )
    building_editor_add_preset: EnumProperty(
        name="New Building Type",
        description="Choose a practical preset before clicking its placement location",
        items=(
            ("SMALL_HOUSE", "Small House", "One or two-storey house with a pitched roof"),
            ("RESIDENTIAL", "Residential", "Typical multi-storey residential block"),
            ("COMMERCIAL", "Commercial", "Commercial building with a glass facade profile"),
            ("MOSQUE", "Mosque", "Prayer hall with a dome roof"),
            ("TOWER", "Tower", "Tall round tower"),
            ("CUSTOM", "Custom", "Keep the current manually entered building values"),
        ),
        default="RESIDENTIAL",
        update=_apply_building_editor_preset,
    )

    user_building_shape: EnumProperty(
        name="Footprint",
        items=(
            ("BOX", "Rectangle", "Rectangular building footprint"),
            ("L_SHAPE", "L Shape", "L-shaped building footprint"),
            ("U_SHAPE", "U Shape", "U-shaped building footprint"),
            ("CYLINDER", "Round", "Round or elliptical tower footprint"),
            ("DOME", "Domed", "Round footprint with a dome roof"),
        ),
        default="BOX",
    )
    user_building_roof_shape: EnumProperty(
        name="Roof",
        items=(
            ("FLAT", "Flat", "Flat roof"),
            ("GABLED", "Gabled", "Two-sided pitched roof"),
            ("HIPPED", "Hipped", "Four-sided pitched roof"),
            ("PYRAMID", "Pyramid", "Pyramidal roof"),
            ("DOME", "Dome", "Half-dome roof"),
            ("ONION", "Onion Dome", "Onion-shaped landmark roof"),
            ("CONE", "Cone", "Conical tower roof"),
        ),
        default="FLAT",
    )
    user_building_width: FloatProperty(
        name="Width", default=12.0, min=1.0, max=1000.0, unit="LENGTH"
    )
    user_building_depth: FloatProperty(
        name="Depth", default=12.0, min=1.0, max=1000.0, unit="LENGTH"
    )
    user_building_height: FloatProperty(
        name="Total Height", default=9.0, min=1.0, max=1000.0, unit="LENGTH"
    )
    user_building_base_height: FloatProperty(
        name="Base Height",
        description="Height of the building base above map ground",
        default=0.0,
        min=-100.0,
        max=1000.0,
        unit="LENGTH",
    )
    user_building_roof_height: FloatProperty(
        name="Roof Height",
        default=2.0,
        min=0.0,
        max=200.0,
        unit="LENGTH",
    )
    user_building_rotation: FloatProperty(
        name="Rotation",
        description="Rotation around the vertical axis",
        default=0.0,
        min=-6.283185307,
        max=6.283185307,
        subtype="ANGLE",
    )


    user_building_facade_profile: EnumProperty(
        name="Facade",
        items=(
            ("generic_plaster", "Plaster", "Generic plaster facade"),
            ("residential_brick", "Residential Brick", "Brick residential facade"),
            ("commercial_glass", "Commercial Glass", "Glass and metal commercial facade"),
            ("institutional_stone", "Stone", "Institutional stone facade"),
            ("industrial_concrete", "Concrete", "Industrial concrete facade"),
            ("worship_stone", "Worship Stone", "Light stone facade for religious buildings"),
            ("historic_brick", "Historic Brick", "Historic brick facade"),
            ("metal_tower", "Metal", "Metal tower facade"),
        ),
        default="generic_plaster",
    )
    user_building_facade_color: FloatVectorProperty(
        name="Facade Color",
        subtype="COLOR",
        size=4,
        min=0.0,
        max=1.0,
        default=(0.72, 0.61, 0.45, 1.0),
    )
    user_building_roof_color: FloatVectorProperty(
        name="Roof Color",
        subtype="COLOR",
        size=4,
        min=0.0,
        max=1.0,
        default=(0.38, 0.30, 0.22, 1.0),
    )
    user_building_name: StringProperty(
        name="Building Name",
        description="Optional readable name for the added or replacement building",
        default="",
    )
    building_editor_expanded: BoolProperty(
        name="Building Correction Studio",
        default=True,
    )
    correction_status: StringProperty(
        name="Correction Status",
        default="No pending building corrections.",
        options={"SKIP_SAVE"},
    )
    correction_requires_regeneration: BoolProperty(
        default=False,
        options={"SKIP_SAVE"},
    )
    inspector_roof_height: FloatProperty(default=0.0, options={"SKIP_SAVE"})
    inspector_base_height: FloatProperty(default=0.0, options={"SKIP_SAVE"})
    inspector_facade_hex: StringProperty(default="", options={"SKIP_SAVE"})
    inspector_roof_hex: StringProperty(default="", options={"SKIP_SAVE"})

    inspector_source_id: StringProperty(default="", options={"SKIP_SAVE"})
    inspector_building_type: StringProperty(default="", options={"SKIP_SAVE"})
    inspector_height: StringProperty(default="", options={"SKIP_SAVE"})
    inspector_height_source: StringProperty(default="", options={"SKIP_SAVE"})
    inspector_footprint_source: StringProperty(default="", options={"SKIP_SAVE"})
    inspector_roof: StringProperty(default="", options={"SKIP_SAVE"})
    inspector_facade: StringProperty(default="", options={"SKIP_SAVE"})
    inspector_confidence: StringProperty(default="", options={"SKIP_SAVE"})
    inspector_datasets: StringProperty(default="", options={"SKIP_SAVE"})
    inspector_distance: StringProperty(default="", options={"SKIP_SAVE"})

    status: StringProperty(name="Status", default="Ready", options={"SKIP_SAVE"})
    last_summary: StringProperty(
        name="Last Result",
        default="No map has been generated in this scene.",
        options={"SKIP_SAVE"},
    )
    building_summary_1: StringProperty(default="", options={"SKIP_SAVE"})
    building_summary_2: StringProperty(default="", options={"SKIP_SAVE"})
    building_summary_3: StringProperty(default="", options={"SKIP_SAVE"})
    building_summary_4: StringProperty(default="", options={"SKIP_SAVE"})
    building_summary_5: StringProperty(default="", options={"SKIP_SAVE"})
