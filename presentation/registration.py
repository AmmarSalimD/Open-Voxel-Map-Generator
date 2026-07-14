"""Central Blender class registration."""

from __future__ import annotations

import bpy
from bpy.props import PointerProperty

from ..core.exceptions import OVMGError
from ..domain.models import BoundingBox
from ..infrastructure.blender.selection_thumbnail import (
    BlenderSelectionThumbnailService,
)
from ..infrastructure.map_selector.session import MapSelectorSessionManager
from .building_editor import (
    OVMG_OT_MakeInspectedBuildingEditable,
    OVMG_OT_OpenBuildingEditor,
    OVMG_OT_PickBuildingInteractive,
    OVMG_OT_PlaceBuildingInteractive,
    OVMG_OT_ResetBuildingEditorDefaults,
)
from .operators import (
    OVMG_OT_AddUserBuilding,
    OVMG_OT_AboutAddon,
    OVMG_OT_AddBuildingFromCurve,
    OVMG_OT_LoadInspectedBuilding,
    OVMG_OT_CreateBuildingReplacement,
    OVMG_OT_MarkInspectedBuildingDeleted,
    OVMG_OT_RestoreInspectedBuilding,
    OVMG_OT_LoadSelectedUserBuilding,
    OVMG_OT_ApplySelectedUserBuilding,
    OVMG_OT_DuplicateSelectedUserBuilding,
    OVMG_OT_SnapSelectedUserBuilding,
    OVMG_OT_RegenerateWithCorrections,
    OVMG_OT_RestoreRecommendedDefaults,
    OVMG_OT_ClearBuildingCorrections,
    OVMG_OT_ClearAreaSelection,
    OVMG_OT_ConfirmAreaSelection,
    OVMG_OT_ConvertSelectedToUserBuilding,
    OVMG_OT_DeleteMap,
    OVMG_OT_DeleteSelectedUserBuildings,
    OVMG_OT_DiscardPendingArea,
    OVMG_OT_EditGenerationSettings,
    OVMG_OT_EditPendingArea,
    OVMG_OT_EnableMapTiling,
    OVMG_OT_ExportMap,
    OVMG_OT_FrameAreaPreview,
    OVMG_OT_GenerateMap,
    OVMG_OT_OpenPendingPreview,
    OVMG_OT_CopyOvertureDiagnostics,
    OVMG_OT_SelectAreaOnMap,
    OVMG_OT_SelectUserBuildings,
    OVMG_OT_UseOsmOnly,
    OVMG_OT_UsePreviousArea,
    OVMG_OT_InspectNearestBuilding,
    OVMG_OT_ToggleAccuracyOverlay,
    OVMG_OT_WizardBack,
    OVMG_OT_WizardNext,
    OVMG_OT_WizardRestart,
    _poll_map_selector_result,
    _english_area_name,
)
from .panels import OVMG_PT_ExportPanel, OVMG_PT_MainPanel
from .properties import (
    OVMG_AddonPreferences,
    OVMG_SceneSettings,
    restore_recommended_generation_defaults,
)

_CLASSES = (
    OVMG_AddonPreferences,
    OVMG_SceneSettings,
    OVMG_OT_AboutAddon,
    OVMG_OT_SelectAreaOnMap,
    OVMG_OT_ConfirmAreaSelection,
    OVMG_OT_OpenPendingPreview,
    OVMG_OT_EditPendingArea,
    OVMG_OT_DiscardPendingArea,
    OVMG_OT_ClearAreaSelection,
    OVMG_OT_FrameAreaPreview,
    OVMG_OT_UsePreviousArea,
    OVMG_OT_UseOsmOnly,
    OVMG_OT_CopyOvertureDiagnostics,
    OVMG_OT_EditGenerationSettings,
    OVMG_OT_EnableMapTiling,
    OVMG_OT_WizardNext,
    OVMG_OT_WizardBack,
    OVMG_OT_WizardRestart,
    OVMG_OT_RestoreRecommendedDefaults,
    OVMG_OT_GenerateMap,
    OVMG_OT_OpenBuildingEditor,
    OVMG_OT_PickBuildingInteractive,
    OVMG_OT_PlaceBuildingInteractive,
    OVMG_OT_ResetBuildingEditorDefaults,
    OVMG_OT_MakeInspectedBuildingEditable,
    OVMG_OT_AddUserBuilding,
    OVMG_OT_AddBuildingFromCurve,
    OVMG_OT_LoadInspectedBuilding,
    OVMG_OT_CreateBuildingReplacement,
    OVMG_OT_MarkInspectedBuildingDeleted,
    OVMG_OT_RestoreInspectedBuilding,
    OVMG_OT_LoadSelectedUserBuilding,
    OVMG_OT_ApplySelectedUserBuilding,
    OVMG_OT_DuplicateSelectedUserBuilding,
    OVMG_OT_SnapSelectedUserBuilding,
    OVMG_OT_RegenerateWithCorrections,
    OVMG_OT_ClearBuildingCorrections,
    OVMG_OT_ConvertSelectedToUserBuilding,
    OVMG_OT_SelectUserBuildings,
    OVMG_OT_DeleteSelectedUserBuildings,
    OVMG_OT_InspectNearestBuilding,
    OVMG_OT_ToggleAccuracyOverlay,
    OVMG_OT_ExportMap,
    OVMG_OT_DeleteMap,
    OVMG_PT_MainPanel,
    OVMG_PT_ExportPanel,
)


def _initialize_scene_settings() -> float | None:
    """Apply migrated defaults after Blender exposes its scene data.

    Blender uses a restricted ``bpy.data`` proxy while an extension is being
    installed.  Accessing ``bpy.data.scenes`` during ``register()`` therefore
    raises ``'_RestrictData' object has no attribute 'scenes'``.  A timer runs
    this migration on the first normal application tick instead.
    """
    scenes = getattr(bpy.data, "scenes", None)
    if scenes is None:
        return 0.1

    for scene in scenes:
        settings = getattr(scene, "ovmg_settings", None)
        if settings is None:
            continue
        if int(getattr(settings, "settings_schema_version", 0)) < 186:
            restore_recommended_generation_defaults(settings)
            settings.settings_schema_version = 186
        try:
            saved_bounds = BoundingBox(
                south=float(settings.south),
                west=float(settings.west),
                north=float(settings.north),
                east=float(settings.east),
            )
            saved_bounds.validate()
        except (OVMGError, TypeError, ValueError):
            saved_bounds = None
        settings.area_name = _english_area_name(
            str(settings.area_name),
            saved_bounds,
        )
        settings.label_language = "ENGLISH"
        if str(getattr(settings, "pending_area_name", "")).strip():
            settings.pending_area_name = _english_area_name(
                str(settings.pending_area_name)
            )
    return None


def register_addon() -> None:
    """Register all extension classes and scene state."""
    registered_now = []
    try:
        for cls in _CLASSES:
            existing = getattr(bpy.types, cls.__name__, None)
            if existing is cls:
                continue
            if existing is not None:
                try:
                    bpy.utils.unregister_class(existing)
                except (RuntimeError, ValueError):
                    pass
            bpy.utils.register_class(cls)
            registered_now.append(cls)

        bpy.types.Scene.ovmg_settings = PointerProperty(type=OVMG_SceneSettings)
        if not bpy.app.timers.is_registered(_initialize_scene_settings):
            bpy.app.timers.register(_initialize_scene_settings, first_interval=0.0)
    except Exception:
        # Do not leave Blender in a half-registered state after a failed install.
        if hasattr(bpy.types.Scene, "ovmg_settings"):
            del bpy.types.Scene.ovmg_settings
        for cls in reversed(registered_now):
            try:
                bpy.utils.unregister_class(cls)
            except (RuntimeError, ValueError):
                pass
        raise


def unregister_addon() -> None:
    """Stop localhost resources and unregister classes in reverse order."""
    MapSelectorSessionManager.stop()
    if bpy.app.timers.is_registered(_initialize_scene_settings):
        bpy.app.timers.unregister(_initialize_scene_settings)
    if bpy.app.timers.is_registered(_poll_map_selector_result):
        bpy.app.timers.unregister(_poll_map_selector_result)
    scenes = getattr(bpy.data, "scenes", ())
    for scene in scenes:
        if hasattr(scene, "ovmg_settings"):
            BlenderSelectionThumbnailService.remove(scene.ovmg_settings)
    BlenderSelectionThumbnailService.shutdown()
    if hasattr(bpy.types.Scene, "ovmg_settings"):
        del bpy.types.Scene.ovmg_settings
    for cls in reversed(_CLASSES):
        if getattr(bpy.types, cls.__name__, None) is cls:
            bpy.utils.unregister_class(cls)
