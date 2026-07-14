"""Blender operators for generation, export, and cleanup."""

from __future__ import annotations

from pathlib import Path
import json
from math import hypot
import traceback
import unicodedata
import webbrowser

import bpy
from bpy.props import StringProperty

from ..application.factory import ApplicationFactory
from ..core.constants import ADDON_ID, ADDON_PACKAGE
from ..core.exceptions import NetworkAccessError, OVMGError, ValidationError
from ..core.naming import building_metadata_name, category_collection_name, sanitize_name
from ..domain.enums import (
    BuildingSource,
    ExportFormat,
    FeatureType,
    GeometryStyle,
    InputMode,
    LabelLanguage,
    LabelMode,
    LargeAreaMode,
    MaterialStyle,
    ModelStyle,
    QualityPreset,
)
from ..domain.models import BoundingBox, MapBuildRequest, VoxelSettings
from ..infrastructure.blender.area_preview import BlenderAreaPreviewService
from ..infrastructure.blender.building_corrections import (
    BuildingCorrectionStore,
    BuildingMetadataReader,
    mark_user_building,
)
from ..infrastructure.blender.editable_buildings import (
    assign_building_material,
    building_hierarchy,
    create_editable_building,
    curve_world_rings,
    primitive_footprint,
    rebuild_editable_building,
    remove_building_hierarchy,
    root_user_building,
)
from ..infrastructure.blender.export_service import ProjectExportService
from ..infrastructure.blender.selection_thumbnail import (
    BlenderSelectionThumbnailService,
)
from ..infrastructure.geocoding.nominatim import NominatimGeocoder
from ..infrastructure.map_selector.history import AreaHistoryStore
from ..infrastructure.map_selector.metrics import (
    AreaLoadLevel,
    AreaMetricsCalculator,
    quality_generation_multiplier,
)
from ..infrastructure.map_selector.session import (
    MapSelectorConfig,
    MapSelectorSessionManager,
)
from ..infrastructure.network.http_client import JsonHttpClient
from ..infrastructure.overture.runtime import refresh_overture_runtime_probe
from .properties import restore_recommended_generation_defaults


class _PreferenceAccess:
    """Resolve preferences under legacy and extension package namespaces."""

    @staticmethod
    def get(context: bpy.types.Context) -> object:
        addon = context.preferences.addons.get(ADDON_PACKAGE)
        if addon is None:
            addon = context.preferences.addons.get(ADDON_ID)
        if addon is None:
            raise OVMGError("Open Voxel Map Generator preferences are unavailable.")
        return addon.preferences


class OVMG_OT_UseOsmOnly(bpy.types.Operator):
    """Switch building retrieval to the dependency-free OSM source."""

    bl_idname = "ovmg.use_osm_only"
    bl_label = "Use OSM Buildings Only"
    bl_description = (
        "Continue without the optional Overture runtime. Building coverage may "
        "be lower in areas where OSM footprints are incomplete"
    )
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context: bpy.types.Context) -> set[str]:
        settings = context.scene.ovmg_settings
        settings.building_source = "OSM_ONLY"
        settings.last_summary = (
            "Building source changed to OpenStreetMap only. Generation can "
            "continue without PyArrow or the Overture Python client."
        )
        self.report({"INFO"}, settings.last_summary)
        return {"FINISHED"}


class OVMG_OT_EnableMapTiling(bpy.types.Operator):
    """Allow an oversized selection to generate as aligned map tiles."""

    bl_idname = "ovmg.enable_map_tiling"
    bl_label = "Split Into Tiles"
    bl_description = (
        "Divide the selected area into safe aligned tiles and assemble them "
        "inside one generated map project"
    )
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context: bpy.types.Context) -> set[str]:
        settings = context.scene.ovmg_settings
        settings.large_area_mode = "SPLIT_TILES"
        settings.status = "Map tiling enabled"
        settings.last_summary = (
            "The selected area will be generated as aligned tiles in one project."
        )
        self.report({"INFO"}, settings.last_summary)
        return {"FINISHED"}


class OVMG_OT_CopyOvertureDiagnostics(bpy.types.Operator):
    """Recheck the Overture dependency stack and copy a complete report."""

    bl_idname = "ovmg.copy_overture_diagnostics"
    bl_label = "Copy Runtime Diagnostics"
    bl_description = (
        "Recheck NumPy, PyArrow, Shapely, and overturemaps, then copy the full "
        "diagnostic report to the clipboard"
    )

    def execute(self, context: bpy.types.Context) -> set[str]:
        status = refresh_overture_runtime_probe()
        context.window_manager.clipboard = status.diagnostics
        settings = context.scene.ovmg_settings
        settings.last_summary = status.summary
        if status.available:
            self.report({"INFO"}, "Overture runtime is ready; diagnostics copied.")
        else:
            self.report({"WARNING"}, "Overture diagnostics copied to clipboard.")
        return {"FINISHED"}


class OVMG_OT_AboutAddon(bpy.types.Operator):
    """Show a compact add-on description and creator link."""

    bl_idname = "ovmg.about_addon"
    bl_label = "About OVMG"
    bl_description = "About Open Voxel Map Generator"

    def invoke(self, context: bpy.types.Context, _event: bpy.types.Event) -> set[str]:
        return context.window_manager.invoke_props_dialog(self, width=430)

    def draw(self, _context: bpy.types.Context) -> None:
        layout = self.layout
        layout.label(text="Open Voxel Map Generator", icon="WORLD_DATA")
        layout.label(text="Converts selected real-world GIS areas into optimized 3D maps.")
        layout.label(text="Includes buildings, roads, terrain, water, green areas, and trees.")
        layout.separator()
        layout.label(text="Created by Ammar Salim")
        link = layout.operator("wm.url_open", text="Instagram — @ammar_salim_d", icon="URL")
        link.url = "https://www.instagram.com/ammar_salim_d/"

    def execute(self, _context: bpy.types.Context) -> set[str]:
        return {"FINISHED"}


def _redraw_viewports() -> None:
    """Request redraws without accessing Blender data from background threads."""
    window_manager = getattr(bpy.context, "window_manager", None)
    if window_manager is None:
        return
    for window in window_manager.windows:
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()


def _english_area_name(value: str, bounds: BoundingBox | None = None) -> str:
    """Keep Blender labels readable when its UI cannot shape Arabic text."""
    cleaned = str(value).strip()
    has_rtl = any(
        unicodedata.bidirectional(char) in {"R", "AL", "AN"}
        or "\ufb1d" <= char <= "\ufeff"
        for char in cleaned
    )
    if cleaned and not has_rtl:
        return cleaned
    if bounds is not None:
        center = bounds.center
        return f"Selected Map Area — {center.latitude:.5f}, {center.longitude:.5f}"
    return "Selected Map Area"


def _pending_bounds(settings: object) -> BoundingBox:
    """Return validated pending bounds received from the browser."""
    bounds = BoundingBox(
        south=float(settings.pending_south),
        west=float(settings.pending_west),
        north=float(settings.pending_north),
        east=float(settings.pending_east),
    )
    bounds.validate()
    return bounds


def _clear_pending_fields(settings: object) -> None:
    """Clear pending coordinates while leaving a loaded preview alive briefly."""
    settings.visual_selection_pending = False
    settings.pending_area_name = ""
    settings.pending_south = 0.0
    settings.pending_west = 0.0
    settings.pending_north = 0.0
    settings.pending_east = 0.0


def _clear_pending_selection(settings: object) -> None:
    """Clear pending browser data and release its staged preview."""
    BlenderSelectionThumbnailService.remove(settings)
    _clear_pending_fields(settings)


def _poll_map_selector_result() -> float | None:
    """Consume browser proposals on Blender's main thread without applying them."""
    result = MapSelectorSessionManager.consume_result()
    if result is None:
        return 0.25 if MapSelectorSessionManager.has_active_session() else None

    scene = bpy.data.scenes.get(result.scene_name)
    if scene is None or not hasattr(scene, "ovmg_settings"):
        MapSelectorSessionManager.stop()
        return None
    settings = scene.ovmg_settings
    if result.cancelled or result.bounds is None:
        _clear_pending_selection(settings)
        settings.status = "Area selection cancelled"
        settings.last_summary = "No geographic bounds were changed."
        MapSelectorSessionManager.stop()
        _redraw_viewports()
        return None

    settings.pending_south = result.bounds.south
    settings.pending_west = result.bounds.west
    settings.pending_north = result.bounds.north
    settings.pending_east = result.bounds.east
    settings.pending_area_name = _english_area_name(result.display_name, result.bounds)
    settings.visual_selection_pending = True

    try:
        preferences = _PreferenceAccess.get(bpy.context)
        metrics = AreaMetricsCalculator.calculate(
            result.bounds,
            float(settings.voxel_size),
            int(preferences.max_voxel_cells),
            quality_generation_multiplier(str(settings.quality_preset)),
        )
        BlenderSelectionThumbnailService.stage(
            settings,
            result.thumbnail_bytes,
            result.thumbnail_mime_type,
        )
        settings.status = "Area preview received — click Review and Confirm"
        settings.last_summary = (
            f"{metrics.width_km:.2f} × {metrics.height_km:.2f} km • "
            f"{metrics.area_square_km:.2f} km² • "
            f"{metrics.load_level.value.replace('_', ' ').title()} load"
        )
        _redraw_viewports()
    except Exception as exc:
        traceback.print_exc()
        _clear_pending_selection(settings)
        settings.status = f"Error: {exc}"
        settings.last_summary = "The proposed browser bounds could not be staged safely."
        _redraw_viewports()

    return 0.25 if MapSelectorSessionManager.has_active_session() else None


class OVMG_OT_SelectAreaOnMap(bpy.types.Operator):
    """Open the secure localhost visual rectangle selector in a web browser."""

    bl_idname = "ovmg.select_area_on_map"
    bl_label = "Select Area on Map"
    bl_description = "Draw and edit the exact geographic generation rectangle"
    bl_options = {"REGISTER"}

    def execute(self, context: bpy.types.Context) -> set[str]:
        settings = context.scene.ovmg_settings
        try:
            if hasattr(bpy.app, "online_access") and not bpy.app.online_access:
                raise NetworkAccessError(
                    "Blender Online Access is disabled. Enable it in Preferences "
                    "before opening the visual map selector."
                )
            preferences = _PreferenceAccess.get(context)
            _clear_pending_selection(settings)
            bounds = BoundingBox(
                south=float(settings.south),
                west=float(settings.west),
                north=float(settings.north),
                east=float(settings.east),
            )
            if (
                not settings.visual_area_selected
                and len(settings.area_name.strip()) >= 3
            ):
                client = JsonHttpClient(
                    user_agent=str(preferences.user_agent),
                    timeout_seconds=int(preferences.network_timeout),
                    retry_count=int(preferences.network_retries),
                )
                bounds = NominatimGeocoder(
                    str(preferences.nominatim_endpoint),
                    client,
                ).resolve(str(settings.area_name))

            url = MapSelectorSessionManager.start(
                MapSelectorConfig(
                    scene_name=context.scene.name,
                    area_name=str(settings.area_name),
                    bounds=bounds,
                    voxel_size=float(settings.voxel_size),
                    max_voxel_cells=int(preferences.max_voxel_cells),
                    quality_name=str(settings.quality_preset).replace("_", " ").title(),
                    nominatim_endpoint=str(preferences.nominatim_endpoint),
                    user_agent=str(preferences.user_agent),
                    network_timeout=int(preferences.network_timeout),
                    split_large_area=str(settings.large_area_mode) == "SPLIT_TILES",
                )
            )
            opened = webbrowser.open(url, new=2, autoraise=True)
            if not opened:
                context.window_manager.clipboard = url
                settings.last_summary = (
                    "Browser launch was blocked; the selector URL was copied "
                    "to the clipboard."
                )
            else:
                settings.last_summary = (
                    "Draw, move, or resize the rectangle, then choose "
                    "Send Preview to Blender."
                )
            settings.status = "Visual selector opened"
            if not bpy.app.timers.is_registered(_poll_map_selector_result):
                bpy.app.timers.register(
                    _poll_map_selector_result,
                    first_interval=0.25,
                )
            self.report({"INFO"}, settings.last_summary)
            return {"FINISHED"}
        except OVMGError as exc:
            settings.status = f"Error: {exc}"
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        except Exception as exc:
            traceback.print_exc()
            settings.status = f"Unexpected selector error: {exc}"
            self.report({"ERROR"}, settings.status)
            return {"CANCELLED"}


class OVMG_OT_ConfirmAreaSelection(bpy.types.Operator):
    """Show and confirm the browser proposal before changing active bounds."""

    bl_idname = "ovmg.confirm_area_selection"
    bl_label = "Confirm Selected Area"
    bl_description = "Review the map thumbnail and apply the proposed bounds"
    bl_options = {"REGISTER", "INTERNAL"}

    def invoke(
        self,
        context: bpy.types.Context,
        _event: bpy.types.Event,
    ) -> set[str]:
        settings = context.scene.ovmg_settings
        if not settings.visual_selection_pending:
            self.report({"WARNING"}, "No browser area is waiting for confirmation.")
            return {"CANCELLED"}
        self._preview_icon_id = BlenderSelectionThumbnailService.prepare_for_ui(
            settings
        )
        return context.window_manager.invoke_props_dialog(
            self,
            width=560,
            title="Confirm Selected Area",
            confirm_text="Confirm Selection",
        )

    def draw(self, context: bpy.types.Context) -> None:
        settings = context.scene.ovmg_settings
        layout = self.layout
        layout.label(
            text="Review this area before it replaces the current selection.",
            icon="INFO",
        )
        icon_id = int(getattr(self, "_preview_icon_id", 0))
        if icon_id:
            preview = layout.box()
            preview.template_icon(icon_value=icon_id, scale=16.0)
        else:
            layout.label(
                text="Map thumbnail unavailable; review the measurements below.",
                icon="INFO",
            )
            layout.operator(
                "ovmg.open_pending_preview",
                text="Open Preview in Image Viewer",
                icon="IMAGE_DATA",
            )

        try:
            bounds = _pending_bounds(settings)
            preferences = _PreferenceAccess.get(context)
            metrics = AreaMetricsCalculator.calculate(
                bounds,
                float(settings.voxel_size),
                int(preferences.max_voxel_cells),
                quality_generation_multiplier(str(settings.quality_preset)),
            )
            details = layout.box()
            if settings.pending_area_name:
                details.label(text=str(settings.pending_area_name), icon="WORLD_DATA")
            details.label(
                text=f"Size: {metrics.width_km:.2f} × {metrics.height_km:.2f} km"
            )
            details.label(text=f"Area: {metrics.area_square_km:.2f} km²")
            details.label(text=f"Quality: {str(settings.quality_preset).title()}")
            load = metrics.load_level.value.replace("_", " ").title()
            details.label(text=f"Estimated Load: {load}")
            if metrics.load_level.value == "TOO_LARGE":
                warning = layout.box()
                warning.alert = True
                warning.label(
                    text="This selection is too large for the current quality preset.",
                    icon="CANCEL",
                )
                warning.label(text="Make the map smaller or choose a lower quality.")
        except OVMGError as exc:
            error = layout.box()
            error.alert = True
            error.label(text=str(exc), icon="ERROR")

        layout.separator()
        layout.label(
            text="Confirm applies it. Cancel returns to the map for more editing.",
            icon="MOUSE_MOVE",
        )

    def execute(self, context: bpy.types.Context) -> set[str]:
        settings = context.scene.ovmg_settings
        try:
            bounds = _pending_bounds(settings)
            preferences = _PreferenceAccess.get(context)
            metrics = AreaMetricsCalculator.calculate(
                bounds,
                float(settings.voxel_size),
                int(preferences.max_voxel_cells),
                quality_generation_multiplier(str(settings.quality_preset)),
            )
            settings.south = bounds.south
            settings.west = bounds.west
            settings.north = bounds.north
            settings.east = bounds.east
            settings.input_mode = "VISUAL_MAP"
            settings.visual_area_selected = True
            if settings.pending_area_name.strip():
                settings.area_name = _english_area_name(
                    settings.pending_area_name,
                    bounds,
                )
            if str(settings.project_name) in {
                "Voxel_Map_Project",
                "Adhamiya_Voxel_Map",
            }:
                short_name = str(settings.area_name).split(",", 1)[0].strip()
                settings.project_name = (
                    f"{sanitize_name(short_name, fallback='Selected_Area')}_Voxel_Map"
                )
            BlenderAreaPreviewService.create(
                context.scene,
                bounds,
                float(settings.voxel_size),
                int(preferences.max_voxel_cells),
            )
            saved = AreaHistoryStore.add(
                preferences,
                str(settings.area_name),
                bounds,
            )
            settings.previous_area_key = saved.key
            settings.wizard_step = 2
            settings.status = "Visual area confirmed"
            settings.last_summary = (
                f"{metrics.width_km:.2f} × {metrics.height_km:.2f} km • "
                f"{metrics.area_square_km:.2f} km² • "
                f"{metrics.load_level.value.replace('_', ' ').title()} load"
            )
            _clear_pending_fields(settings)
            MapSelectorSessionManager.stop()
            _redraw_viewports()
            self.report({"INFO"}, "Selected map area confirmed.")
            return {"FINISHED"}
        except OVMGError as exc:
            settings.status = f"Error: {exc}"
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}

    def cancel(self, context: bpy.types.Context) -> None:
        settings = context.scene.ovmg_settings
        settings.status = "Selection not confirmed"
        settings.last_summary = (
            "Return to the browser, resize or move the rectangle, then send it again."
        )
        url = MapSelectorSessionManager.active_url()
        if url:
            webbrowser.open(url, new=0, autoraise=True)
        _redraw_viewports()


class OVMG_OT_OpenPendingPreview(bpy.types.Operator):
    """Open the staged map thumbnail in the operating system image viewer."""

    bl_idname = "ovmg.open_pending_preview"
    bl_label = "Open Pending Preview"
    bl_description = "Open the staged map thumbnail outside Blender"
    bl_options = {"REGISTER", "INTERNAL"}

    def execute(self, context: bpy.types.Context) -> set[str]:
        settings = context.scene.ovmg_settings
        if BlenderSelectionThumbnailService.open_external(settings):
            return {"FINISHED"}
        self.report({"WARNING"}, "The staged map preview is unavailable.")
        return {"CANCELLED"}


class OVMG_OT_EditPendingArea(bpy.types.Operator):
    """Return to the active browser selector without applying pending bounds."""

    bl_idname = "ovmg.edit_pending_area"
    bl_label = "Back to Map"
    bl_description = "Resize or move the pending rectangle in the browser"
    bl_options = {"REGISTER"}

    def execute(self, context: bpy.types.Context) -> set[str]:
        settings = context.scene.ovmg_settings
        url = MapSelectorSessionManager.active_url()
        if not url:
            self.report({"WARNING"}, "The browser selector session has expired.")
            return {"CANCELLED"}
        opened = webbrowser.open(url, new=0, autoraise=True)
        if not opened:
            context.window_manager.clipboard = url
            self.report({"INFO"}, "Selector URL copied to the clipboard.")
        settings.status = "Editing proposed area in browser"
        settings.last_summary = (
            "Resize, move, or redraw the rectangle and send it again."
        )
        return {"FINISHED"}


class OVMG_OT_DiscardPendingArea(bpy.types.Operator):
    """Discard an unconfirmed browser proposal and retain active bounds."""

    bl_idname = "ovmg.discard_pending_area"
    bl_label = "Discard Proposal"
    bl_description = "Discard the unconfirmed area without changing active bounds"
    bl_options = {"REGISTER"}

    def execute(self, context: bpy.types.Context) -> set[str]:
        settings = context.scene.ovmg_settings
        _clear_pending_selection(settings)
        MapSelectorSessionManager.stop()
        settings.status = "Area proposal discarded"
        settings.last_summary = "The previously confirmed area was not changed."
        _redraw_viewports()
        return {"FINISHED"}


class OVMG_OT_ClearAreaSelection(bpy.types.Operator):
    """Clear the visual selection and its temporary Blender preview."""

    bl_idname = "ovmg.clear_area_selection"
    bl_label = "Clear"
    bl_description = "Clear selected visual bounds and remove the preview outline"
    bl_options = {"REGISTER"}

    def execute(self, context: bpy.types.Context) -> set[str]:
        settings = context.scene.ovmg_settings
        BlenderAreaPreviewService.remove(context.scene)
        _clear_pending_selection(settings)
        MapSelectorSessionManager.stop()
        settings.visual_area_selected = False
        settings.wizard_step = 1
        settings.status = "Visual area cleared"
        settings.last_summary = "Open the visual selector to draw a new rectangle."
        return {"FINISHED"}


class OVMG_OT_FrameAreaPreview(bpy.types.Operator):
    """Frame the temporary selected-area outline in the active 3D View."""

    bl_idname = "ovmg.frame_area_preview"
    bl_label = "Frame Preview"
    bl_description = "Frame the selected geographic rectangle in the 3D View"
    bl_options = {"REGISTER"}

    def execute(self, context: bpy.types.Context) -> set[str]:
        outline = BlenderAreaPreviewService.find_outline()
        if outline is None:
            self.report({"WARNING"}, "No selected-area preview exists.")
            return {"CANCELLED"}
        for obj in context.selected_objects:
            obj.select_set(False)
        outline.hide_viewport = False
        outline.select_set(True)
        context.view_layer.objects.active = outline
        if context.area is not None and context.area.type == "VIEW_3D":
            bpy.ops.view3d.view_selected(use_all_regions=False)
        return {"FINISHED"}


class OVMG_OT_UsePreviousArea(bpy.types.Operator):
    """Load one automatically saved visual-map rectangle."""

    bl_idname = "ovmg.use_previous_area"
    bl_label = "Use Previous Area"
    bl_description = "Load the selected previous rectangle and review it"
    bl_options = {"REGISTER"}

    def execute(self, context: bpy.types.Context) -> set[str]:
        settings = context.scene.ovmg_settings
        preferences = _PreferenceAccess.get(context)
        key = str(settings.previous_area_key)
        if not key or key == "NONE":
            self.report({"WARNING"}, "No previous map area is available.")
            return {"CANCELLED"}
        entry = AreaHistoryStore.find(preferences, key)
        if entry is None:
            self.report({"ERROR"}, "The selected previous area could not be loaded.")
            return {"CANCELLED"}
        bounds = entry.bounds
        settings.area_name = entry.name
        settings.input_mode = "VISUAL_MAP"
        settings.south = bounds.south
        settings.west = bounds.west
        settings.north = bounds.north
        settings.east = bounds.east
        settings.visual_area_selected = True
        settings.visual_selection_pending = False
        settings.wizard_step = 2
        BlenderAreaPreviewService.create(
            context.scene,
            bounds,
            float(settings.voxel_size),
            int(preferences.max_voxel_cells),
        )
        metrics = AreaMetricsCalculator.calculate(
            bounds,
            float(settings.voxel_size),
            int(preferences.max_voxel_cells),
            quality_generation_multiplier(str(settings.quality_preset)),
        )
        settings.status = "Previous area loaded"
        settings.last_summary = (
            f"{metrics.width_km:.2f} × {metrics.height_km:.2f} km • "
            f"{metrics.area_square_km:.2f} km²"
        )
        self.report({"INFO"}, f"Loaded previous area: {entry.name}")
        return {"FINISHED"}


class OVMG_OT_WizardNext(bpy.types.Operator):
    """Advance one validated step in the setup wizard."""

    bl_idname = "ovmg.wizard_next"
    bl_label = "Next"
    bl_description = "Continue to the next setup step"
    bl_options = {"REGISTER", "INTERNAL"}

    def execute(self, context: bpy.types.Context) -> set[str]:
        settings = context.scene.ovmg_settings
        step = int(settings.wizard_step)
        if step in {1, 2}:
            if settings.visual_selection_pending:
                self.report({"WARNING"}, "Review and confirm the proposed area first.")
                return {"CANCELLED"}
            if not settings.visual_area_selected:
                self.report({"WARNING"}, "Choose or load a map area first.")
                return {"CANCELLED"}
        if step >= 6:
            return {"CANCELLED"}
        settings.wizard_step = min(6, step + 1)
        settings.status = f"Setup step {settings.wizard_step} of 6"
        return {"FINISHED"}


class OVMG_OT_WizardBack(bpy.types.Operator):
    """Return to the previous setup step without losing selections."""

    bl_idname = "ovmg.wizard_back"
    bl_label = "Back"
    bl_description = "Return to the previous setup step"
    bl_options = {"REGISTER", "INTERNAL"}

    def execute(self, context: bpy.types.Context) -> set[str]:
        settings = context.scene.ovmg_settings
        if int(settings.wizard_step) <= 1:
            return {"CANCELLED"}
        settings.wizard_step = max(1, int(settings.wizard_step) - 1)
        settings.status = f"Setup step {settings.wizard_step} of 6"
        return {"FINISHED"}


class OVMG_OT_EditGenerationSettings(bpy.types.Operator):
    """Return to style selection for a controlled regeneration."""

    bl_idname = "ovmg.edit_generation_settings"
    bl_label = "Change Settings / Regenerate"
    bl_description = "Return to style, quality, and material choices"
    bl_options = {"REGISTER"}

    def execute(self, context: bpy.types.Context) -> set[str]:
        settings = context.scene.ovmg_settings
        settings.wizard_step = 3
        settings.status = "Edit generation settings"
        settings.last_summary = (
            "The current map remains visible until Generate Map is run again."
        )
        return {"FINISHED"}


class OVMG_OT_RestoreRecommendedDefaults(bpy.types.Operator):
    """Restore safe generation settings without changing the selected area."""

    bl_idname = "ovmg.restore_recommended_defaults"
    bl_label = "Restore Recommended Defaults"
    bl_description = (
        "Reset quality, building, material, layer, label, and export settings "
        "while preserving the selected map area and existing scene"
    )
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context: bpy.types.Context) -> set[str]:
        settings = context.scene.ovmg_settings
        restore_recommended_generation_defaults(settings)
        settings.status = "Recommended generation settings restored"
        settings.last_summary = (
            "Recommended defaults restored. The selected area and generated map "
            "were not deleted. Approximate buildings and landmark proxies are off."
        )
        self.report({"INFO"}, "Recommended OVMG settings restored.")
        return {"FINISHED"}


class OVMG_OT_WizardRestart(bpy.types.Operator):
    """Start configuring another project while leaving existing maps intact."""

    bl_idname = "ovmg.wizard_restart"
    bl_label = "Start New Map"
    bl_description = "Return to area selection; existing generated projects remain"
    bl_options = {"REGISTER"}

    def execute(self, context: bpy.types.Context) -> set[str]:
        settings = context.scene.ovmg_settings
        restore_recommended_generation_defaults(settings)
        settings.settings_schema_version = 186
        settings.wizard_step = 1
        settings.generated_map_ready = False
        settings.status = "Choose a map area"
        settings.last_summary = "Existing generated maps remain in the scene."
        return {"FINISHED"}


def _user_building_collection(
    scene: bpy.types.Scene,
    project_name: str,
) -> bpy.types.Collection:
    """Return the persistent collection for manually created buildings."""
    name = category_collection_name(f"User_Buildings_{project_name}")
    collection = bpy.data.collections.get(name)
    if collection is None:
        collection = bpy.data.collections.new(name)
        scene.collection.children.link(collection)
    collection["ovmg_user_buildings"] = True
    collection["ovmg_project"] = project_name
    return collection


def _select_only(context: bpy.types.Context, obj: bpy.types.Object) -> None:
    """Select one object and make it active without relying on global context."""
    for current in context.selected_objects:
        current.select_set(False)
    obj.hide_set(False)
    obj.select_set(True)
    context.view_layer.objects.active = obj


def _hex_rgba(value: str, fallback: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    """Convert an optional six-digit metadata color to Blender RGBA."""
    token = str(value or "").strip().lstrip("#")
    if len(token) != 6:
        return fallback
    try:
        return (
            int(token[0:2], 16) / 255.0,
            int(token[2:4], 16) / 255.0,
            int(token[4:6], 16) / 255.0,
            1.0,
        )
    except ValueError:
        return fallback


def _next_building_name(settings: object, prefix: str = "UserBuilding") -> str:
    """Return a stable unique editable-building object name."""
    project = str(settings.project_name)
    count = 1 + sum(
        1
        for obj in bpy.data.objects
        if obj.get("ovmg_user_building")
        and obj.get("ovmg_project") == project
        and obj.get("ovmg_user_building_root")
    )
    readable = sanitize_name(str(settings.user_building_name), fallback=prefix)
    return f"OVMG_{readable}_{sanitize_name(project)}_{count:04d}"


def _related_building_source_ids(project_name: str, source_id: str) -> tuple[str, ...]:
    """Return one source id plus rendered building parts linked to its parent id."""
    record = BuildingMetadataReader.find(project_name, source_id)
    if record is None or bool(record.get("is_building_part", False)):
        return (source_id,) if source_id else ()
    parent_token = source_id.rsplit("/", 1)[-1]
    related = {source_id}
    for candidate in BuildingMetadataReader.all(project_name):
        if str(candidate.get("parent_source_id", "")) == parent_token:
            candidate_id = str(candidate.get("source_id", ""))
            if candidate_id:
                related.add(candidate_id)
    return tuple(sorted(related))


def _create_editor_building(
    context: bpy.types.Context,
    outer_rings: object,
    inner_rings: object,
    base_z: float,
    source_id: str = "",
    correction_kind: str = "ADDED",
    prefix: str = "UserBuilding",
) -> bpy.types.Object:
    """Create one editable building from the current correction-studio values."""
    settings = context.scene.ovmg_settings
    collection = _user_building_collection(context.scene, str(settings.project_name))
    roof_shape = str(settings.user_building_roof_shape)
    if str(settings.user_building_shape) == "DOME" and roof_shape == "FLAT":
        roof_shape = "DOME"
    quality = QualityPreset(str(settings.quality_preset))
    if quality is QualityPreset.CUSTOM:
        quality = QualityPreset.MEDIUM
    obj = create_editable_building(
        name=_next_building_name(settings, prefix),
        outer_rings=outer_rings,
        inner_rings=inner_rings,
        base_z=float(base_z),
        total_height=float(settings.user_building_height),
        roof_shape=roof_shape,
        roof_height=float(settings.user_building_roof_height),
        rotation_z=float(settings.user_building_rotation),
        collection=collection,
        project_name=str(settings.project_name),
        source_id=source_id,
        correction_kind=correction_kind,
        profile=str(settings.user_building_facade_profile),
        facade_color=tuple(settings.user_building_facade_color),
        roof_color=tuple(settings.user_building_roof_color),
        realistic_material=str(settings.material_style) == "REALISTIC",
        quality=quality,
        roof_segments=max(8, int(settings.curved_detail_segments)),
    )
    obj["ovmg_building_name"] = str(settings.user_building_name)
    obj["ovmg_editor_width_m"] = float(settings.user_building_width)
    obj["ovmg_editor_depth_m"] = float(settings.user_building_depth)
    obj["ovmg_editor_total_height_m"] = float(settings.user_building_height)
    obj["ovmg_editor_base_height_m"] = float(base_z)
    obj["ovmg_editor_roof_height_m"] = float(settings.user_building_roof_height)
    obj["ovmg_editor_roof_shape"] = roof_shape
    obj.show_wire = True
    obj.show_all_edges = True
    _select_only(context, obj)
    return obj


class OVMG_OT_AddUserBuilding(bpy.types.Operator):
    """Create one persistent editable building at the 3D cursor."""

    bl_idname = "ovmg.add_user_building"
    bl_label = "Add Building at 3D Cursor"
    bl_description = "Create a persistent editable building at the 3D Cursor"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context: bpy.types.Context) -> set[str]:
        settings = context.scene.ovmg_settings
        segments = 8 if str(settings.model_style) in {"MINECRAFT", "LOW_POLY"} else 24
        ring = primitive_footprint(
            str(settings.user_building_shape),
            float(settings.user_building_width),
            float(settings.user_building_depth),
            segments,
        )
        cursor = context.scene.cursor.location
        world_ring = tuple((x + cursor.x, y + cursor.y) for x, y in ring)
        obj = _create_editor_building(
            context,
            (world_ring,),
            (),
            cursor.z + float(settings.user_building_base_height),
        )
        BuildingCorrectionStore.register_added_object(
            str(settings.project_name), obj.name
        )
        settings.status = "Editable building added"
        settings.correction_status = (
            "The new building is separate from generated chunks and is included in export."
        )
        settings.last_summary = "Use Load Selected and Apply Changes for numeric editing."
        self.report({"INFO"}, settings.last_summary)
        return {"FINISHED"}


class OVMG_OT_AddBuildingFromCurve(bpy.types.Operator):
    """Extrude a closed Blender Curve into a persistent editable building."""

    bl_idname = "ovmg.add_building_from_curve"
    bl_label = "Build from Selected Curve"
    bl_description = "Use the selected closed Curve as an exact custom building footprint"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context: bpy.types.Context) -> set[str]:
        source = context.view_layer.objects.active
        try:
            rings = curve_world_rings(source)
        except (TypeError, ValueError) as exc:
            self.report({"WARNING"}, str(exc))
            return {"CANCELLED"}
        settings = context.scene.ovmg_settings
        obj = _create_editor_building(
            context,
            (rings[0],),
            rings[1:],
            context.scene.cursor.location.z + float(settings.user_building_base_height),
            prefix="CurveBuilding",
        )
        BuildingCorrectionStore.register_added_object(
            str(settings.project_name), obj.name
        )
        settings.status = "Building created from Curve footprint"
        settings.correction_status = "The source Curve remains available for further edits."
        self.report({"INFO"}, settings.correction_status)
        return {"FINISHED"}


class OVMG_OT_LoadInspectedBuilding(bpy.types.Operator):
    """Load source building values into the correction-studio controls."""

    bl_idname = "ovmg.load_inspected_building"
    bl_label = "Load Source Values"
    bl_description = "Load the inspected building dimensions, roof, and facade into the editor"
    bl_options = {"REGISTER"}

    def execute(self, context: bpy.types.Context) -> set[str]:
        settings = context.scene.ovmg_settings
        source_id = str(settings.inspector_source_id)
        record = BuildingMetadataReader.find(str(settings.project_name), source_id)
        if record is None:
            self.report({"WARNING"}, "Inspect a generated building first.")
            return {"CANCELLED"}
        outer = record.get("outer_rings_xy", [])
        points = [point for ring in outer for point in ring]
        if points:
            xs = [float(point[0]) for point in points]
            ys = [float(point[1]) for point in points]
            settings.user_building_width = max(1.0, max(xs) - min(xs))
            settings.user_building_depth = max(1.0, max(ys) - min(ys))
        base_height = float(record.get("minimum_height_m", 0.0))
        source_top = float(record.get("height_m", 9.0))
        settings.user_building_height = max(1.0, source_top - base_height)
        settings.user_building_base_height = base_height
        settings.user_building_roof_height = max(0.0, float(record.get("roof_height_m", 0.0)))
        roof = str(record.get("roof_shape", "flat")).upper()
        roof_aliases = {"ONION": "ONION", "DOME": "DOME", "CONE": "CONE", "GABLED": "GABLED", "HIPPED": "HIPPED", "PYRAMID": "PYRAMID"}
        settings.user_building_roof_shape = roof_aliases.get(roof, "FLAT")
        profile = str(record.get("facade_profile", "generic_plaster"))
        valid_profiles = {
            "generic_plaster", "residential_brick", "commercial_glass",
            "institutional_stone", "industrial_concrete", "worship_stone",
            "historic_brick", "metal_tower",
        }
        settings.user_building_facade_profile = profile if profile in valid_profiles else "generic_plaster"
        settings.user_building_facade_color = _hex_rgba(
            str(record.get("facade_color", "")),
            tuple(settings.user_building_facade_color),
        )
        settings.user_building_roof_color = _hex_rgba(
            str(record.get("roof_color", "")),
            tuple(settings.user_building_roof_color),
        )
        settings.user_building_name = str(record.get("building_type", "Building")).replace("_", " ").title()
        settings.correction_status = "Source values loaded. Adjust them, then create a replacement."
        self.report({"INFO"}, settings.correction_status)
        return {"FINISHED"}


class OVMG_OT_CreateBuildingReplacement(bpy.types.Operator):
    """Create an editable replacement and suppress its generated source."""

    bl_idname = "ovmg.create_building_replacement"
    bl_label = "Create Editable Replacement"
    bl_description = "Create a separate editable copy of the inspected source footprint"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context: bpy.types.Context) -> set[str]:
        settings = context.scene.ovmg_settings
        project = str(settings.project_name)
        source_id = str(settings.inspector_source_id)
        record = BuildingMetadataReader.find(project, source_id)
        if record is None:
            self.report({"WARNING"}, "Inspect a generated building first.")
            return {"CANCELLED"}
        existing = BuildingCorrectionStore.replacement_object(project, source_id)
        if existing is not None:
            _select_only(context, existing)
            self.report({"INFO"}, "The existing replacement was selected.")
            return {"FINISHED"}
        outer = record.get("outer_rings_xy", [])
        inner = record.get("inner_rings_xy", [])
        if not outer:
            position = record.get("position", [0.0, 0.0, 0.0])
            ring = primitive_footprint(
                "BOX",
                float(settings.user_building_width),
                float(settings.user_building_depth),
                8,
            )
            outer = [[
                [x + float(position[0]), y + float(position[1])] for x, y in ring
            ]]
        obj = _create_editor_building(
            context,
            outer,
            inner,
            float(settings.user_building_base_height),
            source_id=source_id,
            correction_kind="REPLACEMENT",
            prefix="Replacement",
        )
        related_ids = _related_building_source_ids(project, source_id)
        for related_id in related_ids:
            BuildingCorrectionStore.suppress(
                project,
                related_id,
                obj.name if related_id == source_id else "",
            )
        obj["ovmg_suppressed_source_ids_json"] = json.dumps(related_ids)
        settings.correction_requires_regeneration = True
        settings.correction_status = (
            "Replacement created. Regenerate once to remove the original chunk geometry."
        )
        settings.status = "Building replacement pending regeneration"
        self.report({"WARNING"}, settings.correction_status)
        return {"FINISHED"}


class OVMG_OT_MarkInspectedBuildingDeleted(bpy.types.Operator):
    """Suppress the inspected generated building on the next regeneration."""

    bl_idname = "ovmg.mark_inspected_building_deleted"
    bl_label = "Remove Source Building"
    bl_description = "Mark the inspected source building for removal on regeneration"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context: bpy.types.Context) -> set[str]:
        settings = context.scene.ovmg_settings
        source_id = str(settings.inspector_source_id)
        if not source_id:
            self.report({"WARNING"}, "Inspect a generated building first.")
            return {"CANCELLED"}
        project = str(settings.project_name)
        related_ids = _related_building_source_ids(project, source_id)
        for related_id in related_ids:
            BuildingCorrectionStore.suppress(project, related_id)
        settings.correction_requires_regeneration = True
        settings.correction_status = "Source building marked for removal. Regenerate to apply."
        self.report({"WARNING"}, settings.correction_status)
        return {"FINISHED"}


class OVMG_OT_RestoreInspectedBuilding(bpy.types.Operator):
    """Restore a suppressed source building and remove its replacement."""

    bl_idname = "ovmg.restore_inspected_building"
    bl_label = "Restore Original"
    bl_description = "Cancel the correction and restore the source building on regeneration"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context: bpy.types.Context) -> set[str]:
        settings = context.scene.ovmg_settings
        project = str(settings.project_name)
        source_id = str(settings.inspector_source_id)
        if not source_id:
            self.report({"WARNING"}, "Inspect a generated building first.")
            return {"CANCELLED"}
        related_ids = _related_building_source_ids(project, source_id)
        replacement = BuildingCorrectionStore.replacement_object(project, source_id)
        for related_id in related_ids:
            BuildingCorrectionStore.restore(project, related_id)
        if replacement is not None:
            root = root_user_building(replacement) or replacement
            remove_building_hierarchy(root)
        settings.correction_requires_regeneration = True
        settings.correction_status = "Original source restored. Regenerate to apply."
        self.report({"INFO"}, settings.correction_status)
        return {"FINISHED"}


class OVMG_OT_LoadSelectedUserBuilding(bpy.types.Operator):
    """Load the selected editable building into numeric controls."""

    bl_idname = "ovmg.load_selected_user_building"
    bl_label = "Load Selected"
    bl_description = "Load dimensions, rotation, roof, and facade of the selected editable building"
    bl_options = {"REGISTER"}

    def execute(self, context: bpy.types.Context) -> set[str]:
        settings = context.scene.ovmg_settings
        root = root_user_building(context.view_layer.objects.active)
        if root is None:
            self.report({"WARNING"}, "Select an editable OVMG building.")
            return {"CANCELLED"}
        dimensions = root.dimensions
        settings.user_building_width = max(1.0, float(dimensions.x))
        settings.user_building_depth = max(1.0, float(dimensions.y))
        settings.user_building_height = max(
            1.0,
            float(root.get("ovmg_editor_total_height_m", dimensions.z)),
        )
        settings.user_building_base_height = float(root.location.z)
        settings.user_building_roof_height = float(root.get("ovmg_roof_height_m", 0.0))
        roof = str(root.get("ovmg_roof_shape", "FLAT"))
        if roof in {"FLAT", "GABLED", "HIPPED", "PYRAMID", "DOME", "ONION", "CONE"}:
            settings.user_building_roof_shape = roof
        profile = str(root.get("ovmg_facade_profile", "generic_plaster"))
        try:
            settings.user_building_facade_profile = profile
        except TypeError:
            settings.user_building_facade_profile = "generic_plaster"
        settings.user_building_rotation = float(root.rotation_euler.z)
        settings.user_building_name = str(root.get("ovmg_building_name", root.name))
        settings.user_building_facade_color = _hex_rgba(
            str(root.get("ovmg_facade_color", "")),
            tuple(settings.user_building_facade_color),
        )
        settings.user_building_roof_color = _hex_rgba(
            str(root.get("ovmg_roof_color", "")),
            tuple(settings.user_building_roof_color),
        )
        settings.correction_status = f"Loaded editable building: {root.name}"
        self.report({"INFO"}, settings.correction_status)
        return {"FINISHED"}


class OVMG_OT_ApplySelectedUserBuilding(bpy.types.Operator):
    """Apply numeric editor values to the selected editable building hierarchy."""

    bl_idname = "ovmg.apply_selected_user_building"
    bl_label = "Apply Changes"
    bl_description = (
        "Rebuild an OVMG footprint building, or scale an imported custom mesh, "
        "using the current correction-studio values"
    )
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context: bpy.types.Context) -> set[str]:
        settings = context.scene.ovmg_settings
        root = root_user_building(context.view_layer.objects.active)
        if root is None:
            self.report({"WARNING"}, "Select an editable OVMG building.")
            return {"CANCELLED"}
        quality = QualityPreset(str(settings.quality_preset))
        if quality is QualityPreset.CUSTOM:
            quality = QualityPreset.MEDIUM
        try:
            rebuilt = rebuild_editable_building(
                root,
                target_width=float(settings.user_building_width),
                target_depth=float(settings.user_building_depth),
                base_z=float(settings.user_building_base_height),
                total_height=float(settings.user_building_height),
                roof_shape=str(settings.user_building_roof_shape),
                roof_height=float(settings.user_building_roof_height),
                rotation_z=float(settings.user_building_rotation),
                profile=str(settings.user_building_facade_profile),
                facade_color=tuple(settings.user_building_facade_color),
                roof_color=tuple(settings.user_building_roof_color),
                realistic_material=str(settings.material_style) == "REALISTIC",
                quality=quality,
                roof_segments=max(8, int(settings.curved_detail_segments)),
            )
        except (TypeError, ValueError) as exc:
            self.report({"ERROR"}, f"Building rebuild failed: {exc}")
            return {"CANCELLED"}

        if not rebuilt:
            current_width = max(0.001, float(root.dimensions.x))
            current_depth = max(0.001, float(root.dimensions.y))
            current_height = max(0.001, float(root.dimensions.z))
            root.scale.x *= float(settings.user_building_width) / current_width
            root.scale.y *= float(settings.user_building_depth) / current_depth
            root.scale.z *= float(settings.user_building_height) / current_height
            root.location.z = float(settings.user_building_base_height)
            root.rotation_euler.z = float(settings.user_building_rotation)
            for obj in building_hierarchy(root):
                assign_building_material(
                    obj,
                    str(settings.user_building_facade_profile),
                    tuple(settings.user_building_facade_color),
                    tuple(settings.user_building_roof_color),
                    str(settings.material_style) == "REALISTIC",
                    quality,
                )

        root["ovmg_editor_total_height_m"] = float(settings.user_building_height)
        root["ovmg_editor_width_m"] = float(settings.user_building_width)
        root["ovmg_editor_depth_m"] = float(settings.user_building_depth)
        root["ovmg_editor_base_height_m"] = float(settings.user_building_base_height)
        root["ovmg_roof_shape"] = str(settings.user_building_roof_shape)
        root["ovmg_roof_height_m"] = float(settings.user_building_roof_height)
        root["ovmg_building_name"] = str(settings.user_building_name)
        root["ovmg_facade_profile"] = str(settings.user_building_facade_profile)
        settings.correction_status = (
            f"Rebuilt {root.name} with the new footprint, roof, height, and facade."
            if rebuilt
            else f"Applied transform and material changes to imported mesh {root.name}."
        )
        self.report({"INFO"}, settings.correction_status)
        return {"FINISHED"}


class OVMG_OT_DuplicateSelectedUserBuilding(bpy.types.Operator):
    """Duplicate one editable building hierarchy as a new added building."""

    bl_idname = "ovmg.duplicate_selected_user_building"
    bl_label = "Duplicate Selected"
    bl_description = "Duplicate the selected editable building and offset it for placement"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context: bpy.types.Context) -> set[str]:
        root = root_user_building(context.view_layer.objects.active)
        if root is None:
            self.report({"WARNING"}, "Select an editable OVMG building.")
            return {"CANCELLED"}
        settings = context.scene.ovmg_settings
        collection = _user_building_collection(context.scene, str(settings.project_name))
        duplicate = root.copy()
        duplicate.data = root.data.copy() if root.data is not None else None
        duplicate.name = _next_building_name(settings, "Duplicate")
        duplicate.location.x += max(2.0, root.dimensions.x * 0.15)
        duplicate.location.y += max(2.0, root.dimensions.y * 0.15)
        duplicate.parent = None
        if "ovmg_replaces_source_id" in duplicate:
            del duplicate["ovmg_replaces_source_id"]
        mark_user_building(duplicate, str(settings.project_name), correction_kind="ADDED")
        duplicate["ovmg_user_building_root"] = True
        collection.objects.link(duplicate)
        for child in root.children:
            child_copy = child.copy()
            child_copy.data = child.data.copy() if child.data is not None else None
            child_copy.parent = duplicate
            child_copy.matrix_parent_inverse = child.matrix_parent_inverse.copy()
            if "ovmg_replaces_source_id" in child_copy:
                del child_copy["ovmg_replaces_source_id"]
            mark_user_building(child_copy, str(settings.project_name), correction_kind="ADDED")
            collection.objects.link(child_copy)
        BuildingCorrectionStore.register_added_object(str(settings.project_name), duplicate.name)
        _select_only(context, duplicate)
        settings.correction_status = f"Duplicated {root.name}."
        self.report({"INFO"}, settings.correction_status)
        return {"FINISHED"}


class OVMG_OT_SnapSelectedUserBuilding(bpy.types.Operator):
    """Place the selected editable building base on the map ground plane."""

    bl_idname = "ovmg.snap_selected_user_building"
    bl_label = "Snap Base to Ground"
    bl_description = "Move the selected editable building base to Z = 0"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context: bpy.types.Context) -> set[str]:
        root = root_user_building(context.view_layer.objects.active)
        if root is None:
            self.report({"WARNING"}, "Select an editable OVMG building.")
            return {"CANCELLED"}
        root.location.z = 0.0
        root["ovmg_editor_base_height_m"] = 0.0
        context.scene.ovmg_settings.user_building_base_height = 0.0
        self.report({"INFO"}, f"{root.name} snapped to map ground.")
        return {"FINISHED"}


class OVMG_OT_ConvertSelectedToUserBuilding(bpy.types.Operator):
    """Register selected meshes as persistent user buildings for export."""

    bl_idname = "ovmg.convert_selected_to_user_building"
    bl_label = "Register Selected Objects"
    bl_description = "Include selected mesh objects as persistent user buildings"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context: bpy.types.Context) -> set[str]:
        settings = context.scene.ovmg_settings
        project = str(settings.project_name)
        collection = _user_building_collection(context.scene, project)
        count = 0
        for obj in context.selected_objects:
            if obj.type != "MESH":
                continue
            mark_user_building(obj, project, correction_kind="ADDED")
            obj["ovmg_user_building_root"] = obj.parent is None
            if collection.objects.get(obj.name) is None:
                collection.objects.link(obj)
            BuildingCorrectionStore.register_added_object(project, obj.name)
            count += 1
        if count == 0:
            self.report({"WARNING"}, "Select at least one mesh object.")
            return {"CANCELLED"}
        settings.status = "Selected objects registered"
        settings.correction_status = f"Registered {count} editable building object(s)."
        self.report({"INFO"}, settings.correction_status)
        return {"FINISHED"}


class OVMG_OT_SelectUserBuildings(bpy.types.Operator):
    """Select all persistent custom building roots for this project."""

    bl_idname = "ovmg.select_user_buildings"
    bl_label = "Select User Buildings"
    bl_description = "Select all manually added and replacement buildings"
    bl_options = {"REGISTER"}

    def execute(self, context: bpy.types.Context) -> set[str]:
        project = str(context.scene.ovmg_settings.project_name)
        for obj in context.view_layer.objects:
            obj.select_set(False)
        selected = [
            obj
            for obj in context.view_layer.objects
            if obj.get("ovmg_user_building_root")
            and obj.get("ovmg_project") == project
        ]
        for obj in selected:
            obj.hide_set(False)
            obj.select_set(True)
        if selected:
            context.view_layer.objects.active = selected[0]
        self.report({"INFO"}, f"Selected {len(selected)} editable building(s).")
        return {"FINISHED"}


class OVMG_OT_DeleteSelectedUserBuildings(bpy.types.Operator):
    """Delete selected editable building hierarchies and repair correction state."""

    bl_idname = "ovmg.delete_selected_user_buildings"
    bl_label = "Delete Selected User Buildings"
    bl_description = "Delete selected editable buildings; replacement sources are restored"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context: bpy.types.Context) -> set[str]:
        project = str(context.scene.ovmg_settings.project_name)
        roots = {
            root
            for obj in context.selected_objects
            for root in (root_user_building(obj),)
            if root is not None
        }
        if not roots:
            self.report({"WARNING"}, "Select one or more editable buildings.")
            return {"CANCELLED"}
        for root in roots:
            BuildingCorrectionStore.unregister_object(project, root)
            remove_building_hierarchy(root)
        settings = context.scene.ovmg_settings
        settings.correction_requires_regeneration = True
        settings.correction_status = (
            f"Deleted {len(roots)} editable building(s). Regenerate if a source was restored."
        )
        self.report({"INFO"}, settings.correction_status)
        return {"FINISHED"}


class OVMG_OT_ClearBuildingCorrections(bpy.types.Operator):
    """Delete every editable correction object and restore all source buildings."""

    bl_idname = "ovmg.clear_building_corrections"
    bl_label = "Clear All Building Corrections"
    bl_description = (
        "Delete all added/replacement building objects and restore every suppressed "
        "source on the next regeneration"
    )
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context: bpy.types.Context) -> set[str]:
        settings = context.scene.ovmg_settings
        project = str(settings.project_name)
        roots = [
            obj
            for obj in list(bpy.data.objects)
            if obj.get("ovmg_user_building_root")
            and obj.get("ovmg_project") == project
        ]
        for root in roots:
            if bpy.data.objects.get(root.name) is not None:
                remove_building_hierarchy(root)
        BuildingCorrectionStore.clear(project)
        settings.correction_requires_regeneration = True
        settings.correction_status = (
            f"Cleared {len(roots)} editable building(s). Regenerate to restore all sources."
        )
        self.report({"WARNING"}, settings.correction_status)
        return {"FINISHED"}


class OVMG_OT_RegenerateWithCorrections(bpy.types.Operator):
    """Regenerate generated chunks while preserving editable correction objects."""

    bl_idname = "ovmg.regenerate_with_building_corrections"
    bl_label = "Apply Corrections and Regenerate"
    bl_description = "Regenerate the map, omitting suppressed source buildings and preserving custom objects"
    bl_options = {"REGISTER"}

    def execute(self, context: bpy.types.Context) -> set[str]:
        context.scene.ovmg_settings.correction_requires_regeneration = False
        return bpy.ops.ovmg.generate_map()


class OVMG_OT_GenerateMap(bpy.types.Operator):
    """Generate an optimized voxel map from current scene settings."""

    bl_idname = "ovmg.generate_map"
    bl_label = "Generate Map"
    bl_description = "Download GIS data and generate the configured map"
    bl_options = {"REGISTER"}

    def execute(self, context: bpy.types.Context) -> set[str]:
        settings = context.scene.ovmg_settings
        window_manager = context.window_manager
        settings.wizard_step = 7
        settings.status = "Preparing generation"
        settings.generation_progress = 0.0
        window_manager.progress_begin(0, 1000)
        last_draw_percent = -2

        def update_progress(ratio: float, message: str) -> None:
            nonlocal last_draw_percent
            clamped = max(0.0, min(1.0, ratio))
            settings.status = message
            settings.generation_progress = clamped
            window_manager.progress_update(int(clamped * 1000))
            percent = int(clamped * 100.0)
            if context.area is not None:
                context.area.tag_redraw()
            if percent >= last_draw_percent + 2 or percent in {0, 100}:
                last_draw_percent = percent
                try:
                    bpy.ops.wm.redraw_timer(type="DRAW_WIN_SWAP", iterations=1)
                except RuntimeError:
                    pass

        try:
            if bool(settings.visual_selection_pending):
                raise ValidationError(
                    "Confirm or discard the proposed map area before generation."
                )
            if (
                str(settings.input_mode) == "VISUAL_MAP"
                and not bool(settings.visual_area_selected)
            ):
                raise ValidationError(
                    "Select an area on the visual map before generation."
                )
            if hasattr(bpy.app, "online_access") and not bpy.app.online_access:
                raise NetworkAccessError(
                    "Blender Online Access is disabled. Enable it in Preferences "
                    "before downloading geographic data."
                )

            preferences = _PreferenceAccess.get(context)
            request = self._build_request(settings, preferences)
            metrics = AreaMetricsCalculator.calculate(
                request.bounding_box,
                request.voxel.voxel_size,
                request.max_voxel_cells,
                quality_generation_multiplier(str(settings.quality_preset)),
            )
            if (
                metrics.load_level is AreaLoadLevel.TOO_LARGE
                and request.large_area_mode is not LargeAreaMode.SPLIT_TILES
            ):
                raise ValidationError(
                    "The selected map is too large for the current quality. "
                    "Make the map smaller or choose a lower quality before generating."
                )
            service = ApplicationFactory.create_generation_service(
                context.scene,
                preferences,
                request.voxel,
            )
            result = service.generate(request, update_progress)
            BlenderAreaPreviewService.remove(context.scene)

            settings.south = result.bounds.south
            settings.west = result.bounds.west
            settings.north = result.bounds.north
            settings.east = result.bounds.east
            stats = result.statistics
            building = stats.building_statistics
            settings.status = (
                "Generation complete with warnings"
                if stats.warnings
                else "Generation complete"
            )
            settings.generated_map_ready = True
            settings.generation_progress = 1.0
            settings.wizard_step = 6
            settings.correction_requires_regeneration = False
            correction_summary = BuildingCorrectionStore.summary(
                str(settings.project_name)
            )
            settings.correction_status = (
                f"Corrections active: {correction_summary.replacements} replacement(s), "
                f"{correction_summary.added_buildings} added building(s), "
                f"{correction_summary.suppressed_sources} hidden source feature(s)."
            )
            details = (
                f" • {stats.curved_detail_count:,} curved details"
                if stats.curved_detail_count
                else ""
            )
            labels = f" • {stats.label_count:,} labels" if stats.label_count else ""
            tiles = f" • {stats.tile_count:,} map tiles" if stats.tile_count > 1 else ""
            settings.last_summary = (
                f"{stats.source_feature_count:,} GIS features • "
                f"{stats.voxel_count:,} voxels • "
                f"{stats.chunk_count:,} chunk meshes{tiles}{details}{labels}"
            )
            if any(
                "OpenStreetMap buildings only" in warning
                or "Overture buildings were unavailable" in warning
                for warning in stats.warnings
            ):
                settings.last_summary += " • OSM fallback used"
            if any(
                "Automatic building safety checks" in warning
                for warning in stats.warnings
            ):
                settings.last_summary += " • building safety filters applied"
            settings.building_summary_1 = (
                f"OSM {building.osm_buildings:,} • Overture "
                f"{building.overture_buildings:,} • Parts {building.overture_parts:,}"
            )
            settings.building_summary_2 = (
                f"Merged {building.merged_duplicates:,} duplicates • Final "
                f"{building.final_building_features:,} building features"
            )
            settings.building_summary_3 = (
                f"Height: direct {building.real_height:,} • floors "
                f"{building.real_levels:,} • inferred {building.inferred_height:,} "
                f"• default {building.default_height:,}"
            )
            accuracy = stats.building_accuracy
            settings.building_summary_4 = (
                f"Confidence: high {accuracy.high_confidence:,} • medium "
                f"{accuracy.medium_confidence:,} • low {accuracy.low_confidence:,} "
                f"• very low {accuracy.very_low_confidence:,}"
            )
            settings.building_summary_5 = (
                f"Facades: mapped evidence {accuracy.source_facades:,} • no source data "
                f"{accuracy.unavailable_facades:,} • procedural {accuracy.procedural_facades:,}"
            )
            self.report({"INFO"}, settings.last_summary)
            return {"FINISHED"}
        except OVMGError as exc:
            settings.status = f"Error: {exc}"
            settings.last_summary = (
                "Generation stopped before scene replacement; any existing map "
                "was left unchanged. Retry or select a smaller area."
            )
            settings.wizard_step = 6
            self._clear_building_summary(settings)
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        except Exception as exc:  # Blender operators must surface unexpected faults.
            traceback.print_exc()
            settings.wizard_step = 6
            settings.status = f"Unexpected error: {exc}"
            settings.last_summary = (
                "Generation stopped before scene replacement; any existing map "
                "was left unchanged."
            )
            self.report({"ERROR"}, f"Unexpected generation error: {exc}")
            return {"CANCELLED"}
        finally:
            window_manager.progress_end()
            if context.area is not None:
                context.area.tag_redraw()

    @classmethod
    def _build_request(cls, settings: object, preferences: object) -> MapBuildRequest:
        input_mode = InputMode(str(settings.input_mode))
        return MapBuildRequest(
            project_name=str(settings.project_name),
            input_mode=input_mode,
            area_name=str(settings.area_name),
            bounding_box=BoundingBox(
                south=float(settings.south),
                west=float(settings.west),
                north=float(settings.north),
                east=float(settings.east),
            ),
            voxel=cls._build_voxel_settings(settings),
            max_voxel_cells=int(preferences.max_voxel_cells),
            large_area_mode=LargeAreaMode(str(settings.large_area_mode)),
        )

    @staticmethod
    def _build_voxel_settings(settings: object) -> VoxelSettings:
        """Build one conservative runtime configuration from simple choices.

        Advanced building variables from older saved files are deliberately not
        trusted here. The user chooses only style, quality, materials, layers,
        labels, and data source; safe geometry rules remain automatic.
        """
        quality_name = str(settings.quality_preset)
        if quality_name not in {"LOW", "MEDIUM", "HIGH"}:
            quality_name = "MEDIUM"
        quality_values = {
            "LOW": {
                "voxel_size": 5.0,
                "vertical_step": 1.5,
                "chunk_size": 64,
                "tree_density": 0.08,
                "minimum_building_area": 25.0,
            },
            "MEDIUM": {
                "voxel_size": 2.5,
                "vertical_step": 1.0,
                "chunk_size": 64,
                "tree_density": 0.20,
                "minimum_building_area": 15.0,
            },
            "HIGH": {
                "voxel_size": 1.5,
                "vertical_step": 0.5,
                "chunk_size": 64,
                "tree_density": 0.30,
                "minimum_building_area": 6.0,
            },
        }[quality_name]

        model_style = ModelStyle(str(settings.model_style))
        style_values = {
            ModelStyle.CLASSIC_VOXEL: {
                "use_building_parts": True,
                "use_roof_shapes": True,
                "geometry_style": GeometryStyle.VOXEL_ONLY,
                "curved_detail_segments": 12,
                "generate_facade_detail": False,
            },
            ModelStyle.MINECRAFT: {
                "use_building_parts": True,
                "use_roof_shapes": False,
                "geometry_style": GeometryStyle.VOXEL_ONLY,
                "curved_detail_segments": 8,
                "generate_facade_detail": False,
            },
            ModelStyle.LOW_POLY: {
                "use_building_parts": True,
                "use_roof_shapes": True,
                "geometry_style": GeometryStyle.VOXEL_CURVED,
                "curved_detail_segments": 8,
                "generate_facade_detail": False,
            },
            ModelStyle.REAL: {
                "use_building_parts": True,
                "use_roof_shapes": True,
                "geometry_style": GeometryStyle.VOXEL_CURVED,
                "curved_detail_segments": 16,
                "generate_facade_detail": True,
            },
            ModelStyle.ARCHITECTURAL_MODEL: {
                "use_building_parts": True,
                "use_roof_shapes": True,
                "geometry_style": GeometryStyle.VOXEL_CURVED,
                "curved_detail_segments": 20,
                "generate_facade_detail": False,
            },
        }[model_style]

        source_name = str(settings.building_source)
        if source_name not in {"HYBRID", "OSM_ONLY", "OVERTURE_ONLY"}:
            source_name = "HYBRID"
        material_style = MaterialStyle(str(settings.material_style))

        return VoxelSettings(
            voxel_size=float(quality_values["voxel_size"]),
            vertical_step=float(quality_values["vertical_step"]),
            chunk_size=int(quality_values["chunk_size"]),
            default_building_height=9.0,
            level_height=3.0,
            tree_density=float(quality_values["tree_density"]),
            building_source=BuildingSource(source_name),
            use_building_parts=bool(style_values["use_building_parts"]),
            use_roof_shapes=bool(style_values["use_roof_shapes"]),
            minimum_building_area=float(quality_values["minimum_building_area"]),
            fallback_river_width=220.0,
            infer_missing_heights=True,
            generate_landmark_proxies=False,
            generate_approximate_buildings=False,
            approximate_building_density=0.0,
            include_terrain=bool(settings.include_terrain),
            include_buildings=bool(settings.include_buildings),
            include_roads=bool(settings.include_roads),
            include_water=bool(settings.include_water),
            include_green=bool(settings.include_green),
            include_bridges=bool(settings.include_bridges),
            include_trees=bool(settings.include_trees),
            quality_preset=QualityPreset(quality_name),
            enhanced_materials=material_style is MaterialStyle.REALISTIC,
            geometry_style=style_values["geometry_style"],
            model_style=model_style,
            material_style=material_style,
            curved_detail_segments=int(style_values["curved_detail_segments"]),
            curved_detail_limit=350,
            generate_labels=bool(settings.generate_labels),
            label_mode=LabelMode(str(settings.label_mode)),
            label_language=LabelLanguage(str(settings.label_language)),
            include_street_labels=bool(settings.include_street_labels),
            include_area_labels=bool(settings.include_area_labels),
            include_landmark_labels=bool(settings.include_landmark_labels),
            maximum_label_count=int(settings.maximum_label_count),
            show_accuracy_overlay=bool(settings.show_accuracy_overlay),
            accuracy_overlay_limit=5000,
            generate_facade_detail=bool(style_values["generate_facade_detail"]),
            use_source_facade_hints=True,
            strict_real_facades=bool(settings.strict_real_facades),
            # The six-step workflow always rebuilds from source data. Legacy
            # manual suppression records are deliberately ignored so an old
            # project cannot silently hide real buildings in a new generation.
            excluded_building_source_ids=(),
        )

    @staticmethod
    def _clear_building_summary(settings: object) -> None:
        settings.building_summary_1 = ""
        settings.building_summary_2 = ""
        settings.building_summary_3 = ""
        settings.building_summary_4 = ""
        settings.building_summary_5 = ""


def _point_segment_distance(
    point_x: float,
    point_y: float,
    start: object,
    end: object,
) -> float:
    """Return Euclidean XY distance from a point to one line segment."""
    x1, y1 = float(start[0]), float(start[1])
    x2, y2 = float(end[0]), float(end[1])
    dx = x2 - x1
    dy = y2 - y1
    length_squared = dx * dx + dy * dy
    if length_squared <= 1e-12:
        return hypot(point_x - x1, point_y - y1)
    factor = ((point_x - x1) * dx + (point_y - y1) * dy) / length_squared
    factor = max(0.0, min(1.0, factor))
    return hypot(point_x - (x1 + factor * dx), point_y - (y1 + factor * dy))


def _point_in_metadata_ring(point_x: float, point_y: float, ring: object) -> bool:
    """Return whether one XY point lies inside a serialized footprint ring."""
    if not isinstance(ring, (list, tuple)) or len(ring) < 4:
        return False
    inside = False
    for index in range(len(ring) - 1):
        x1, y1 = float(ring[index][0]), float(ring[index][1])
        x2, y2 = float(ring[index + 1][0]), float(ring[index + 1][1])
        if (y1 > point_y) != (y2 > point_y):
            crossing = (x2 - x1) * (point_y - y1) / max(1e-12, y2 - y1) + x1
            if point_x < crossing:
                inside = not inside
    return inside


def _record_cursor_metric(record: dict[str, object], x: float, y: float) -> tuple[float, float]:
    """Measure cursor proximity to a real footprint, then prefer smaller overlaps."""
    outer = record.get("outer_rings_xy", [])
    inner = record.get("inner_rings_xy", [])
    if isinstance(outer, list) and outer:
        inside_outer = any(_point_in_metadata_ring(x, y, ring) for ring in outer)
        inside_hole = any(_point_in_metadata_ring(x, y, ring) for ring in inner)
        if inside_outer and not inside_hole:
            area = 0.0
            for ring in outer:
                if not isinstance(ring, list):
                    continue
                area += abs(
                    0.5
                    * sum(
                        float(ring[index][0]) * float(ring[index + 1][1])
                        - float(ring[index + 1][0]) * float(ring[index][1])
                        for index in range(len(ring) - 1)
                    )
                )
            return 0.0, area
        distances = [
            _point_segment_distance(x, y, ring[index], ring[index + 1])
            for ring in (*outer, *inner)
            if isinstance(ring, list)
            for index in range(max(0, len(ring) - 1))
        ]
        if distances:
            return min(distances), float("inf")
    position = record.get("position", [0.0, 0.0, 0.0])
    return hypot(float(position[0]) - x, float(position[1]) - y), float("inf")


class OVMG_OT_InspectNearestBuilding(bpy.types.Operator):
    """Inspect the generated building nearest to the 3D cursor."""

    bl_idname = "ovmg.inspect_nearest_building"
    bl_label = "Inspect Nearest Building"
    bl_description = (
        "Read source, height, roof, facade, and confidence metadata for the "
        "building nearest to the 3D Cursor"
    )
    bl_options = {"REGISTER"}

    def execute(self, context: bpy.types.Context) -> set[str]:
        settings = context.scene.ovmg_settings
        text = bpy.data.texts.get(building_metadata_name(str(settings.project_name)))
        if text is None:
            self.report({"WARNING"}, "No building accuracy metadata exists for this map.")
            return {"CANCELLED"}
        try:
            payload = json.loads(text.as_string())
            buildings = payload.get("buildings", [])
            if not buildings:
                raise ValueError("The building metadata list is empty.")
            cursor = context.scene.cursor.location
            nearest = min(
                buildings,
                key=lambda item: _record_cursor_metric(item, cursor.x, cursor.y),
            )
            position = nearest.get("position", [0.0, 0.0, 0.0])
            distance = _record_cursor_metric(nearest, cursor.x, cursor.y)[0]
            settings.inspector_source_id = str(nearest.get("source_id", "Unknown"))
            settings.inspector_building_type = str(nearest.get("building_type", "Unknown")).replace("_", " ").title()
            settings.inspector_height = f"{float(nearest.get('height_m', 0.0)):.2f} m"
            settings.inspector_height_source = str(nearest.get("height_source", "Unknown")).replace("_", " ").title()
            settings.inspector_footprint_source = str(nearest.get("footprint_source", "Unknown")).replace("_", " ").title()
            settings.inspector_roof = (
                f"{str(nearest.get('roof_shape', 'flat')).title()} • "
                f"{str(nearest.get('roof_source', 'Unknown')).replace('_', ' ').title()}"
            )
            settings.inspector_facade = (
                f"{str(nearest.get('facade_profile', 'generic')).replace('_', ' ').title()} • "
                f"{str(nearest.get('facade_source', 'Unknown')).replace('_', ' ').title()}"
            )
            settings.inspector_confidence = (
                f"{str(nearest.get('confidence', 'Unknown')).replace('_', ' ').title()} "
                f"({float(nearest.get('confidence_score', 0.0)):.0%})"
            )
            settings.inspector_datasets = str(nearest.get("source_datasets", "")) or "Not reported"
            settings.inspector_distance = f"{distance:.1f} m from 3D Cursor"
            settings.inspector_roof_height = float(nearest.get("roof_height_m", 0.0))
            settings.inspector_base_height = float(nearest.get("minimum_height_m", 0.0))
            settings.inspector_facade_hex = str(nearest.get("facade_color", ""))
            settings.inspector_roof_hex = str(nearest.get("roof_color", ""))
            settings.status = "Building inspection updated"
            settings.correction_status = (
                "Building identified. Load its source values before creating a replacement."
            )
            self.report({"INFO"}, settings.inspector_confidence)
            return {"FINISHED"}
        except Exception as exc:
            traceback.print_exc()
            self.report({"ERROR"}, f"Building inspection failed: {exc}")
            return {"CANCELLED"}


class OVMG_OT_ToggleAccuracyOverlay(bpy.types.Operator):
    """Show or hide the generated building-confidence overlay."""

    bl_idname = "ovmg.toggle_accuracy_overlay"
    bl_label = "Toggle Accuracy Overlay"
    bl_description = (
        "Show or hide the optional roof-colour overlay that visualizes building "
        "confidence without modifying generated buildings"
    )
    bl_options = {"REGISTER"}

    def execute(self, context: bpy.types.Context) -> set[str]:
        settings = context.scene.ovmg_settings
        project_token = sanitize_name(str(settings.project_name))
        candidates = [
            collection
            for collection in bpy.data.collections
            if project_token in collection.name
            and "Building_Accuracy_Overlay" in collection.name
        ]
        if not candidates:
            self.report(
                {"WARNING"},
                "No accuracy overlay was generated. Enable it under Advanced Options and regenerate.",
            )
            return {"CANCELLED"}
        should_hide = any(not collection.hide_viewport for collection in candidates)
        for collection in candidates:
            collection.hide_viewport = should_hide
            collection.hide_render = should_hide
        settings.status = (
            "Building accuracy overlay hidden"
            if should_hide
            else "Building accuracy overlay shown"
        )
        self.report({"INFO"}, settings.status)
        return {"FINISHED"}


class OVMG_OT_ExportMap(bpy.types.Operator):
    """Export the generated project using the simple export configuration."""

    bl_idname = "ovmg.export_map"
    bl_label = "Export Map"
    bl_description = "Export this generated project to the selected model format"
    bl_options = {"REGISTER"}

    filepath: StringProperty(name="File Path", subtype="FILE_PATH")
    filter_glob: StringProperty(
        default="*.glb;*.fbx;*.obj;*.usd;*.blend",
        options={"HIDDEN"},
    )

    @staticmethod
    def _included_categories(settings: object) -> set[FeatureType] | None:
        mapping = (
            ("export_buildings", FeatureType.BUILDING),
            ("export_roads", FeatureType.ROAD),
            ("export_bridges", FeatureType.BRIDGE),
            ("export_terrain", FeatureType.TERRAIN),
            ("export_water", FeatureType.WATER),
            ("export_green", FeatureType.GREEN),
            ("export_trees", FeatureType.TREE),
        )
        selected = {
            category
            for property_name, category in mapping
            if bool(getattr(settings, property_name, False))
        }
        return None if len(selected) == len(mapping) else selected

    @staticmethod
    def _scope_suffix(settings: object) -> str:
        selected = OVMG_OT_ExportMap._included_categories(settings)
        if selected is None:
            return "Full_Map"
        if selected == {FeatureType.BUILDING}:
            return "Buildings"
        if selected == {FeatureType.ROAD, FeatureType.BRIDGE}:
            return "Roads"
        if selected == {FeatureType.TERRAIN, FeatureType.WATER, FeatureType.GREEN, FeatureType.TREE}:
            return "Environment"
        return "Custom"

    def invoke(
        self,
        context: bpy.types.Context,
        _event: bpy.types.Event,
    ) -> set[str]:
        settings = context.scene.ovmg_settings
        export_format = ExportFormat(str(settings.export_format))
        extension = ProjectExportService._EXTENSIONS[export_format]
        base_directory = (
            Path(bpy.data.filepath).parent if bpy.data.filepath else Path.home()
        )
        self.filepath = str(
            base_directory
            / (
                f"{sanitize_name(settings.project_name)}_"
                f"{self._scope_suffix(settings)}{extension}"
            )
        )
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def check(self, context: bpy.types.Context) -> bool:
        settings = context.scene.ovmg_settings
        export_format = ExportFormat(str(settings.export_format))
        corrected = ProjectExportService.ensure_extension(
            Path(self.filepath),
            export_format,
        )
        changed = str(corrected) != self.filepath
        self.filepath = str(corrected)
        return changed

    def execute(self, context: bpy.types.Context) -> set[str]:
        settings = context.scene.ovmg_settings
        try:
            export_format = ExportFormat(str(settings.export_format))
            categories = self._included_categories(settings)
            if categories == set() and not bool(settings.export_include_labels):
                raise OVMGError("Select at least one map category before exporting.")
            if export_format is ExportFormat.BLEND and categories is not None:
                raise OVMGError(
                    "BLEND saves the complete scene. Select every category or use GLB, "
                    "FBX, OBJ, or USD for category export."
                )
            model_path, label_path = ProjectExportService().export(
                context=context,
                project_name=str(settings.project_name),
                filepath=self.filepath,
                export_format=export_format,
                include_materials=bool(settings.export_include_materials),
                include_labels=bool(settings.export_include_labels),
                apply_transforms=bool(settings.export_apply_transforms),
                included_categories=categories,
            )
            settings.status = "Export complete"
            if label_path is None:
                settings.last_summary = f"Exported: {model_path.name}"
            else:
                settings.last_summary = (
                    f"Exported: {model_path.name} + {label_path.name}"
                )
            self.report({"INFO"}, settings.last_summary)
            return {"FINISHED"}
        except OVMGError as exc:
            settings.status = f"Error: {exc}"
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        except Exception as exc:
            traceback.print_exc()
            settings.status = f"Unexpected export error: {exc}"
            self.report({"ERROR"}, settings.status)
            return {"CANCELLED"}


class OVMG_OT_DeleteMap(bpy.types.Operator):
    """Delete the generated collection for the current project name."""

    bl_idname = "ovmg.delete_map"
    bl_label = "Delete Generated Map"
    bl_description = "Remove generated OVMG objects, data blocks, and collections"
    bl_options = {"REGISTER"}

    def execute(self, context: bpy.types.Context) -> set[str]:
        settings = context.scene.ovmg_settings
        try:
            preferences = _PreferenceAccess.get(context)
            voxel_settings = OVMG_OT_GenerateMap._build_voxel_settings(settings)
            service = ApplicationFactory.create_generation_service(
                context.scene,
                preferences,
                voxel_settings,
            )
            deleted = service.delete(str(settings.project_name))
            settings.status = "Generated map deleted"
            settings.generated_map_ready = False
            settings.wizard_step = 6 if settings.visual_area_selected else 1
            settings.last_summary = f"Deleted {deleted:,} generated objects."
            OVMG_OT_GenerateMap._clear_building_summary(settings)
            self.report({"INFO"}, settings.last_summary)
            return {"FINISHED"}
        except OVMGError as exc:
            settings.status = f"Error: {exc}"
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
