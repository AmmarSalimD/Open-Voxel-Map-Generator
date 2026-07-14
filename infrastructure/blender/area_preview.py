"""Temporary Blender geometry previewing the selected geographic bounds."""

from __future__ import annotations

import bpy

from ...domain.models import BoundingBox
from ..map_selector.metrics import AreaMetricsCalculator

_PREVIEW_COLLECTION = "OVMG_Area_Preview"
_PREVIEW_TAG = "ovmg_area_preview"


class BlenderAreaPreviewService:
    """Create and remove lightweight non-exportable area preview geometry."""

    @classmethod
    def create(
        cls,
        scene: bpy.types.Scene,
        bounds: BoundingBox,
        voxel_size: float,
        max_voxel_cells: int,
    ) -> bpy.types.Object:
        """Replace the current preview with an outline centered at the origin."""
        cls.remove(scene)
        metrics = AreaMetricsCalculator.calculate(
            bounds,
            voxel_size,
            max_voxel_cells,
        )
        collection = bpy.data.collections.new(_PREVIEW_COLLECTION)
        collection[_PREVIEW_TAG] = True
        scene.collection.children.link(collection)

        width = metrics.width_meters
        height = metrics.height_meters
        half_width = width * 0.5
        half_height = height * 0.5
        z = 0.55
        line_width = max(0.15, min(4.0, min(width, height) * 0.0015))

        curve = bpy.data.curves.new("OVMG_Area_Preview_Outline", type="CURVE")
        curve.dimensions = "3D"
        curve.resolution_u = 1
        curve.bevel_depth = line_width
        curve.bevel_resolution = 0
        spline = curve.splines.new("POLY")
        spline.points.add(4)
        points = (
            (-half_width, -half_height, z, 1.0),
            (half_width, -half_height, z, 1.0),
            (half_width, half_height, z, 1.0),
            (-half_width, half_height, z, 1.0),
            (-half_width, -half_height, z, 1.0),
        )
        for point, value in zip(spline.points, points, strict=True):
            point.co = value

        outline = bpy.data.objects.new("OVMG_Area_Preview_Outline", curve)
        outline.hide_render = True
        outline[_PREVIEW_TAG] = True
        outline["ovmg_width_m"] = width
        outline["ovmg_height_m"] = height
        outline["ovmg_area_km2"] = metrics.area_square_km
        outline["ovmg_south"] = bounds.south
        outline["ovmg_west"] = bounds.west
        outline["ovmg_north"] = bounds.north
        outline["ovmg_east"] = bounds.east
        collection.objects.link(outline)
        curve.materials.append(cls._preview_material())

        cls._create_north_indicator(collection, half_height, line_width, z)
        return outline

    @classmethod
    def remove(cls, scene: bpy.types.Scene | None = None) -> int:
        """Remove all area-preview objects and data blocks."""
        collections = [
            collection
            for collection in bpy.data.collections
            if collection.get(_PREVIEW_TAG, False)
            or collection.name.startswith(_PREVIEW_COLLECTION)
        ]
        removed = 0
        for collection in collections:
            objects = list(collection.objects)
            for obj in objects:
                data = obj.data
                bpy.data.objects.remove(obj, do_unlink=True)
                removed += 1
                if isinstance(data, bpy.types.Curve) and data.users == 0:
                    bpy.data.curves.remove(data)
                elif isinstance(data, bpy.types.Mesh) and data.users == 0:
                    bpy.data.meshes.remove(data)
            if collection.users == 0 or scene is None:
                bpy.data.collections.remove(collection)
            else:
                for parent in bpy.data.collections:
                    if collection.name in parent.children:
                        parent.children.unlink(collection)
                if collection.name in scene.collection.children:
                    scene.collection.children.unlink(collection)
                bpy.data.collections.remove(collection)
        return removed

    @staticmethod
    def find_outline() -> bpy.types.Object | None:
        """Return the current preview outline, if present."""
        return next(
            (
                obj
                for obj in bpy.data.objects
                if obj.get(_PREVIEW_TAG, False)
                and obj.name.startswith("OVMG_Area_Preview_Outline")
            ),
            None,
        )

    @staticmethod
    def _preview_material() -> bpy.types.Material:
        name = "OVMG_Area_Preview_Material"
        material = bpy.data.materials.get(name)
        if material is None:
            material = bpy.data.materials.new(name=name)
            material.diffuse_color = (0.95, 0.55, 0.08, 1.0)
            material.use_nodes = True
            if material.node_tree is not None:
                shader = material.node_tree.nodes.get("Principled BSDF")
                if shader is not None:
                    base = shader.inputs.get("Base Color")
                    roughness = shader.inputs.get("Roughness")
                    emission = shader.inputs.get("Emission Color") or shader.inputs.get(
                        "Emission"
                    )
                    emission_strength = shader.inputs.get("Emission Strength")
                    if base is not None:
                        base.default_value = (0.95, 0.55, 0.08, 1.0)
                    if roughness is not None:
                        roughness.default_value = 0.45
                    if emission is not None:
                        emission.default_value = (0.20, 0.06, 0.01, 1.0)
                    if emission_strength is not None:
                        emission_strength.default_value = 0.35
        return material

    @classmethod
    def _create_north_indicator(
        cls,
        collection: bpy.types.Collection,
        half_height: float,
        line_width: float,
        z: float,
    ) -> None:
        length = max(10.0, min(150.0, half_height * 0.16))
        y0 = half_height - length * 1.25
        y1 = half_height - length * 0.20
        head = length * 0.22
        curve = bpy.data.curves.new("OVMG_Area_Preview_North", type="CURVE")
        curve.dimensions = "3D"
        curve.bevel_depth = line_width * 0.75
        curve.bevel_resolution = 0
        for points in (
            ((0.0, y0, z), (0.0, y1, z)),
            ((0.0, y1, z), (-head, y1 - head, z)),
            ((0.0, y1, z), (head, y1 - head, z)),
        ):
            spline = curve.splines.new("POLY")
            spline.points.add(1)
            for point, value in zip(spline.points, points, strict=True):
                point.co = (*value, 1.0)
        obj = bpy.data.objects.new("OVMG_Area_Preview_North", curve)
        obj.hide_render = True
        obj[_PREVIEW_TAG] = True
        collection.objects.link(obj)
        curve.materials.append(cls._preview_material())
