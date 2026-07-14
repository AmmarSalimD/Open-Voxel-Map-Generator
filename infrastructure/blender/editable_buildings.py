"""Geometry and material helpers for persistent editable building objects."""

from __future__ import annotations

from collections.abc import Sequence
import json
from math import cos, pi, sin
import bpy
from mathutils import Vector
from mathutils.geometry import interpolate_bezier, tessellate_polygon

from ...domain.enums import FeatureType, QualityPreset
from .building_corrections import mark_user_building
from .materials import MaterialFactory

Point2D = tuple[float, float]
Ring2D = tuple[Point2D, ...]


def color_to_hex(color: Sequence[float]) -> str:
    """Convert a Blender linear-style RGBA tuple to a six-digit RGB token."""
    channels = [max(0, min(255, round(float(value) * 255.0))) for value in color[:3]]
    return "".join(f"{value:02x}" for value in channels)


def building_material_variant(
    profile: str,
    facade_color: Sequence[float],
    roof_color: Sequence[float],
    realistic: bool,
) -> str:
    """Return the compact material variant consumed by MaterialFactory."""
    detail = "facade" if realistic else "plain"
    return (
        f"building|{profile or 'generic_plaster'}|"
        f"{color_to_hex(facade_color)}|{color_to_hex(roof_color)}|{detail}"
    )


def primitive_footprint(shape: str, width: float, depth: float, segments: int) -> Ring2D:
    """Return a centered closed footprint for one simple building shape."""
    half_width = width * 0.5
    half_depth = depth * 0.5
    if shape == "CYLINDER" or shape == "DOME":
        count = max(8, segments)
        points = tuple(
            (
                cos(2.0 * pi * index / count) * half_width,
                sin(2.0 * pi * index / count) * half_depth,
            )
            for index in range(count)
        )
        return (*points, points[0])
    if shape == "L_SHAPE":
        points = (
            (-half_width, -half_depth),
            (half_width, -half_depth),
            (half_width, 0.0),
            (0.0, 0.0),
            (0.0, half_depth),
            (-half_width, half_depth),
        )
        return (*points, points[0])
    if shape == "U_SHAPE":
        arm = max(min(width, depth) * 0.28, min(width, depth) * 0.18)
        x1 = -half_width + arm
        x2 = half_width - arm
        y2 = half_depth - arm
        points = (
            (-half_width, -half_depth),
            (half_width, -half_depth),
            (half_width, half_depth),
            (x2, half_depth),
            (x2, -half_depth + arm),
            (x1, -half_depth + arm),
            (x1, half_depth),
            (-half_width, half_depth),
        )
        return (*points, points[0])
    points = (
        (-half_width, -half_depth),
        (half_width, -half_depth),
        (half_width, half_depth),
        (-half_width, half_depth),
    )
    return (*points, points[0])


def normalize_ring(points: Sequence[Sequence[float]]) -> Ring2D:
    """Return a closed ring without consecutive duplicate coordinates."""
    result: list[Point2D] = []
    for point in points:
        if len(point) < 2:
            continue
        value = (float(point[0]), float(point[1]))
        if not result or value != result[-1]:
            result.append(value)
    if len(result) >= 3 and result[0] != result[-1]:
        result.append(result[0])
    return tuple(result) if len(result) >= 4 else ()


def _ring_area(ring: Ring2D) -> float:
    return 0.5 * sum(
        ring[index][0] * ring[index + 1][1]
        - ring[index + 1][0] * ring[index][1]
        for index in range(len(ring) - 1)
    )


def _oriented(ring: Ring2D, clockwise: bool) -> Ring2D:
    if not ring:
        return ring
    is_clockwise = _ring_area(ring) < 0.0
    if is_clockwise == clockwise:
        return ring
    core = tuple(reversed(ring[:-1]))
    return (*core, core[0])


def _point_in_ring(point: Point2D, ring: Ring2D) -> bool:
    x, y = point
    inside = False
    for index in range(len(ring) - 1):
        x1, y1 = ring[index]
        x2, y2 = ring[index + 1]
        crosses = (y1 > y) != (y2 > y)
        if crosses:
            intersection = (x2 - x1) * (y - y1) / (y2 - y1) + x1
            if x < intersection:
                inside = not inside
    return inside


def localize_rings(
    outer_rings: Sequence[Sequence[Sequence[float]]],
    inner_rings: Sequence[Sequence[Sequence[float]]] = (),
) -> tuple[tuple[Ring2D, ...], tuple[Ring2D, ...], Point2D]:
    """Center source rings around a stable object origin for convenient editing."""
    outer = tuple(filter(None, (normalize_ring(ring) for ring in outer_rings)))
    inner = tuple(filter(None, (normalize_ring(ring) for ring in inner_rings)))
    if not outer:
        raise ValueError("The building footprint does not contain a valid outer ring.")
    coordinates = [point for ring in outer for point in ring[:-1]]
    center = (
        sum(point[0] for point in coordinates) / len(coordinates),
        sum(point[1] for point in coordinates) / len(coordinates),
    )

    def local(ring: Ring2D) -> Ring2D:
        return tuple((x - center[0], y - center[1]) for x, y in ring)

    return tuple(local(ring) for ring in outer), tuple(local(ring) for ring in inner), center


def prism_geometry(
    outer_rings: Sequence[Ring2D],
    inner_rings: Sequence[Ring2D],
    height: float,
) -> tuple[list[tuple[float, float, float]], list[tuple[int, ...]]]:
    """Extrude polygon rings into one manifold-like editable prism mesh."""
    vertices: list[tuple[float, float, float]] = []
    faces: list[tuple[int, ...]] = []
    height = max(0.05, float(height))

    for outer in outer_rings:
        outer = _oriented(normalize_ring(outer), clockwise=False)
        if not outer:
            continue
        holes = [
            _oriented(normalize_ring(ring), clockwise=True)
            for ring in inner_rings
            if ring and _point_in_ring(ring[0], outer)
        ]
        loops = [outer, *holes]
        bottom_lookup: dict[tuple[float, float], int] = {}
        top_lookup: dict[tuple[float, float], int] = {}
        vector_loops: list[list[Vector]] = []
        for ring in loops:
            core = ring[:-1]
            vector_loop: list[Vector] = []
            for x, y in core:
                key = (round(x, 8), round(y, 8))
                bottom_lookup[key] = len(vertices)
                vertices.append((x, y, 0.0))
                top_lookup[key] = len(vertices)
                vertices.append((x, y, height))
                vector_loop.append(Vector((x, y, 0.0)))
            vector_loops.append(vector_loop)
            for index in range(len(core)):
                nxt = (index + 1) % len(core)
                current_key = (round(core[index][0], 8), round(core[index][1], 8))
                next_key = (round(core[nxt][0], 8), round(core[nxt][1], 8))
                side = (
                    bottom_lookup[current_key],
                    bottom_lookup[next_key],
                    top_lookup[next_key],
                    top_lookup[current_key],
                )
                faces.append(tuple(reversed(side)) if ring is not outer else side)

        # Blender 5.1 returns indices into the flattened input-point list.
        # Some older builds returned Vector-like values, so accept both forms.
        flattened_keys = [
            (round(value.x, 8), round(value.y, 8))
            for vector_loop in vector_loops
            for value in vector_loop
        ]
        for triangle in tessellate_polygon(vector_loops):
            keys: list[tuple[float, float]] = []
            for value in triangle:
                if isinstance(value, int):
                    if value < 0 or value >= len(flattened_keys):
                        keys = []
                        break
                    keys.append(flattened_keys[value])
                    continue
                if hasattr(value, "x") and hasattr(value, "y"):
                    keys.append((round(float(value.x), 8), round(float(value.y), 8)))
                    continue
                if isinstance(value, Sequence) and len(value) >= 2:
                    keys.append((round(float(value[0]), 8), round(float(value[1]), 8)))
                    continue
                keys = []
                break
            if len(keys) != 3 or not all(key in bottom_lookup for key in keys):
                continue
            faces.append(tuple(reversed(tuple(bottom_lookup[key] for key in keys))))
            faces.append(tuple(top_lookup[key] for key in keys))
    return vertices, faces


def _roof_geometry(
    roof_shape: str,
    width: float,
    depth: float,
    roof_height: float,
    segments: int,
) -> tuple[list[tuple[float, float, float]], list[tuple[int, ...]]]:
    half_width = width * 0.5
    half_depth = depth * 0.5
    roof_height = max(0.05, roof_height)
    if roof_shape == "GABLED":
        if width >= depth:
            vertices = [
                (-half_width, -half_depth, 0.0),
                (half_width, -half_depth, 0.0),
                (half_width, half_depth, 0.0),
                (-half_width, half_depth, 0.0),
                (-half_width, 0.0, roof_height),
                (half_width, 0.0, roof_height),
            ]
            faces = [(0, 1, 5, 4), (3, 4, 5, 2), (0, 4, 3), (1, 2, 5)]
        else:
            vertices = [
                (-half_width, -half_depth, 0.0),
                (half_width, -half_depth, 0.0),
                (half_width, half_depth, 0.0),
                (-half_width, half_depth, 0.0),
                (0.0, -half_depth, roof_height),
                (0.0, half_depth, roof_height),
            ]
            faces = [(0, 4, 5, 3), (1, 2, 5, 4), (0, 1, 4), (3, 5, 2)]
        return vertices, faces
    if roof_shape in {"HIPPED", "PYRAMID"}:
        vertices = [
            (-half_width, -half_depth, 0.0),
            (half_width, -half_depth, 0.0),
            (half_width, half_depth, 0.0),
            (-half_width, half_depth, 0.0),
            (0.0, 0.0, roof_height),
        ]
        return vertices, [(0, 1, 4), (1, 2, 4), (2, 3, 4), (3, 0, 4)]
    if roof_shape in {"DOME", "ONION"}:
        count = max(8, segments)
        rings = max(3, count // 4)
        vertices: list[tuple[float, float, float]] = []
        for ring_index in range(rings):
            phi = (pi * 0.5) * ring_index / rings
            radius = cos(phi)
            z = sin(phi) * roof_height
            if roof_shape == "ONION":
                radius *= 1.0 + 0.22 * sin(phi * pi)
                z *= 1.2
            for index in range(count):
                angle = 2.0 * pi * index / count
                vertices.append(
                    (
                        cos(angle) * half_width * radius,
                        sin(angle) * half_depth * radius,
                        z,
                    )
                )
        top = len(vertices)
        vertices.append((0.0, 0.0, roof_height * (1.35 if roof_shape == "ONION" else 1.0)))
        faces: list[tuple[int, ...]] = []
        for ring_index in range(rings - 1):
            start = ring_index * count
            next_start = (ring_index + 1) * count
            for index in range(count):
                nxt = (index + 1) % count
                faces.append((start + index, start + nxt, next_start + nxt, next_start + index))
        last = (rings - 1) * count
        for index in range(count):
            faces.append((last + index, last + (index + 1) % count, top))
        return vertices, faces
    if roof_shape == "CONE":
        count = max(8, segments)
        vertices = [
            (
                cos(2.0 * pi * index / count) * half_width,
                sin(2.0 * pi * index / count) * half_depth,
                0.0,
            )
            for index in range(count)
        ]
        top = len(vertices)
        vertices.append((0.0, 0.0, roof_height))
        return vertices, [
            (index, (index + 1) % count, top) for index in range(count)
        ]
    return [], []


def assign_building_material(
    obj: bpy.types.Object,
    profile: str,
    facade_color: Sequence[float],
    roof_color: Sequence[float],
    realistic: bool,
    quality: QualityPreset,
) -> str:
    """Assign a compact source-aware building material and return its variant."""
    variant = building_material_variant(
        profile,
        facade_color,
        roof_color,
        realistic,
    )
    material = MaterialFactory(enhanced=realistic, quality=quality).get_or_create(
        FeatureType.BUILDING,
        variant,
    )
    if obj.data is not None and hasattr(obj.data, "materials"):
        obj.data.materials.clear()
        obj.data.materials.append(material)
    obj["ovmg_material_variant"] = variant
    obj["ovmg_facade_profile"] = profile
    obj["ovmg_facade_color"] = color_to_hex(facade_color)
    obj["ovmg_roof_color"] = color_to_hex(roof_color)
    return variant


def create_editable_building(
    *,
    name: str,
    outer_rings: Sequence[Sequence[Sequence[float]]],
    inner_rings: Sequence[Sequence[Sequence[float]]],
    base_z: float,
    total_height: float,
    roof_shape: str,
    roof_height: float,
    rotation_z: float,
    collection: bpy.types.Collection,
    project_name: str,
    source_id: str,
    correction_kind: str,
    profile: str,
    facade_color: Sequence[float],
    roof_color: Sequence[float],
    realistic_material: bool,
    quality: QualityPreset,
    roof_segments: int,
) -> bpy.types.Object:
    """Create one editable body plus an optional parented roof component."""
    localized_outer, localized_inner, center = localize_rings(outer_rings, inner_rings)
    roof_shape = roof_shape.upper()
    effective_roof_height = (
        min(max(0.0, roof_height), max(0.0, total_height - 0.25))
        if roof_shape != "FLAT"
        else 0.0
    )
    wall_height = max(0.25, total_height - effective_roof_height)
    vertices, faces = prism_geometry(localized_outer, localized_inner, wall_height)
    mesh = bpy.data.meshes.new(f"{name}_Mesh")
    mesh.from_pydata(vertices, [], faces)
    mesh.validate(verbose=False, clean_customdata=False)
    mesh.update()
    obj = bpy.data.objects.new(name, mesh)
    obj.location = (center[0], center[1], base_z)
    obj.rotation_euler[2] = rotation_z
    collection.objects.link(obj)
    mark_user_building(obj, project_name, source_id, correction_kind)
    obj["ovmg_user_building_root"] = True
    obj["ovmg_roof_shape"] = roof_shape
    obj["ovmg_roof_height_m"] = effective_roof_height
    obj["ovmg_total_height_m"] = total_height
    obj["ovmg_footprint_outer_json"] = json.dumps(localized_outer)
    obj["ovmg_footprint_inner_json"] = json.dumps(localized_inner)
    obj["ovmg_editable_geometry_kind"] = "FOOTPRINT"
    assign_building_material(
        obj,
        profile,
        facade_color,
        roof_color,
        realistic_material,
        quality,
    )

    if roof_shape != "FLAT" and effective_roof_height > 0.0:
        width = max(point[0] for ring in localized_outer for point in ring[:-1]) - min(
            point[0] for ring in localized_outer for point in ring[:-1]
        )
        depth = max(point[1] for ring in localized_outer for point in ring[:-1]) - min(
            point[1] for ring in localized_outer for point in ring[:-1]
        )
        roof_vertices, roof_faces = _roof_geometry(
            roof_shape,
            max(0.25, width),
            max(0.25, depth),
            effective_roof_height,
            roof_segments,
        )
        if roof_faces:
            roof_mesh = bpy.data.meshes.new(f"{name}_Roof_Mesh")
            roof_mesh.from_pydata(roof_vertices, [], roof_faces)
            roof_mesh.validate(verbose=False, clean_customdata=False)
            roof_mesh.update()
            roof = bpy.data.objects.new(f"{name}_Roof", roof_mesh)
            roof.location = (0.0, 0.0, wall_height)
            roof.parent = obj
            collection.objects.link(roof)
            mark_user_building(roof, project_name, source_id, correction_kind)
            roof["ovmg_user_building_component"] = "ROOF"
            assign_building_material(
                roof,
                profile,
                roof_color,
                roof_color,
                realistic_material,
                quality,
            )
    return obj



def _stored_local_rings(
    root: bpy.types.Object,
) -> tuple[tuple[Ring2D, ...], tuple[Ring2D, ...]] | None:
    """Read the editable local footprint stored on a generated correction root."""
    outer_raw = str(root.get("ovmg_footprint_outer_json", ""))
    inner_raw = str(root.get("ovmg_footprint_inner_json", ""))
    if not outer_raw:
        return None
    try:
        outer_data = json.loads(outer_raw)
        inner_data = json.loads(inner_raw or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    outer = tuple(filter(None, (normalize_ring(ring) for ring in outer_data)))
    inner = tuple(filter(None, (normalize_ring(ring) for ring in inner_data)))
    return (outer, inner) if outer else None


def _scaled_local_rings(
    outer_rings: Sequence[Ring2D],
    inner_rings: Sequence[Ring2D],
    target_width: float,
    target_depth: float,
) -> tuple[tuple[Ring2D, ...], tuple[Ring2D, ...]]:
    """Scale editable footprint rings around their local origin."""
    points = [point for ring in outer_rings for point in ring[:-1]]
    current_width = max(point[0] for point in points) - min(point[0] for point in points)
    current_depth = max(point[1] for point in points) - min(point[1] for point in points)
    scale_x = max(0.001, float(target_width)) / max(0.001, current_width)
    scale_y = max(0.001, float(target_depth)) / max(0.001, current_depth)

    def scale(ring: Ring2D) -> Ring2D:
        return tuple((x * scale_x, y * scale_y) for x, y in ring)

    return tuple(scale(ring) for ring in outer_rings), tuple(
        scale(ring) for ring in inner_rings
    )


def _delete_child_components(root: bpy.types.Object) -> None:
    """Remove generated roof/detail descendants without touching the root object."""
    descendants = list(building_hierarchy(root))[1:]
    for obj in reversed(descendants):
        data = obj.data
        bpy.data.objects.remove(obj, do_unlink=True)
        if data is not None and getattr(data, "users", 1) == 0:
            if isinstance(data, bpy.types.Mesh):
                bpy.data.meshes.remove(data)


def _replace_root_mesh(
    root: bpy.types.Object,
    vertices: Sequence[tuple[float, float, float]],
    faces: Sequence[tuple[int, ...]],
) -> None:
    """Replace one editable root mesh while preserving object identity and metadata."""
    old_data = root.data
    mesh = bpy.data.meshes.new(f"{root.name}_Mesh")
    mesh.from_pydata(list(vertices), [], list(faces))
    mesh.validate(verbose=False, clean_customdata=False)
    mesh.update()
    root.data = mesh
    if old_data is not None and getattr(old_data, "users", 1) == 0:
        if isinstance(old_data, bpy.types.Mesh):
            bpy.data.meshes.remove(old_data)


def _append_roof_component(
    root: bpy.types.Object,
    collection: bpy.types.Collection,
    source_id: str,
    correction_kind: str,
    profile: str,
    facade_color: Sequence[float],
    roof_color: Sequence[float],
    realistic_material: bool,
    quality: QualityPreset,
    roof_shape: str,
    roof_height: float,
    wall_height: float,
    outer_rings: Sequence[Ring2D],
    roof_segments: int,
) -> None:
    """Create the optional editable roof child for one correction root."""
    if roof_shape == "FLAT" or roof_height <= 0.0:
        return
    width = max(point[0] for ring in outer_rings for point in ring[:-1]) - min(
        point[0] for ring in outer_rings for point in ring[:-1]
    )
    depth = max(point[1] for ring in outer_rings for point in ring[:-1]) - min(
        point[1] for ring in outer_rings for point in ring[:-1]
    )
    roof_vertices, roof_faces = _roof_geometry(
        roof_shape,
        max(0.25, width),
        max(0.25, depth),
        roof_height,
        roof_segments,
    )
    if not roof_faces:
        return
    roof_mesh = bpy.data.meshes.new(f"{root.name}_Roof_Mesh")
    roof_mesh.from_pydata(roof_vertices, [], roof_faces)
    roof_mesh.validate(verbose=False, clean_customdata=False)
    roof_mesh.update()
    roof = bpy.data.objects.new(f"{root.name}_Roof", roof_mesh)
    roof.location = (0.0, 0.0, wall_height)
    roof.parent = root
    collection.objects.link(roof)
    mark_user_building(roof, str(root.get("ovmg_project", "")), source_id, correction_kind)
    roof["ovmg_user_building_component"] = "ROOF"
    assign_building_material(
        roof,
        profile,
        roof_color,
        roof_color,
        realistic_material,
        quality,
    )


def rebuild_editable_building(
    root: bpy.types.Object,
    *,
    target_width: float,
    target_depth: float,
    base_z: float,
    total_height: float,
    roof_shape: str,
    roof_height: float,
    rotation_z: float,
    profile: str,
    facade_color: Sequence[float],
    roof_color: Sequence[float],
    realistic_material: bool,
    quality: QualityPreset,
    roof_segments: int,
) -> bool:
    """Rebuild an OVMG footprint building in place and preserve its object identity.

    Returns ``False`` for imported/custom meshes that have no OVMG footprint metadata;
    callers may then fall back to ordinary Blender transform scaling.
    """
    rings = _stored_local_rings(root)
    if rings is None:
        return False
    outer_rings, inner_rings = _scaled_local_rings(
        rings[0],
        rings[1],
        target_width,
        target_depth,
    )
    roof_shape = str(roof_shape).upper()
    effective_roof_height = (
        min(max(0.0, float(roof_height)), max(0.0, float(total_height) - 0.25))
        if roof_shape != "FLAT"
        else 0.0
    )
    wall_height = max(0.25, float(total_height) - effective_roof_height)
    vertices, faces = prism_geometry(outer_rings, inner_rings, wall_height)
    if not faces:
        raise ValueError("The editable footprint could not be rebuilt.")

    _delete_child_components(root)
    _replace_root_mesh(root, vertices, faces)
    root.scale = (1.0, 1.0, 1.0)
    root.location.z = float(base_z)
    root.rotation_euler.z = float(rotation_z)
    root["ovmg_footprint_outer_json"] = json.dumps(outer_rings)
    root["ovmg_footprint_inner_json"] = json.dumps(inner_rings)
    root["ovmg_roof_shape"] = roof_shape
    root["ovmg_roof_height_m"] = effective_roof_height
    root["ovmg_total_height_m"] = float(total_height)
    root["ovmg_editor_width_m"] = float(target_width)
    root["ovmg_editor_depth_m"] = float(target_depth)
    root["ovmg_editor_total_height_m"] = float(total_height)
    root["ovmg_editor_base_height_m"] = float(base_z)
    root["ovmg_editor_roof_height_m"] = effective_roof_height
    root["ovmg_editor_roof_shape"] = roof_shape
    assign_building_material(
        root,
        profile,
        facade_color,
        roof_color,
        realistic_material,
        quality,
    )

    collections = tuple(root.users_collection)
    if collections:
        source_id = str(root.get("ovmg_replaces_source_id", ""))
        correction_kind = str(root.get("ovmg_correction_kind", "ADDED"))
        _append_roof_component(
            root,
            collections[0],
            source_id,
            correction_kind,
            profile,
            facade_color,
            roof_color,
            realistic_material,
            quality,
            roof_shape,
            effective_roof_height,
            wall_height,
            outer_rings,
            max(8, int(roof_segments)),
        )
    return True


def curve_world_rings(obj: bpy.types.Object, samples_per_segment: int = 8) -> tuple[Ring2D, ...]:
    """Sample closed Curve splines into world-space XY footprint rings."""
    if obj.type != "CURVE":
        raise ValueError("Select a closed Curve object to use as a footprint.")
    rings: list[Ring2D] = []
    matrix = obj.matrix_world
    for spline in obj.data.splines:
        if not spline.use_cyclic_u:
            continue
        points: list[Point2D] = []
        if spline.type == "POLY":
            for point in spline.points:
                world = matrix @ Vector(point.co[:3])
                points.append((world.x, world.y))
        elif spline.type == "BEZIER":
            bezier = spline.bezier_points
            for index, current in enumerate(bezier):
                nxt = bezier[(index + 1) % len(bezier)]
                segment = interpolate_bezier(
                    current.co,
                    current.handle_right,
                    nxt.handle_left,
                    nxt.co,
                    max(2, samples_per_segment + 1),
                )
                for value in segment[:-1]:
                    world = matrix @ value
                    points.append((world.x, world.y))
        ring = normalize_ring(points)
        if ring:
            rings.append(ring)
    if not rings:
        raise ValueError("The selected Curve has no closed POLY or BEZIER spline.")
    return tuple(rings)


def root_user_building(obj: bpy.types.Object | None) -> bpy.types.Object | None:
    """Resolve a selected roof child back to its editable root building."""
    current = obj
    while current is not None:
        if current.get("ovmg_user_building_root"):
            return current
        current = current.parent
    return None


def building_hierarchy(root: bpy.types.Object) -> tuple[bpy.types.Object, ...]:
    """Return one editable root followed by every descendant component."""
    result: list[bpy.types.Object] = [root]
    stack = list(root.children)
    while stack:
        current = stack.pop()
        result.append(current)
        stack.extend(current.children)
    return tuple(result)


def remove_building_hierarchy(root: bpy.types.Object) -> list[bpy.types.Object]:
    """Delete one editable building root and all of its generated components."""
    targets = list(reversed(building_hierarchy(root)))
    for obj in targets:
        data = obj.data
        bpy.data.objects.remove(obj, do_unlink=True)
        if data is not None and getattr(data, "users", 1) == 0:
            if isinstance(data, bpy.types.Mesh):
                bpy.data.meshes.remove(data)
    return targets
