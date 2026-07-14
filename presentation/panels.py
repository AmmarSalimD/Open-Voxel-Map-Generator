"""Guided 3D View sidebar for Open Voxel Map Generator."""

from __future__ import annotations

import bpy

from ..core.constants import ADDON_ID, ADDON_PACKAGE, ADDON_VERSION, PANEL_CATEGORY
from ..domain.models import BoundingBox
from ..infrastructure.blender.selection_thumbnail import (
    BlenderSelectionThumbnailService,
)
from ..infrastructure.map_selector.metrics import (
    AreaLoadLevel,
    AreaMetrics,
    AreaMetricsCalculator,
    quality_generation_multiplier,
)
from ..infrastructure.overture.runtime import probe_overture_runtime


class OVMG_PT_MainPanel(bpy.types.Panel):
    """Step-by-step map-generation wizard and post-generation workspace."""

    bl_idname = "OVMG_PT_main"
    bl_label = "Open Voxel Map Generator"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = PANEL_CATEGORY

    _STEP_LABELS = (
        "Area",
        "Confirm",
        "Style",
        "Quality",
        "Materials",
        "Review",
    )

    @staticmethod
    def _draw_step_error(layout: bpy.types.UILayout, exc: Exception) -> None:
        """Keep the panel usable if one wizard step cannot be drawn."""
        box = layout.box()
        box.alert = True
        box.label(text="The current wizard step could not be displayed.")
        message = f"{type(exc).__name__}: {exc}"
        # Blender sidebar labels do not wrap automatically. Keep the message
        # readable while preserving the useful exception type and beginning.
        box.label(text=message[:110])
        box.label(text="Use Restart Wizard, then report the message above.")
        box.operator("ovmg.wizard_restart", text="Restart Wizard")

    def draw(self, context: bpy.types.Context) -> None:
        settings = context.scene.ovmg_settings
        layout = self.layout

        header = layout.box()
        title = header.row(align=True)
        title.label(text=f"OVMG {ADDON_VERSION}", icon="WORLD_DATA")
        title.operator("ovmg.about_addon", text="About", icon="INFO")
        header.label(text="Guided real-map to 3D workflow")

        step = int(settings.wizard_step)
        if step <= 6:
            self._draw_step_header(layout, step)
        elif step == 7:
            progress = layout.box()
            progress.label(text="Generating Map", icon="TIME")
            factor = max(0.0, min(1.0, float(settings.generation_progress)))
            percent = int(round(factor * 100.0))
            if hasattr(progress, "progress"):
                progress.progress(
                    factor=factor,
                    type="BAR",
                    text=f"{percent}%",
                )
            else:
                fallback = progress.row()
                fallback.enabled = False
                fallback.prop(settings, "generation_progress", text=f"{percent}%", slider=True)
            progress.label(text=str(settings.status), icon="INFO")
            progress.label(text="Please wait; Blender may remain busy during meshing.")
            progress.label(text="Existing scene objects remain safe until generation completes.")
        else:
            # Legacy files may contain an older post-generation step. Return
            # them to the sixth and final wizard page.
            settings.wizard_step = 6
            step = 6
            self._draw_step_header(layout, step)

        try:
            if step == 1:
                self._draw_area_step(layout, context, settings)
            elif step == 2:
                self._draw_confirm_step(layout, context, settings)
            elif step == 3:
                self._draw_style_step(layout, settings)
            elif step == 4:
                self._draw_quality_step(layout, context, settings)
            elif step == 5:
                self._draw_material_step(layout, settings)
            elif step == 6:
                self._draw_review_step(layout, context, settings)

        except Exception as exc:  # Blender draw callbacks must never blank the panel.
            self._draw_step_error(layout, exc)

        if settings.status or settings.last_summary:
            status_box = layout.box()
            is_error = str(settings.status).startswith(("Error", "Unexpected"))
            status_box.alert = is_error
            status_box.label(
                text=str(settings.status),
                icon="ERROR" if is_error else "INFO",
            )
            if settings.last_summary:
                status_box.label(text=str(settings.last_summary))

    @classmethod
    def _draw_step_header(cls, layout: bpy.types.UILayout, step: int) -> None:
        box = layout.box()
        box.label(text=f"Step {step} of 6 — {cls._STEP_LABELS[step - 1]}")
        row = box.row(align=True)
        for index, label in enumerate(cls._STEP_LABELS, start=1):
            icon = "CHECKMARK" if index < step else "RADIOBUT_ON" if index == step else "RADIOBUT_OFF"
            row.label(text=str(index), icon=icon)
        current_name = cls._STEP_LABELS[step - 1]
        box.label(text=f"Current: {current_name}")

    @classmethod
    def _draw_area_step(
        cls,
        layout: bpy.types.UILayout,
        context: bpy.types.Context,
        settings: object,
    ) -> None:
        box = layout.box()
        box.label(text="1. Choose the map area", icon="WORLD_DATA")
        box.prop(settings, "area_choice", expand=True)

        if settings.area_choice == "NEW":
            box.prop(settings, "area_name", text="Map Search Hint", icon="VIEWZOOM")
            box.label(text="The hint only centers the browser map; you still draw the exact rectangle.")
            row = box.row()
            row.scale_y = 1.45
            row.operator("ovmg.select_area_on_map", text="Choose Area on Map", icon="URL")
        else:
            box.prop(settings, "previous_area_key")
            row = box.row()
            row.scale_y = 1.35
            row.operator("ovmg.use_previous_area", icon="TIME")
            box.label(text="Confirmed map rectangles are saved automatically.", icon="TIME")

        if settings.visual_selection_pending:
            cls._draw_pending_area(box, context, settings)
        elif settings.visual_area_selected:
            metrics = cls._metrics(context, settings)
            if metrics is not None:
                selected = box.box()
                selected.label(text="Current area is ready for review", icon="CHECKMARK")
                selected.label(text=f"{metrics.width_km:.2f} × {metrics.height_km:.2f} km")
                selected.label(text=f"Area: {metrics.area_square_km:.2f} km²")
                nav = box.row(align=True)
                nav.operator("ovmg.wizard_next", text="Review Area", icon="TRIA_RIGHT")

    @classmethod
    def _draw_confirm_step(
        cls,
        layout: bpy.types.UILayout,
        context: bpy.types.Context,
        settings: object,
    ) -> None:
        box = layout.box()
        box.label(text="2. Review and confirm the selected area", icon="IMAGE_DATA")
        if settings.visual_selection_pending:
            cls._draw_pending_area(box, context, settings)
            return
        metrics = cls._metrics(context, settings)
        if not settings.visual_area_selected or metrics is None:
            box.alert = True
            box.label(text="No confirmed area is available.", icon="ERROR")
            box.operator("ovmg.wizard_back", text="Back to Area Selection", icon="TRIA_LEFT")
            return

        summary = box.box()
        summary.label(text=str(settings.area_name), icon="WORLD_DATA")
        summary.label(text=f"Size: {metrics.width_km:.2f} × {metrics.height_km:.2f} km")
        summary.label(text=f"Area: {metrics.area_square_km:.2f} km²")
        summary.label(text=f"Estimated Load: {metrics.load_level.value.replace('_', ' ').title()}")
        actions = box.row(align=True)
        actions.operator("ovmg.frame_area_preview", text="Frame Area", icon="VIEWZOOM")
        actions.operator("ovmg.select_area_on_map", text="Change on Map", icon="URL")
        actions.operator("ovmg.clear_area_selection", text="Clear", icon="X")
        cls._draw_navigation(box, next_enabled=True)

    @staticmethod
    def _draw_style_step(layout: bpy.types.UILayout, settings: object) -> None:
        box = layout.box()
        box.label(text="3. Choose the 3D style", icon="MESH_CUBE")
        box.prop(settings, "model_style", expand=True)
        descriptions = {
            "CLASSIC_VOXEL": "True map footprints converted into optimized voxel blocks.",
            "MINECRAFT": "Coarse block grid, stepped heights, flat roofs, and no curved details.",
            "LOW_POLY": "Direct simplified footprints with faceted roofs and low-segment landmarks.",
            "REAL": "Direct high-fidelity footprints, source heights, parts, roofs, domes, and facade profiles.",
            "ARCHITECTURAL_MODEL": "Clean architectural city model: sand buildings, light roads, blue water, green parks, and source-accurate geometry.",
        }
        info = box.box()
        info.label(text=descriptions.get(str(settings.model_style), ""), icon="INFO")
        OVMG_PT_MainPanel._draw_navigation(box, next_enabled=True)

    @classmethod
    def _draw_quality_step(
        cls,
        layout: bpy.types.UILayout,
        context: bpy.types.Context,
        settings: object,
    ) -> None:
        box = layout.box()
        box.label(text="4. Choose conversion quality", icon="MOD_REMESH")
        box.prop(settings, "quality_preset", expand=True)
        metrics = cls._metrics(context, settings)
        if metrics is not None:
            load = metrics.load_level.value.replace("_", " ").title()
            info = box.box()
            info.label(text=f"Current area load at this quality: {load}")
            info.label(text=f"Estimated surface cells: {metrics.estimated_surface_cells:,}")
            if metrics.load_level is AreaLoadLevel.TOO_LARGE:
                info.alert = True
                info.label(text="The selected map is too large for this quality.", icon="ERROR")
                info.label(text="Make the map smaller or choose a lower quality.")
        box.label(text="High is the default for maximum accuracy; use Medium only for larger areas.")

        source = box.box()
        source.label(text="Building data source", icon="WORLD_DATA")
        source.prop(settings, "building_source", expand=True)
        source_help = {
            "HYBRID": "Recommended: Overture footprints fused with OSM architectural attributes and unmatched buildings.",
            "OVERTURE_ONLY": "Use Overture footprints only; roads, water, and other map layers still come from OSM.",
            "OSM_ONLY": "Use OpenStreetMap buildings only; fastest fallback when the Overture runtime is unavailable.",
        }
        source.label(
            text=source_help.get(str(settings.building_source), ""),
            icon="INFO",
        )
        cls._draw_navigation(box, next_enabled=True)

    @classmethod
    def _draw_material_step(
        cls,
        layout: bpy.types.UILayout,
        settings: object,
    ) -> None:
        box = layout.box()
        box.label(text="5. Choose materials", icon="MATERIAL")
        box.prop(settings, "material_style", expand=True)
        info = box.box()
        if settings.material_style == "REALISTIC":
            info.label(text="Strict real facades use only mapped material and colour evidence.")
            info.label(text="Buildings without facade evidence remain neutral and explicitly unverified.", icon="INFO")
            info.prop(settings, "strict_real_facades")
        else:
            info.label(text="Compact category colors for fast generation and game-engine export.")
        cls._draw_navigation(box, next_enabled=True)

    @classmethod
    def _draw_review_step(
        cls,
        layout: bpy.types.UILayout,
        context: bpy.types.Context,
        settings: object,
    ) -> None:
        box = layout.box()
        box.label(text="6. Review and generate", icon="CHECKMARK")
        metrics = cls._metrics(context, settings)
        summary = box.box()
        summary.label(text=f"Area: {settings.area_name}")
        if metrics is not None:
            summary.label(
                text=f"Size: {metrics.width_km:.2f} × {metrics.height_km:.2f} km"
            )
            summary.label(
                text=(
                    "Estimated Load: "
                    f"{metrics.load_level.value.replace('_', ' ').title()}"
                )
            )
        style_label = str(settings.model_style).replace("_", " ").title()
        summary.label(text=f"3D Style: {style_label}")
        summary.label(text=f"Quality: {str(settings.quality_preset).title()}")
        summary.label(text=f"Materials: {str(settings.material_style).title()}")
        source_label = {
            "HYBRID": "Hybrid — Recommended",
            "OVERTURE_ONLY": "Overture Only",
            "OSM_ONLY": "OSM Only",
        }.get(str(settings.building_source), str(settings.building_source))
        summary.label(text=f"Building Data: {source_label}")

        if bool(settings.generated_map_ready):
            success = box.box()
            success.label(text="Map generated successfully", icon="CHECKMARK")
            if settings.last_summary:
                success.label(text=str(settings.last_summary))
            evidence = box.box()
            evidence.label(text="Building data evidence", icon="INFO")
            for field in (
                "building_summary_1",
                "building_summary_2",
                "building_summary_3",
                "building_summary_4",
                "building_summary_5",
            ):
                value = str(getattr(settings, field, ""))
                if value:
                    evidence.label(text=value)

            export = box.box()
            export.label(text="Export is available in the dedicated Export Map panel.", icon="EXPORT")

            actions = box.row(align=True)
            actions.operator("ovmg.wizard_restart", text="Start New Map", icon="FILE_NEW")
            actions.operator("ovmg.delete_map", text="Delete Generated Map", icon="TRASH")
            return

        if metrics is not None:
            readiness = box.box()
            if metrics.load_level is AreaLoadLevel.SAFE:
                readiness.label(text="READY — This map can be generated.", icon="CHECKMARK")
                readiness.label(text="The selected area fits the current quality budget.")
            elif metrics.load_level is AreaLoadLevel.HEAVY:
                readiness.label(text="CAUTION — This map may be slow or memory intensive.", icon="INFO")
                readiness.label(text="For safer generation, reduce the area or choose a lower quality.")
            elif str(settings.large_area_mode) == "SPLIT_TILES":
                readiness.label(text="READY — The map will be split into aligned tiles.", icon="CHECKMARK")
                readiness.label(text="All tiles will be assembled inside one map project.")
            else:
                readiness.alert = True
                readiness.label(text="CANNOT GENERATE — The selected map is too large.", icon="CANCEL")
                readiness.label(text="Choose one of the safe options below.")
                choices = readiness.row(align=True)
                choices.operator("ovmg.select_area_on_map", text="Resize Map Area", icon="URL")
                choices.operator("ovmg.enable_map_tiling", text="Split Into Tiles", icon="MOD_ARRAY")

        runtime_blocks_generation = False
        if str(settings.building_source) != "OSM_ONLY":
            runtime = probe_overture_runtime()
            runtime_box = box.box()
            if runtime.available:
                runtime_box.label(
                    text="Overture building runtime ready",
                    icon="CHECKMARK",
                )
            else:
                runtime_box.alert = True
                runtime_box.label(
                    text="Overture building runtime unavailable",
                    icon="ERROR",
                )
                runtime_box.label(text=runtime.summary[:110])
                if str(settings.building_source) == "HYBRID":
                    runtime_box.label(
                        text="Generation will continue with OSM buildings only.",
                        icon="INFO",
                    )
                else:
                    runtime_box.label(
                        text="Overture Only cannot generate until this is fixed.",
                        icon="CANCEL",
                    )
                    runtime_blocks_generation = True
                actions = runtime_box.row(align=True)
                actions.operator("ovmg.use_osm_only", text="Use OSM Only")
                actions.operator(
                    "ovmg.copy_overture_diagnostics",
                    text="Copy Diagnostics",
                )

        can_generate = (
            bool(settings.visual_area_selected)
            and not bool(settings.visual_selection_pending)
            and not runtime_blocks_generation
        )
        if metrics is not None and metrics.load_level is AreaLoadLevel.TOO_LARGE:
            if settings.large_area_mode != "SPLIT_TILES":
                can_generate = False
        generate = box.row()
        generate.enabled = can_generate
        generate.scale_y = 1.6
        generate.operator("ovmg.generate_map", text="Generate Map", icon="MOD_BUILD")
        cls._draw_navigation(box, next_enabled=False)


    @classmethod
    def _draw_pending_area(
        cls,
        layout: bpy.types.UILayout,
        context: bpy.types.Context,
        settings: object,
    ) -> None:
        pending = layout.box()
        pending.alert = False
        pending.label(text="New Area Ready for Confirmation", icon="CHECKMARK")
        pending.label(text="The browser preview was received safely.")
        icon_id = BlenderSelectionThumbnailService.loaded_icon_id()
        if icon_id:
            pending.template_icon(icon_value=icon_id, scale=6.0)
        try:
            bounds = BoundingBox(
                south=float(settings.pending_south),
                west=float(settings.pending_west),
                north=float(settings.pending_north),
                east=float(settings.pending_east),
            )
            metrics = cls._metrics_for_bounds(context, settings, bounds)
        except (ValueError, RuntimeError):
            metrics = None
        if settings.pending_area_name:
            pending.label(text=str(settings.pending_area_name))
        if metrics is not None:
            pending.label(text=f"{metrics.width_km:.2f} × {metrics.height_km:.2f} km • {metrics.area_square_km:.2f} km²")
            pending.label(text=f"Estimated Load: {metrics.load_level.value.replace('_', ' ').title()}")
        confirm = pending.row()
        confirm.scale_y = 1.6
        confirm.operator(
            "ovmg.confirm_area_selection",
            text="Confirm Selected Area",
            icon="CHECKMARK",
            depress=True,
        )
        actions = pending.row(align=True)
        actions.operator("ovmg.open_pending_preview", text="Open Image", icon="IMAGE_DATA")
        actions.operator("ovmg.edit_pending_area", text="Back to Map", icon="URL")
        actions.operator("ovmg.discard_pending_area", text="Discard", icon="X")

    @staticmethod
    def _draw_navigation(box: bpy.types.UILayout, next_enabled: bool) -> None:
        nav = box.row(align=True)
        nav.operator("ovmg.wizard_back", text="Back", icon="TRIA_LEFT")
        if next_enabled:
            nav.operator("ovmg.wizard_next", text="Next", icon="TRIA_RIGHT")

    @classmethod
    def _metrics(
        cls,
        context: bpy.types.Context,
        settings: object,
    ) -> AreaMetrics | None:
        try:
            bounds = BoundingBox(
                south=float(settings.south),
                west=float(settings.west),
                north=float(settings.north),
                east=float(settings.east),
            )
            return cls._metrics_for_bounds(context, settings, bounds)
        except (ValueError, RuntimeError):
            return None


    @staticmethod
    def _metrics_for_bounds(
        context: bpy.types.Context,
        settings: object,
        bounds: BoundingBox,
    ) -> AreaMetrics | None:
        try:
            addon = context.preferences.addons.get(ADDON_PACKAGE)
            if addon is None:
                addon = context.preferences.addons.get(ADDON_ID)
            max_cells = int(addon.preferences.max_voxel_cells) if addon is not None else 12_000_000
            quality_voxel_size = {
                "LOW": 5.0,
                "MEDIUM": 2.5,
                "HIGH": 1.5,
            }.get(str(settings.quality_preset), 1.5)
            return AreaMetricsCalculator.calculate(
                bounds,
                quality_voxel_size,
                max_cells,
                quality_generation_multiplier(str(settings.quality_preset)),
            )
        except (ValueError, RuntimeError):
            return None


class OVMG_PT_ExportPanel(bpy.types.Panel):
    """Dedicated category-aware export workspace."""

    bl_idname = "OVMG_PT_export"
    bl_label = "Export Map"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = PANEL_CATEGORY
    bl_order = 20
    bl_options = {"DEFAULT_CLOSED"}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        settings = getattr(context.scene, "ovmg_settings", None)
        return settings is not None and bool(settings.generated_map_ready)

    def draw(self, context: bpy.types.Context) -> None:
        settings = context.scene.ovmg_settings
        layout = self.layout

        fmt = layout.box()
        fmt.label(text="1. File format", icon="FILE_3D")
        fmt.prop(settings, "export_format", expand=True)

        scope = layout.box()
        scope.label(text="2. Export content", icon="OUTLINER_COLLECTION")
        grid = scope.grid_flow(row_major=True, columns=2, even_columns=True)
        grid.prop(settings, "export_buildings")
        grid.prop(settings, "export_roads")
        grid.prop(settings, "export_bridges")
        grid.prop(settings, "export_terrain")
        grid.prop(settings, "export_water")
        grid.prop(settings, "export_green")
        grid.prop(settings, "export_trees")

        materials = layout.box()
        materials.label(text="3. Materials and metadata", icon="MATERIAL")
        materials.prop(settings, "export_include_materials")
        label_row = materials.row()
        label_row.enabled = bool(settings.generate_labels)
        label_row.prop(settings, "export_include_labels")
        materials.prop(settings, "export_apply_transforms")
        if str(settings.export_format) == "GLB" and bool(settings.export_include_materials):
            materials.label(text="GLB embeds supported materials in one file.", icon="INFO")
        all_categories = all(
            bool(getattr(settings, name))
            for name in (
                "export_buildings", "export_roads", "export_bridges",
                "export_terrain", "export_water", "export_green", "export_trees",
            )
        )
        if str(settings.export_format) == "BLEND" and not all_categories:
            warning = materials.box()
            warning.alert = True
            warning.label(text="BLEND can only save the complete scene.", icon="ERROR")

        action = layout.row()
        action.scale_y = 2.2
        action.operator("ovmg.export_map", text="Export Selected Content", icon="EXPORT")
