"""Blender implementation of the generated-scene repository port."""

from __future__ import annotations

from collections.abc import Iterable
import json
from math import degrees

import bpy

from ...core.constants import (
    ADDON_VERSION,
    GENERATED_COLLECTION_NAME,
    METADATA_OBJECT_NAME,
)
from ...core.naming import (
    building_metadata_name,
    category_collection_name,
    label_metadata_name,
    project_collection_name,
    sanitize_name,
)
from ...domain.enums import FeatureType, LabelKind, LabelMode, MaterialStyle, ModelStyle
from ...domain.models import (
    BoundingBox,
    CurvedDetailPayload,
    LabelPayload,
    MeshPayload,
    ProjectStatistics,
    VoxelSettings,
)
from ...domain.ports import ProgressCallback, SceneRepository
from .materials import MaterialFactory


class BlenderSceneRepository(SceneRepository):
    """Write optimized meshes, optional details, labels, and metadata."""

    def __init__(self, scene: bpy.types.Scene) -> None:
        self._scene = scene

    def replace_project(
        self,
        project_name: str,
        bounds: BoundingBox,
        settings: VoxelSettings,
        meshes: Iterable[MeshPayload],
        curved_details: Iterable[CurvedDetailPayload],
        labels: Iterable[LabelPayload],
        statistics: ProjectStatistics,
        progress: ProgressCallback,
    ) -> int:
        """Replace an existing project with newly generated scene content."""
        self.delete_project(project_name)
        root = self._create_collection(project_collection_name(project_name))
        self._scene.collection.children.link(root)
        generated = self._create_collection(
            f"{GENERATED_COLLECTION_NAME}_{sanitize_name(project_name)}"
        )
        root.children.link(generated)

        category_collections = {
            category: self._create_collection(
                category_collection_name(f"{project_name}_{category.value}")
            )
            for category in FeatureType
        }
        for collection in category_collections.values():
            generated.children.link(collection)
        grouped_collections: dict[str, bpy.types.Collection] = {}

        materials = MaterialFactory(
            enhanced=(
                settings.enhanced_materials
                or settings.material_style is MaterialStyle.REALISTIC
            ),
            quality=settings.quality_preset,
        )
        payloads = list(meshes)
        detail_payloads = list(curved_details)
        label_payloads = list(labels)
        label_object_count = (
            len(label_payloads)
            if settings.generate_labels
            and settings.label_mode is LabelMode.BLENDER_TEXT
            else 0
        )
        total_steps = max(1, len(payloads) + len(detail_payloads) + label_object_count)
        completed = 0
        created = 0

        for payload in payloads:
            target_collection = category_collections[payload.category]
            if payload.collection_group:
                target_collection = grouped_collections.get(payload.collection_group)
                if target_collection is None:
                    target_collection = self._create_collection(
                        category_collection_name(
                            f"{project_name}_{payload.collection_group}"
                        )
                    )
                    generated.children.link(target_collection)
                    grouped_collections[payload.collection_group] = target_collection
            self._create_mesh_object(
                payload.name,
                payload.vertices,
                payload.faces,
                payload.category,
                target_collection,
                materials,
                project_name,
                chunk_x=payload.chunk_x,
                chunk_y=payload.chunk_y,
                horizontal_voxel_size=settings.voxel_size,
                vertical_step=settings.vertical_step,
                material_variant=payload.material_variant,
                display_name=payload.display_name,
            )
            created += 1
            completed += 1
            progress(completed / total_steps, f"Writing {payload.name}")

        if detail_payloads:
            detail_collection = self._create_collection(
                category_collection_name(f"{project_name}_Curved_Details")
            )
            generated.children.link(detail_collection)
            for payload in detail_payloads:
                obj = self._create_mesh_object(
                    payload.name,
                    payload.vertices,
                    payload.faces,
                    payload.category,
                    detail_collection,
                    materials,
                    project_name,
                    material_variant=(
                        "architectural|building"
                        if settings.model_style is ModelStyle.ARCHITECTURAL_MODEL
                        else ""
                    ),
                )
                obj["ovmg_curved_detail"] = True
                obj["ovmg_detail_kind"] = payload.kind.value
                obj["ovmg_source_id"] = payload.source_id
                created += 1
                completed += 1
                progress(completed / total_steps, f"Writing {payload.name}")

        label_collection = None
        if label_payloads:
            label_collection = self._create_collection(
                category_collection_name(f"{project_name}_Labels")
            )
            generated.children.link(label_collection)
            self._write_label_metadata(project_name, label_payloads)
            if settings.label_mode is LabelMode.BLENDER_TEXT:
                label_material = self._get_or_create_label_material()
                for index, payload in enumerate(label_payloads):
                    self._create_text_label(
                        project_name,
                        payload,
                        index,
                        label_collection,
                        label_material,
                    )
                    created += 1
                    completed += 1
                    progress(completed / total_steps, f"Writing label {payload.text}")

        if statistics.building_records:
            self._write_building_metadata(project_name, statistics)

        self._create_metadata_object(
            root,
            project_name,
            bounds,
            settings,
            statistics,
            created,
            has_label_metadata=bool(label_payloads),
        )
        root["ovmg_generated"] = True
        root["ovmg_project"] = project_name
        root["ovmg_version"] = ADDON_VERSION
        return created

    def delete_project(self, project_name: str) -> int:
        """Delete generated objects, meshes, curves, collections, and label data."""
        root = bpy.data.collections.get(project_collection_name(project_name))
        if root is None:
            self._remove_label_metadata(project_name)
            self._remove_building_metadata(project_name)
            return 0

        objects = self._collect_objects_recursive(root)
        mesh_data = {
            obj.data for obj in objects if isinstance(obj.data, bpy.types.Mesh)
        }
        curve_data = {
            obj.data for obj in objects if isinstance(obj.data, bpy.types.Curve)
        }
        for obj in objects:
            bpy.data.objects.remove(obj, do_unlink=True)
        for mesh in mesh_data:
            if mesh.users == 0:
                bpy.data.meshes.remove(mesh)
        for curve in curve_data:
            if curve.users == 0:
                bpy.data.curves.remove(curve)

        collections = self._collect_collections_postorder(root)
        for collection in collections:
            bpy.data.collections.remove(collection)
        self._remove_label_metadata(project_name)
        self._remove_building_metadata(project_name)
        return len(objects)

    @staticmethod
    def _create_mesh_object(
        name: str,
        vertices: Iterable[tuple[float, float, float]],
        faces: Iterable[tuple[int, ...]],
        category: FeatureType,
        collection: bpy.types.Collection,
        materials: MaterialFactory,
        project_name: str,
        chunk_x: int | None = None,
        chunk_y: int | None = None,
        horizontal_voxel_size: float | None = None,
        vertical_step: float | None = None,
        material_variant: str = "",
        display_name: str = "",
    ) -> bpy.types.Object:
        mesh = bpy.data.meshes.new(f"{name}_Mesh")
        mesh.from_pydata(list(vertices), [], list(faces))
        mesh.validate(verbose=False, clean_customdata=False)
        mesh.update(calc_edges=False, calc_edges_loose=False)

        obj = bpy.data.objects.new(name, mesh)
        obj["ovmg_generated"] = True
        obj["ovmg_project"] = project_name
        obj["ovmg_category"] = category.value
        obj["ovmg_material_variant"] = material_variant
        if display_name:
            obj["ovmg_display_name"] = display_name
        if chunk_x is not None:
            obj["ovmg_chunk_x"] = chunk_x
        if chunk_y is not None:
            obj["ovmg_chunk_y"] = chunk_y
        if horizontal_voxel_size is not None:
            obj["ovmg_horizontal_voxel_size_m"] = horizontal_voxel_size
        if vertical_step is not None:
            obj["ovmg_vertical_step_m"] = vertical_step
        mesh.materials.append(materials.get_or_create(category, material_variant))
        collection.objects.link(obj)
        return obj

    @staticmethod
    def _create_text_label(
        project_name: str,
        payload: LabelPayload,
        index: int,
        collection: bpy.types.Collection,
        material: bpy.types.Material,
    ) -> bpy.types.Object:
        safe_text = sanitize_name(payload.text, fallback=f"Label_{index}")
        curve = bpy.data.curves.new(
            name=f"OVMG_LabelCurve_{safe_text}_{index:04d}",
            type="FONT",
        )
        curve.body = payload.text
        curve.align_x = "CENTER"
        curve.align_y = "CENTER"
        curve.size = {
            LabelKind.AREA: 7.0,
            LabelKind.STREET: 2.2,
            LabelKind.LANDMARK: 3.2,
        }[payload.kind]
        curve.extrude = 0.025
        curve.bevel_depth = 0.008
        curve.materials.append(material)
        obj = bpy.data.objects.new(
            f"OVMG_Label_{payload.kind.value}_{safe_text}_{index:04d}",
            curve,
        )
        obj.location = payload.position
        obj.rotation_euler[2] = payload.rotation_z
        obj["ovmg_generated"] = True
        obj["ovmg_project"] = project_name
        obj["ovmg_label"] = True
        obj["ovmg_label_kind"] = payload.kind.value
        obj["ovmg_label_text"] = payload.text
        obj["ovmg_label_importance"] = payload.importance
        obj["ovmg_source_id"] = payload.source_id
        collection.objects.link(obj)
        return obj

    @staticmethod
    def _get_or_create_label_material() -> bpy.types.Material:
        name = "OVMG_MAT_Labels"
        material = bpy.data.materials.get(name)
        if material is None:
            material = bpy.data.materials.new(name=name)
        material.use_nodes = True
        material.diffuse_color = (0.95, 0.95, 0.90, 1.0)
        node_tree = material.node_tree
        if node_tree is not None:
            shader = node_tree.nodes.get("Principled BSDF")
            if shader is not None:
                base = shader.inputs.get("Base Color")
                roughness = shader.inputs.get("Roughness")
                emission = shader.inputs.get("Emission Color") or shader.inputs.get(
                    "Emission"
                )
                emission_strength = shader.inputs.get("Emission Strength")
                if base is not None:
                    base.default_value = material.diffuse_color
                if roughness is not None:
                    roughness.default_value = 0.55
                if emission is not None:
                    emission.default_value = (0.10, 0.10, 0.08, 1.0)
                if emission_strength is not None:
                    emission_strength.default_value = 0.12
        return material

    @staticmethod
    def _write_label_metadata(
        project_name: str,
        labels: list[LabelPayload],
    ) -> None:
        name = label_metadata_name(project_name)
        text = bpy.data.texts.get(name)
        if text is None:
            text = bpy.data.texts.new(name)
        else:
            text.clear()
        payload = {
            "schema": "ovmg-labels-1.0",
            "project": project_name,
            "coordinate_space": "Blender local meters, Z-up",
            "labels": [
                {
                    "source_id": label.source_id,
                    "kind": label.kind.value,
                    "text": label.text,
                    "name": label.local_name,
                    "name_ar": label.arabic_name,
                    "name_en": label.english_name,
                    "position_blender_xyz": list(label.position),
                    "position_unity_xzy_hint": [
                        label.position[0],
                        label.position[2],
                        label.position[1],
                    ],
                    "rotation_z_blender_radians": label.rotation_z,
                    "heading_degrees_from_local_x": degrees(label.rotation_z),
                    "importance": label.importance,
                }
                for label in labels
            ],
        }
        text.write(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        text["ovmg_generated"] = True
        text["ovmg_project"] = project_name

    @staticmethod
    def _write_building_metadata(
        project_name: str,
        statistics: ProjectStatistics,
    ) -> None:
        """Store inspectable per-building provenance without thousands of empties."""
        name = building_metadata_name(project_name)
        text = bpy.data.texts.get(name)
        if text is None:
            text = bpy.data.texts.new(name)
        else:
            text.clear()
        payload = {
            "schema": "ovmg-building-accuracy-2.0",
            "project": project_name,
            "coordinate_space": "Blender local meters, Z-up",
            "summary": {
                "high": statistics.building_accuracy.high_confidence,
                "medium": statistics.building_accuracy.medium_confidence,
                "low": statistics.building_accuracy.low_confidence,
                "very_low": statistics.building_accuracy.very_low_confidence,
                "source_facades": statistics.building_accuracy.source_facades,
                "unavailable_facades": statistics.building_accuracy.unavailable_facades,
                "procedural_facades": statistics.building_accuracy.procedural_facades,
                "tagged_roofs": statistics.building_accuracy.tagged_roofs,
                "inferred_roofs": statistics.building_accuracy.inferred_roofs,
            },
            "buildings": [
                {
                    "source_id": record.source_id,
                    "position": list(record.position),
                    "height_m": record.height_m,
                    "minimum_height_m": record.minimum_height_m,
                    "height_source": record.height_source.value,
                    "footprint_source": record.footprint_source.value,
                    "roof_shape": record.roof_shape,
                    "roof_source": record.roof_source.value,
                    "facade_source": record.facade_source.value,
                    "facade_profile": record.facade_profile,
                    "building_type": record.building_type,
                    "confidence": record.confidence.value,
                    "confidence_score": record.confidence_score,
                    "source_datasets": record.source_datasets,
                    "roof_height_m": record.roof_height_m,
                    "facade_color": record.facade_color,
                    "roof_color": record.roof_color,
                    "outer_rings_xy": [
                        [list(point) for point in ring]
                        for ring in record.outer_rings_xy
                    ],
                    "inner_rings_xy": [
                        [list(point) for point in ring]
                        for ring in record.inner_rings_xy
                    ],
                    "is_building_part": record.is_building_part,
                    "parent_source_id": record.parent_source_id,
                }
                for record in statistics.building_records
            ],
        }
        text.write(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        text["ovmg_generated"] = True
        text["ovmg_project"] = project_name

    @staticmethod
    def _remove_building_metadata(project_name: str) -> None:
        text = bpy.data.texts.get(building_metadata_name(project_name))
        if text is not None:
            bpy.data.texts.remove(text)

    @staticmethod
    def _remove_label_metadata(project_name: str) -> None:
        text = bpy.data.texts.get(label_metadata_name(project_name))
        if text is not None:
            bpy.data.texts.remove(text)

    @staticmethod
    def _create_collection(name: str) -> bpy.types.Collection:
        existing = bpy.data.collections.get(name)
        if existing is not None:
            bpy.data.collections.remove(existing)
        return bpy.data.collections.new(name)

    @staticmethod
    def _collect_objects_recursive(
        root: bpy.types.Collection,
    ) -> set[bpy.types.Object]:
        objects = set(root.objects)
        for child in root.children:
            objects.update(BlenderSceneRepository._collect_objects_recursive(child))
        return objects

    @staticmethod
    def _collect_collections_postorder(
        root: bpy.types.Collection,
    ) -> list[bpy.types.Collection]:
        result: list[bpy.types.Collection] = []
        for child in list(root.children):
            result.extend(BlenderSceneRepository._collect_collections_postorder(child))
        result.append(root)
        return result

    @staticmethod
    def _create_metadata_object(
        root: bpy.types.Collection,
        project_name: str,
        bounds: BoundingBox,
        settings: VoxelSettings,
        statistics: ProjectStatistics,
        created_objects: int,
        has_label_metadata: bool,
    ) -> bpy.types.Object:
        metadata = bpy.data.objects.new(
            f"{METADATA_OBJECT_NAME}_{sanitize_name(project_name)}",
            None,
        )
        metadata.empty_display_type = "PLAIN_AXES"
        metadata.empty_display_size = max(1.0, settings.voxel_size)
        metadata.hide_render = True
        metadata["ovmg_generated"] = True
        metadata["ovmg_project"] = project_name
        metadata["ovmg_version"] = ADDON_VERSION
        metadata["ovmg_crs"] = "EPSG:4326 source / local metric Blender space"
        metadata["ovmg_south"] = bounds.south
        metadata["ovmg_west"] = bounds.west
        metadata["ovmg_north"] = bounds.north
        metadata["ovmg_east"] = bounds.east
        metadata["ovmg_center_latitude"] = bounds.center.latitude
        metadata["ovmg_center_longitude"] = bounds.center.longitude
        metadata["ovmg_horizontal_voxel_size_m"] = settings.voxel_size
        metadata["ovmg_vertical_step_m"] = settings.vertical_step
        metadata["ovmg_quality_preset"] = settings.quality_preset.value
        metadata["ovmg_enhanced_materials"] = settings.enhanced_materials
        metadata["ovmg_material_style"] = settings.material_style.value
        metadata["ovmg_model_style"] = settings.model_style.value
        metadata["ovmg_geometry_style"] = settings.geometry_style.value
        metadata["ovmg_building_source"] = settings.building_source.value
        metadata["ovmg_use_building_parts"] = settings.use_building_parts
        metadata["ovmg_use_roof_shapes"] = settings.use_roof_shapes
        metadata["ovmg_generate_labels"] = settings.generate_labels
        metadata["ovmg_label_mode"] = settings.label_mode.value
        metadata["ovmg_label_metadata"] = (
            label_metadata_name(project_name) if has_label_metadata else ""
        )
        metadata["ovmg_building_accuracy_metadata"] = (
            building_metadata_name(project_name)
            if statistics.building_records
            else ""
        )
        metadata["ovmg_attribution"] = " | ".join(statistics.attributions)
        metadata["ovmg_runtime_warnings"] = " | ".join(statistics.warnings)
        metadata["ovmg_source_feature_count"] = statistics.source_feature_count
        metadata["ovmg_voxel_count"] = statistics.voxel_count
        metadata["ovmg_chunk_count"] = statistics.chunk_count
        metadata["ovmg_map_tile_count"] = statistics.tile_count
        metadata["ovmg_object_count"] = created_objects
        metadata["ovmg_curved_detail_count"] = statistics.curved_detail_count
        metadata["ovmg_label_count"] = statistics.label_count
        building = statistics.building_statistics
        metadata["ovmg_osm_buildings"] = building.osm_buildings
        metadata["ovmg_overture_buildings"] = building.overture_buildings
        metadata["ovmg_overture_parts"] = building.overture_parts
        metadata["ovmg_merged_duplicates"] = building.merged_duplicates
        metadata["ovmg_final_building_features"] = building.final_building_features
        metadata["ovmg_real_height_count"] = building.real_height
        metadata["ovmg_real_levels_count"] = building.real_levels
        metadata["ovmg_building_parts_count"] = building.building_parts
        metadata["ovmg_inferred_height_count"] = building.inferred_height
        metadata["ovmg_default_height_count"] = building.default_height
        metadata["ovmg_overture_release"] = building.overture_release
        accuracy = statistics.building_accuracy
        metadata["ovmg_accuracy_high"] = accuracy.high_confidence
        metadata["ovmg_accuracy_medium"] = accuracy.medium_confidence
        metadata["ovmg_accuracy_low"] = accuracy.low_confidence
        metadata["ovmg_accuracy_very_low"] = accuracy.very_low_confidence
        metadata["ovmg_source_facades"] = accuracy.source_facades
        metadata["ovmg_unavailable_facades"] = accuracy.unavailable_facades
        metadata["ovmg_procedural_facades"] = accuracy.procedural_facades
        metadata["ovmg_tagged_roofs"] = accuracy.tagged_roofs
        metadata["ovmg_inferred_roofs"] = accuracy.inferred_roofs
        metadata["ovmg_category_counts_json"] = json.dumps(
            {key.value: value for key, value in statistics.category_counts.items()},
            sort_keys=True,
        )
        root.objects.link(metadata)
        return metadata
