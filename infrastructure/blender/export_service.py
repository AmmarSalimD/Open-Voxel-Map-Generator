"""Simplified project exporter with format-specific Blender adapters."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
import json
import struct

import bpy

from ...core.exceptions import OVMGError
from ...core.naming import label_metadata_name
from ...domain.enums import ExportFormat, FeatureType
from .materials import MaterialFactory


class ProjectExportService:
    """Export one generated OVMG project and optional label metadata."""

    _EXTENSIONS: dict[ExportFormat, str] = {
        ExportFormat.GLB: ".glb",
        ExportFormat.FBX: ".fbx",
        ExportFormat.OBJ: ".obj",
        ExportFormat.USD: ".usd",
        ExportFormat.BLEND: ".blend",
    }

    def export(
        self,
        context: bpy.types.Context,
        project_name: str,
        filepath: str,
        export_format: ExportFormat,
        include_materials: bool,
        include_labels: bool,
        apply_transforms: bool,
        included_categories: set[FeatureType] | None = None,
    ) -> tuple[Path, Path | None]:
        """Export generated objects and return model and optional label paths."""
        path = self.ensure_extension(Path(filepath), export_format)
        path.parent.mkdir(parents=True, exist_ok=True)

        objects = self._project_objects(
            project_name,
            include_labels,
            included_categories,
        )
        if not objects:
            raise OVMGError(
                "No generated objects were found for this project. Generate the map "
                "or verify the Project Name before exporting."
            )

        previous_selection = list(context.selected_objects)
        previous_active = context.view_layer.objects.active
        material_state: list[tuple[object, list[object]]] = []
        try:
            self._select_only(context, objects)
            if include_materials:
                material_state = self._temporarily_assign_export_materials(objects)
            else:
                material_state = self._temporarily_remove_materials(objects)
            self._dispatch_export(
                path,
                export_format,
                include_materials,
                apply_transforms,
            )
            if export_format is ExportFormat.GLB and include_materials:
                self._verify_glb_materials(path)
        finally:
            self._restore_materials(material_state)
            self._restore_selection(context, previous_selection, previous_active)

        label_path = None
        if include_labels:
            label_path = self._write_label_sidecar(project_name, path)
        return path, label_path

    @classmethod
    def ensure_extension(cls, path: Path, export_format: ExportFormat) -> Path:
        """Return a path with the extension required by the selected format."""
        extension = cls._EXTENSIONS[export_format]
        if path.suffix.casefold() != extension:
            path = path.with_suffix(extension)
        return path

    @staticmethod
    def _project_objects(
        project_name: str,
        include_labels: bool,
        included_categories: set[FeatureType] | None = None,
    ) -> list[bpy.types.Object]:
        objects = []
        for obj in bpy.data.objects:
            if obj.get("ovmg_project") != project_name:
                continue
            if not obj.get("ovmg_generated"):
                continue
            if obj.type not in {"MESH", "CURVE", "FONT", "EMPTY"}:
                continue
            if obj.get("ovmg_label") and not include_labels:
                continue
            if str(obj.get("ovmg_material_variant", "")).startswith("accuracy|"):
                continue
            if obj.get("ovmg_label"):
                objects.append(obj)
                continue
            if obj.type == "EMPTY":
                continue
            if included_categories is not None:
                try:
                    category = FeatureType(str(obj.get("ovmg_category", "")))
                except ValueError:
                    continue
                if category not in included_categories:
                    continue
            objects.append(obj)
        return objects

    @staticmethod
    def _select_only(
        context: bpy.types.Context,
        objects: Iterable[bpy.types.Object],
    ) -> None:
        for obj in context.view_layer.objects:
            obj.select_set(False)
        selected = list(objects)
        for obj in selected:
            obj.hide_set(False)
            obj.select_set(True)
        context.view_layer.objects.active = selected[0]

    @staticmethod
    def _restore_selection(
        context: bpy.types.Context,
        objects: Iterable[bpy.types.Object],
        active: bpy.types.Object | None,
    ) -> None:
        for obj in context.view_layer.objects:
            obj.select_set(False)
        for obj in objects:
            if obj.name in bpy.data.objects:
                obj.select_set(True)
        if active is not None and active.name in bpy.data.objects:
            context.view_layer.objects.active = active

    @staticmethod
    def _temporarily_remove_materials(
        objects: Iterable[bpy.types.Object],
    ) -> list[tuple[object, list[object]]]:
        state: list[tuple[object, list[object]]] = []
        visited: set[int] = set()
        for obj in objects:
            data = getattr(obj, "data", None)
            materials = getattr(data, "materials", None)
            if materials is None or id(data) in visited:
                continue
            visited.add(id(data))
            saved = list(materials)
            if saved:
                state.append((data, saved))
                materials.clear()
        return state

    @staticmethod
    def _temporarily_assign_export_materials(
        objects: Iterable[bpy.types.Object],
    ) -> list[tuple[object, list[object]]]:
        """Swap OVMG materials for interchange-safe Principled materials.

        Enhanced viewport materials intentionally contain procedural Blender
        nodes. GLB, FBX, OBJ, and USD exporters cannot preserve those graphs as
        authored without a texture-baking pipeline. A temporary export palette
        guarantees that category colors and core PBR values are written to the
        model while leaving the Blender scene unchanged after export.
        """
        state: list[tuple[object, list[object]]] = []
        visited: set[int] = set()
        for obj in objects:
            data = getattr(obj, "data", None)
            materials = getattr(data, "materials", None)
            if materials is None or id(data) in visited:
                continue
            visited.add(id(data))
            saved = list(materials)
            if not saved:
                continue

            category_value = obj.get("ovmg_category")
            if not category_value:
                # Text labels and any future non-semantic objects keep their
                # existing simple material.
                continue
            try:
                category = FeatureType(str(category_value))
            except ValueError:
                continue

            state.append((data, saved))
            materials.clear()
            materials.append(
                MaterialFactory.get_or_create_export_compatible(
                    category, str(obj.get("ovmg_material_variant", ""))
                )
            )
        return state

    @staticmethod
    def _restore_materials(state: Iterable[tuple[object, list[object]]]) -> None:
        for data, materials in state:
            slots = getattr(data, "materials", None)
            if slots is None:
                continue
            slots.clear()
            for material in materials:
                slots.append(material)

    def _dispatch_export(
        self,
        path: Path,
        export_format: ExportFormat,
        include_materials: bool,
        apply_transforms: bool,
    ) -> None:
        if export_format is ExportFormat.GLB:
            self._call_operator(
                bpy.ops.export_scene.gltf,
                filepath=str(path),
                export_format="GLB",
                use_selection=True,
                export_materials="EXPORT" if include_materials else "NONE",
                export_extras=True,
                export_apply=apply_transforms,
                export_animations=False,
                check_existing=False,
            )
            return
        if export_format is ExportFormat.FBX:
            self._call_operator(
                bpy.ops.export_scene.fbx,
                filepath=str(path),
                use_selection=True,
                apply_unit_scale=True,
                bake_space_transform=apply_transforms,
                axis_forward="-Z",
                axis_up="Y",
                path_mode="COPY" if include_materials else "AUTO",
                embed_textures=include_materials,
                add_leaf_bones=False,
                bake_anim=False,
                check_existing=False,
            )
            return
        if export_format is ExportFormat.OBJ:
            self._call_operator(
                bpy.ops.wm.obj_export,
                filepath=str(path),
                export_selected_objects=True,
                export_materials=include_materials,
                apply_modifiers=True,
                forward_axis="NEGATIVE_Z",
                up_axis="Y",
                check_existing=False,
            )
            return
        if export_format is ExportFormat.USD:
            self._call_operator(
                bpy.ops.wm.usd_export,
                filepath=str(path),
                selected_objects_only=True,
                export_materials=include_materials,
                export_custom_properties=True,
                export_meshes=True,
                export_uvmaps=True,
                export_normals=True,
                check_existing=False,
            )
            return
        if export_format is ExportFormat.BLEND:
            self._call_operator(
                bpy.ops.wm.save_as_mainfile,
                filepath=str(path),
                copy=True,
                check_existing=False,
            )
            return
        raise OVMGError(f"Unsupported export format: {export_format.value}")

    @staticmethod
    def _call_operator(operator: object, **kwargs: object) -> None:
        """Call an operator using only parameters supported by this Blender build."""
        try:
            rna = operator.get_rna_type()
            supported = {
                prop.identifier
                for prop in rna.properties
                if prop.identifier != "rna_type"
            }
            filtered = {key: value for key, value in kwargs.items() if key in supported}
            result = operator(**filtered)
        except AttributeError as exc:
            raise OVMGError(
                "The selected exporter is unavailable in this Blender build."
            ) from exc
        except RuntimeError as exc:
            raise OVMGError(f"Blender export failed: {exc}") from exc
        if "FINISHED" not in result:
            raise OVMGError("Blender did not complete the export operation.")

    @staticmethod
    def _write_label_sidecar(project_name: str, model_path: Path) -> Path | None:
        text = bpy.data.texts.get(label_metadata_name(project_name))
        if text is None:
            return None
        raw = text.as_string()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise OVMGError("Generated label metadata is invalid JSON.") from exc
        sidecar = model_path.with_suffix(".labels.json")
        sidecar.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return sidecar

    @staticmethod
    def _verify_glb_materials(path: Path) -> None:
        """Verify that a generated GLB contains material definitions and links."""
        try:
            with path.open("rb") as stream:
                header = stream.read(12)
                if len(header) != 12:
                    raise ValueError("incomplete GLB header")
                magic, version, _total_length = struct.unpack("<4sII", header)
                if magic != b"glTF" or version != 2:
                    raise ValueError("unsupported GLB header")
                chunk_header = stream.read(8)
                if len(chunk_header) != 8:
                    raise ValueError("missing GLB JSON chunk")
                chunk_length, chunk_type = struct.unpack("<II", chunk_header)
                if chunk_type != 0x4E4F534A:
                    raise ValueError("first GLB chunk is not JSON")
                payload = stream.read(chunk_length).decode("utf-8").rstrip(" \t\r\n\0")
                document = json.loads(payload)
        except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
            raise OVMGError(
                "The GLB file was written, but its material data could not be "
                "validated. Try exporting again or use FBX."
            ) from exc

        materials = document.get("materials", [])
        meshes = document.get("meshes", [])
        linked_material = any(
            "material" in primitive
            for mesh in meshes
            for primitive in mesh.get("primitives", [])
        )
        if not materials or not linked_material:
            raise OVMGError(
                "GLB export completed without material assignments. The export "
                "was cancelled to avoid producing an incorrectly unshaded map."
            )
