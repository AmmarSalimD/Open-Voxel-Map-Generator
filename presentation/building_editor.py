"""Simplified, on-demand building editor for generated OVMG maps.

The main sidebar intentionally exposes only one entry point.  This module
contains the modal viewport pickers and a compact dialog that wrap the more
powerful correction operators already used by the add-on.
"""

from __future__ import annotations

import bpy

from ..infrastructure.blender.building_corrections import BuildingCorrectionStore
from ..infrastructure.blender.editable_buildings import root_user_building


def _active_editable_root(context: bpy.types.Context) -> bpy.types.Object | None:
    """Return the selected editable OVMG root, if any."""
    return root_user_building(context.view_layer.objects.active)


def _tag_view3d_redraw() -> None:
    """Request redraw for every visible 3D viewport."""
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()


def _ray_cast_from_event(
    context: bpy.types.Context,
    event: bpy.types.Event,
) -> tuple[bool, object, bpy.types.Object | None]:
    """Ray-cast from one viewport mouse event into the current scene."""
    from bpy_extras import view3d_utils

    area = context.area
    space = context.space_data
    if area is None or space is None or not hasattr(space, "region_3d"):
        return False, None, None
    region = next((item for item in area.regions if item.type == "WINDOW"), None)
    if region is None:
        return False, None, None
    mouse = (event.mouse_x - region.x, event.mouse_y - region.y)
    origin = view3d_utils.region_2d_to_origin_3d(region, space.region_3d, mouse)
    direction = view3d_utils.region_2d_to_vector_3d(region, space.region_3d, mouse)
    result = context.scene.ray_cast(
        context.evaluated_depsgraph_get(),
        origin,
        direction,
        distance=1.0e9,
    )
    hit = bool(result[0])
    location = result[1] if hit else None
    obj = result[4] if hit and len(result) > 4 else None
    return hit, location, obj




class OVMG_OT_OpenBuildingEditor(bpy.types.Operator):
    """Open the compact building correction dialog on demand."""

    bl_idname = "ovmg.open_building_editor"
    bl_label = "Building Editor"
    bl_description = "Open a simple window for selecting, correcting, deleting, or adding buildings"
    bl_options = {"REGISTER"}

    def invoke(
        self,
        context: bpy.types.Context,
        _event: bpy.types.Event,
    ) -> set[str]:
        return context.window_manager.invoke_props_dialog(self, width=520)

    def execute(self, _context: bpy.types.Context) -> set[str]:
        return {"FINISHED"}

    def draw(self, context: bpy.types.Context) -> None:
        settings = context.scene.ovmg_settings
        layout = self.layout

        header = layout.box()
        header.label(text="Building Editor", icon="HOME")
        header.label(text="Click a building in the map, then edit it with simple controls.")
        header.prop(settings, "building_editor_mode", expand=True)
        reset_row = header.row()
        reset_row.operator(
            "ovmg.reset_building_editor_defaults",
            text="Reset Editor Defaults",
            icon="LOOP_BACK",
        )

        project = str(settings.project_name)
        correction_summary = BuildingCorrectionStore.summary(project)
        summary = header.row(align=True)
        summary.label(text=f"Replaced: {correction_summary.replacements}")
        summary.label(text=f"Added: {correction_summary.added_buildings}")
        summary.label(text=f"Hidden: {correction_summary.suppressed_sources}")

        selection = layout.box()
        selection.label(text="1. Select a Building", icon="RESTRICT_SELECT_OFF")
        pick = selection.row()
        pick.scale_y = 1.35
        pick.operator(
            "ovmg.pick_building_interactive",
            text="Click Building in Viewport",
            icon="EYEDROPPER",
        )

        editable_root = _active_editable_root(context)
        if editable_root is not None:
            selected = selection.box()
            selected.label(text="Editable Building Selected", icon="CHECKMARK")
            selected.label(text=editable_root.name[:90])
        elif settings.inspector_source_id:
            selected = selection.box()
            selected.label(text="Generated Building Selected", icon="CHECKMARK")
            selected.label(
                text=f"{settings.inspector_building_type} • {settings.inspector_height}"
            )
            selected.label(text=f"Confidence: {settings.inspector_confidence}")
        else:
            selection.label(text="No building selected yet.", icon="INFO")

        parameters = layout.box()
        parameters.label(text="2. Quick Changes", icon="PREFERENCES")
        parameters.prop(settings, "user_building_name")
        shape = parameters.row(align=True)
        shape.prop(settings, "user_building_shape")
        shape.prop(settings, "user_building_roof_shape")
        dimensions = parameters.row(align=True)
        dimensions.prop(settings, "user_building_width")
        dimensions.prop(settings, "user_building_depth")
        height = parameters.row(align=True)
        height.prop(settings, "user_building_height")
        height.prop(settings, "user_building_facade_profile")

        if str(settings.building_editor_mode) == "ADVANCED":
            advanced = parameters.box()
            advanced.label(text="Advanced Values")
            row = advanced.row(align=True)
            row.prop(settings, "user_building_base_height")
            row.prop(settings, "user_building_roof_height")
            advanced.prop(settings, "user_building_rotation")
            colors = advanced.row(align=True)
            colors.prop(settings, "user_building_facade_color")
            colors.prop(settings, "user_building_roof_color")

        actions = layout.box()
        actions.label(text="3. Apply an Action", icon="TOOL_SETTINGS")
        if editable_root is not None:
            row = actions.row(align=True)
            row.scale_y = 1.3
            row.operator(
                "ovmg.apply_selected_user_building",
                text="Apply Changes",
                icon="CHECKMARK",
            )
            row.operator(
                "ovmg.delete_selected_user_buildings",
                text="Delete",
                icon="TRASH",
            )
            transform = actions.row(align=True)
            transform.operator("transform.translate", text="Move", icon="TRANSFORM_MOVE")
            transform.operator("transform.resize", text="Resize", icon="TRANSFORM_SCALE")
            transform.operator("transform.rotate", text="Rotate", icon="TRANSFORM_ROTATE")
            extras = actions.row(align=True)
            extras.operator("ovmg.duplicate_selected_user_building", text="Duplicate")
            extras.operator("ovmg.snap_selected_user_building", text="Snap to Ground")
        elif settings.inspector_source_id:
            row = actions.row(align=True)
            row.scale_y = 1.3
            row.operator(
                "ovmg.make_inspected_building_editable",
                text="Make Editable",
                icon="MODIFIER",
            )
            row.operator(
                "ovmg.mark_inspected_building_deleted",
                text="Delete Building",
                icon="TRASH",
            )
            actions.operator(
                "ovmg.restore_inspected_building",
                text="Restore Original",
                icon="LOOP_BACK",
            )
        else:
            actions.label(text="Select a building first.", icon="INFO")

        add = layout.box()
        add.label(text="4. Add a Missing Building", icon="ADD")
        add.prop(settings, "building_editor_add_preset")
        add_row = add.row()
        add_row.scale_y = 1.3
        add_row.operator(
            "ovmg.place_building_interactive",
            text="Click Location and Add",
            icon="CURSOR",
        )
        if str(settings.building_editor_mode) == "ADVANCED":
            advanced_add = add.row(align=True)
            advanced_add.operator(
                "ovmg.add_building_from_curve",
                text="Build from Selected Curve",
            )
            advanced_add.operator(
                "ovmg.convert_selected_to_user_building",
                text="Register Imported",
            )

        pending = layout.box()
        pending.label(text="5. Finish", icon="CHECKMARK")
        if settings.correction_requires_regeneration or correction_summary.suppressed_sources:
            pending.alert = True
            pending.label(text="Source geometry changes are waiting to be applied.")
            apply_row = pending.row()
            apply_row.scale_y = 1.4
            apply_row.operator(
                "ovmg.regenerate_with_building_corrections",
                text="Apply All Changes",
                icon="FILE_REFRESH",
            )
        else:
            pending.label(text="All visible changes are already active.", icon="CHECKMARK")

        if str(settings.building_editor_mode) == "ADVANCED":
            maintenance = layout.box()
            maintenance.label(text="Advanced Tools", icon="TOOL_SETTINGS")
            row = maintenance.row(align=True)
            row.operator("ovmg.select_user_buildings", text="Select All Editable")
            row.operator("ovmg.toggle_accuracy_overlay", text="Accuracy Overlay")
            maintenance.operator(
                "ovmg.clear_building_corrections",
                text="Clear All Corrections",
                icon="TRASH",
            )

        if settings.correction_status:
            status = layout.box()
            status.label(text=str(settings.correction_status)[:120], icon="INFO")


class OVMG_OT_ResetBuildingEditorDefaults(bpy.types.Operator):
    """Restore the compact editor to safe, predictable values."""

    bl_idname = "ovmg.reset_building_editor_defaults"
    bl_label = "Reset Editor Defaults"
    bl_description = (
        "Restore the building editor mode and new-building fields without "
        "changing the generated map or existing corrections"
    )
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context: bpy.types.Context) -> set[str]:
        settings = context.scene.ovmg_settings
        settings.building_editor_mode = "SIMPLE"
        settings.building_editor_add_preset = "RESIDENTIAL"
        # The enum update applies the residential preset. Assign explicitly as
        # well so the reset remains reliable if the same preset was already active.
        settings.user_building_shape = "BOX"
        settings.user_building_roof_shape = "FLAT"
        settings.user_building_width = 18.0
        settings.user_building_depth = 16.0
        settings.user_building_height = 12.0
        settings.user_building_base_height = 0.0
        settings.user_building_roof_height = 0.0
        settings.user_building_rotation = 0.0
        settings.user_building_facade_profile = "generic_plaster"
        settings.user_building_facade_color = (0.72, 0.61, 0.45, 1.0)
        settings.user_building_roof_color = (0.38, 0.30, 0.22, 1.0)
        settings.user_building_name = "Residential Building"
        settings.correction_status = (
            "Building Editor values restored. Existing map corrections were not changed."
        )
        self.report({"INFO"}, "Building Editor defaults restored.")
        _tag_view3d_redraw()
        return {"FINISHED"}


class OVMG_OT_MakeInspectedBuildingEditable(bpy.types.Operator):
    """Create or select an editable replacement for the picked source building."""

    bl_idname = "ovmg.make_inspected_building_editable"
    bl_label = "Make Building Editable"
    bl_description = "Create a separate editable replacement and hide the generated source on apply"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context: bpy.types.Context) -> set[str]:
        settings = context.scene.ovmg_settings
        if not settings.inspector_source_id:
            self.report({"WARNING"}, "Pick a generated building first.")
            return {"CANCELLED"}
        result = bpy.ops.ovmg.create_building_replacement()
        if "FINISHED" not in result:
            return {"CANCELLED"}
        settings.correction_status = (
            "Editable replacement created. Adjust it and use Apply All Changes "
            "to remove the original source geometry."
        )
        self.report({"INFO"}, settings.correction_status)
        return {"FINISHED"}


class _ViewportClickOperator:
    """Shared modal behavior for simple building selection and placement."""

    _window: bpy.types.Window | None = None
    _area: bpy.types.Area | None = None
    _region: bpy.types.Region | None = None

    def _begin(self, context: bpy.types.Context, message: str) -> set[str]:
        if context.area is None or context.area.type != "VIEW_3D":
            self.report({"WARNING"}, "Run this tool from a 3D Viewport.")
            return {"CANCELLED"}
        self._window = context.window
        self._area = context.area
        self._region = next(
            (region for region in context.area.regions if region.type == "WINDOW"),
            context.region,
        )
        context.window_manager.modal_handler_add(self)
        context.window.cursor_modal_set("CROSSHAIR")
        context.workspace.status_text_set(message)
        return {"RUNNING_MODAL"}

    def _finish(self, context: bpy.types.Context) -> None:
        context.window.cursor_modal_restore()
        context.workspace.status_text_set(None)
        _tag_view3d_redraw()

    def _cancel(self, context: bpy.types.Context) -> set[str]:
        self._finish(context)
        return {"CANCELLED"}


class OVMG_OT_PickBuildingInteractive(_ViewportClickOperator, bpy.types.Operator):
    """Pick a generated or editable building directly in the 3D viewport."""

    bl_idname = "ovmg.pick_building_interactive"
    bl_label = "Pick Building"
    bl_description = "Click a visible building in the viewport; no 3D Cursor setup is required"
    bl_options = {"REGISTER"}

    def invoke(
        self,
        context: bpy.types.Context,
        _event: bpy.types.Event,
    ) -> set[str]:
        return self._begin(
            context,
            "Click a building to edit. Right-click or Esc to cancel.",
        )

    def modal(
        self,
        context: bpy.types.Context,
        event: bpy.types.Event,
    ) -> set[str]:
        if event.type in {"ESC", "RIGHTMOUSE"}:
            return self._cancel(context)
        if event.type != "LEFTMOUSE" or event.value != "PRESS":
            return {"RUNNING_MODAL"}

        hit, location, hit_object = _ray_cast_from_event(context, event)
        if not hit or location is None:
            self.report({"WARNING"}, "No map geometry was found under the mouse.")
            return {"RUNNING_MODAL"}

        root = root_user_building(hit_object)
        if root is not None:
            for obj in context.selected_objects:
                obj.select_set(False)
            root.select_set(True)
            context.view_layer.objects.active = root
            bpy.ops.ovmg.load_selected_user_building()
            context.scene.ovmg_settings.correction_status = (
                f"Editable building selected: {root.name}"
            )
        else:
            for obj in context.selected_objects:
                obj.select_set(False)
            context.view_layer.objects.active = None
            context.scene.cursor.location = location
            inspected = bpy.ops.ovmg.inspect_nearest_building()
            if "FINISHED" not in inspected:
                self.report({"WARNING"}, "No generated building metadata was found here.")
                return {"RUNNING_MODAL"}
            bpy.ops.ovmg.load_inspected_building()

        self._finish(context)
        # The original editor dialog remains open while this modal picker runs.
        # Reopening it created a duplicate nested dialog, so only redraw the UI.
        _tag_view3d_redraw()
        return {"FINISHED"}


class OVMG_OT_PlaceBuildingInteractive(_ViewportClickOperator, bpy.types.Operator):
    """Place a missing building with one click on the map surface."""

    bl_idname = "ovmg.place_building_interactive"
    bl_label = "Place New Building"
    bl_description = "Click the map to place a missing building using the selected preset"
    bl_options = {"REGISTER", "UNDO"}

    def invoke(
        self,
        context: bpy.types.Context,
        _event: bpy.types.Event,
    ) -> set[str]:
        return self._begin(
            context,
            "Click the map where the new building should be placed. Right-click or Esc to cancel.",
        )

    def modal(
        self,
        context: bpy.types.Context,
        event: bpy.types.Event,
    ) -> set[str]:
        if event.type in {"ESC", "RIGHTMOUSE"}:
            return self._cancel(context)
        if event.type != "LEFTMOUSE" or event.value != "PRESS":
            return {"RUNNING_MODAL"}

        hit, location, _hit_object = _ray_cast_from_event(context, event)
        if not hit or location is None:
            self.report({"WARNING"}, "Click on visible map geometry to place the building.")
            return {"RUNNING_MODAL"}

        context.scene.cursor.location = location
        added = bpy.ops.ovmg.add_user_building()
        if "FINISHED" not in added:
            self.report({"ERROR"}, "The building could not be created.")
            return self._cancel(context)

        self._finish(context)
        # The original editor dialog remains open while this modal picker runs.
        # Reopening it created a duplicate nested dialog, so only redraw the UI.
        _tag_view3d_redraw()
        return {"FINISHED"}
