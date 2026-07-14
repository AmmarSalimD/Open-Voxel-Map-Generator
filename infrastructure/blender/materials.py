"""Blender material creation for semantic, facade, and accuracy rendering."""

from __future__ import annotations

from colorsys import hsv_to_rgb, rgb_to_hsv
import re

import bpy

from ...core.constants import PROJECT_PREFIX
from ...domain.enums import FeatureType, QualityPreset

_HEX_PATTERN = re.compile(r"^[0-9a-fA-F]{6}$")


class MaterialFactory:
    """Create a compact palette with optional source-aware facade profiles."""

    _COLORS: dict[FeatureType, tuple[float, float, float, float]] = {
        FeatureType.TERRAIN: (0.24, 0.16, 0.08, 1.0),
        FeatureType.WATER: (0.035, 0.25, 0.58, 1.0),
        FeatureType.GREEN: (0.10, 0.38, 0.10, 1.0),
        FeatureType.ROAD: (0.075, 0.085, 0.10, 1.0),
        FeatureType.BRIDGE: (0.30, 0.28, 0.25, 1.0),
        FeatureType.BUILDING: (0.62, 0.50, 0.34, 1.0),
        FeatureType.TREE: (0.035, 0.23, 0.045, 1.0),
    }
    _MINECRAFT_COLORS: dict[FeatureType, tuple[float, float, float, float]] = {
        FeatureType.TERRAIN: (0.34, 0.23, 0.12, 1.0),
        FeatureType.WATER: (0.04, 0.34, 0.88, 1.0),
        FeatureType.GREEN: (0.12, 0.54, 0.10, 1.0),
        FeatureType.ROAD: (0.10, 0.11, 0.13, 1.0),
        FeatureType.BRIDGE: (0.38, 0.34, 0.27, 1.0),
        FeatureType.BUILDING: (0.72, 0.58, 0.37, 1.0),
        FeatureType.TREE: (0.04, 0.34, 0.05, 1.0),
    }
    _CLASSIC_VOXEL_COLORS: dict[FeatureType, tuple[float, float, float, float]] = {
        FeatureType.TERRAIN: (0.28, 0.20, 0.12, 1.0),
        FeatureType.WATER: (0.035, 0.28, 0.64, 1.0),
        FeatureType.GREEN: (0.12, 0.42, 0.12, 1.0),
        FeatureType.ROAD: (0.085, 0.095, 0.11, 1.0),
        FeatureType.BRIDGE: (0.34, 0.31, 0.27, 1.0),
        FeatureType.BUILDING: (0.66, 0.54, 0.38, 1.0),
        FeatureType.TREE: (0.04, 0.27, 0.05, 1.0),
    }
    _ARCHITECTURAL_COLORS: dict[FeatureType, tuple[float, float, float, float]] = {
        FeatureType.TERRAIN: (0.76, 0.76, 0.73, 1.0),
        FeatureType.WATER: (0.34, 0.68, 0.82, 1.0),
        FeatureType.GREEN: (0.18, 0.58, 0.27, 1.0),
        FeatureType.ROAD: (0.90, 0.91, 0.91, 1.0),
        FeatureType.BRIDGE: (0.82, 0.80, 0.74, 1.0),
        FeatureType.BUILDING: (0.72, 0.62, 0.45, 1.0),
        FeatureType.TREE: (0.10, 0.42, 0.17, 1.0),
    }
    _ACCURACY_COLORS = {
        "high": (0.08, 0.72, 0.20, 1.0),
        "medium": (0.10, 0.42, 0.90, 1.0),
        "low": (0.95, 0.62, 0.08, 1.0),
        "very_low": (0.90, 0.08, 0.06, 1.0),
    }

    def __init__(
        self,
        enhanced: bool = False,
        quality: QualityPreset = QualityPreset.MEDIUM,
    ) -> None:
        self._enhanced = enhanced
        self._quality = quality

    def get_or_create(
        self,
        category: FeatureType,
        variant: str = "",
    ) -> bpy.types.Material:
        """Return a material matching category, facade profile, and visual mode."""
        mode = "Realistic" if self._enhanced else "Simple"
        safe_variant = self._safe_variant(variant)
        suffix = f"_{safe_variant}" if safe_variant else ""
        name = (
            f"{PROJECT_PREFIX}_MAT_{category.value}_{mode}_"
            f"{self._quality.value}{suffix}"
        )
        material = bpy.data.materials.get(name)
        if material is None:
            material = bpy.data.materials.new(name=name)
        material.use_nodes = True

        parsed = self._parse_variant(category, variant)
        material.diffuse_color = parsed["base_color"]
        if parsed["kind"] == "accuracy":
            self._configure_accuracy(material, parsed["base_color"])
        elif category is FeatureType.BUILDING and parsed["kind"] == "building":
            self._configure_building(material, parsed)
        elif parsed.get("profile") == "minecraft_block":
            self._configure_basic(material, category, parsed["base_color"])
        elif parsed.get("profile") == "architectural_model":
            self._configure_architectural(
                material,
                category,
                parsed["base_color"],
            )
        elif self._enhanced:
            self._configure_enhanced(material, category, parsed["base_color"])
        else:
            self._configure_basic(material, category, parsed["base_color"])
        material["ovmg_category"] = category.value
        material["ovmg_material_variant"] = variant
        material["ovmg_enhanced"] = self._enhanced
        material["ovmg_quality"] = self._quality.value
        return material

    @classmethod
    def get_or_create_export_compatible(
        cls,
        category: FeatureType,
        variant: str = "",
    ) -> bpy.types.Material:
        """Return a glTF/FBX-safe Principled material preserving profile color."""
        parsed = cls(enhanced=False)._parse_variant(category, variant)
        safe_variant = cls._safe_variant(variant)
        suffix = f"_{safe_variant}" if safe_variant else ""
        name = f"{PROJECT_PREFIX}_MAT_{category.value}_ExportSafe{suffix}"
        material = bpy.data.materials.get(name)
        if material is None:
            material = bpy.data.materials.new(name=name)
        material.use_nodes = True
        material.diffuse_color = parsed["base_color"]
        factory = cls(enhanced=False, quality=QualityPreset.MEDIUM)
        if parsed["kind"] == "accuracy":
            factory._configure_accuracy(material, parsed["base_color"], emission=False)
        else:
            factory._configure_basic(material, category, parsed["base_color"])
        node_tree = material.node_tree
        if node_tree is not None:
            shader = node_tree.nodes.get("Principled BSDF")
            if shader is not None:
                factory._set_input(
                    shader,
                    ("Metallic",),
                    0.12 if parsed.get("profile") == "commercial_glass" else (
                        0.04 if category is FeatureType.WATER else 0.0
                    ),
                )
                if category is FeatureType.WATER:
                    factory._set_input(shader, ("Coat Weight", "Clearcoat"), 0.22)
                    factory._set_input(
                        shader,
                        ("Coat Roughness", "Clearcoat Roughness"),
                        0.15,
                    )
        material["ovmg_category"] = category.value
        material["ovmg_material_variant"] = variant
        material["ovmg_export_safe"] = True
        return material

    def _configure_basic(
        self,
        material: bpy.types.Material,
        category: FeatureType,
        color: tuple[float, float, float, float] | None = None,
    ) -> None:
        node_tree = material.node_tree
        if node_tree is None:
            return
        nodes = node_tree.nodes
        nodes.clear()
        output = nodes.new("ShaderNodeOutputMaterial")
        shader = nodes.new("ShaderNodeBsdfPrincipled")
        output.location = (320.0, 0.0)
        shader.location = (0.0, 0.0)
        node_tree.links.new(shader.outputs["BSDF"], output.inputs["Surface"])
        self._set_input(shader, ("Base Color",), color or self._COLORS[category])
        self._set_input(shader, ("Roughness",), self._roughness(category))
        self._set_input(shader, ("Metallic",), 0.0)

    def _configure_architectural(
        self,
        material: bpy.types.Material,
        category: FeatureType,
        color: tuple[float, float, float, float],
    ) -> None:
        """Create a clean presentation material for architectural city models."""
        self._configure_basic(material, category, color)
        node_tree = material.node_tree
        if node_tree is None:
            return
        shader = node_tree.nodes.get("Principled BSDF")
        if shader is None:
            return
        roughness = {
            FeatureType.WATER: 0.18,
            FeatureType.ROAD: 0.78,
            FeatureType.BRIDGE: 0.64,
            FeatureType.BUILDING: 0.58,
            FeatureType.GREEN: 0.82,
            FeatureType.TERRAIN: 0.86,
            FeatureType.TREE: 0.80,
        }[category]
        self._set_input(shader, ("Roughness",), roughness)
        if category is FeatureType.WATER:
            self._set_input(shader, ("Metallic",), 0.03)
            self._set_input(shader, ("Coat Weight", "Clearcoat"), 0.28)
            self._set_input(
                shader,
                ("Coat Roughness", "Clearcoat Roughness"),
                0.12,
            )
            self._set_input(shader, ("Transmission Weight", "Transmission"), 0.08)
            self._set_input(shader, ("IOR",), 1.333)

    def _configure_building(
        self,
        material: bpy.types.Material,
        parsed: dict[str, object],
    ) -> None:
        """Create profile-specific facades and a top-facing roof-color blend."""
        if not self._enhanced or parsed.get("detail") != "facade":
            self._configure_basic(
                material,
                FeatureType.BUILDING,
                parsed["base_color"],
            )
            node_tree = material.node_tree
            if node_tree is not None:
                shader = node_tree.nodes.get("Principled BSDF")
                if shader is not None:
                    self._set_building_response(shader, str(parsed.get("profile", "")))
            return

        node_tree = material.node_tree
        if node_tree is None:
            return
        nodes = node_tree.nodes
        nodes.clear()
        output = nodes.new("ShaderNodeOutputMaterial")
        shader = nodes.new("ShaderNodeBsdfPrincipled")
        coordinate = nodes.new("ShaderNodeTexCoord")
        geometry = nodes.new("ShaderNodeNewGeometry")
        separate_normal = nodes.new("ShaderNodeSeparateXYZ")
        roof_mask = nodes.new("ShaderNodeMath")
        roof_mask.operation = "GREATER_THAN"
        roof_mask.inputs[1].default_value = 0.58
        roof_mix = nodes.new("ShaderNodeMixRGB")
        roof_mix.blend_type = "MIX"
        bump = nodes.new("ShaderNodeBump")

        coordinate.location = (-950.0, 80.0)
        geometry.location = (-950.0, -250.0)
        separate_normal.location = (-740.0, -250.0)
        roof_mask.location = (-540.0, -250.0)
        roof_mix.location = (-130.0, 130.0)
        bump.location = (-120.0, -150.0)
        shader.location = (130.0, 40.0)
        output.location = (430.0, 40.0)

        profile = str(parsed.get("profile", "generic_plaster"))
        base = parsed["base_color"]
        roof = parsed["roof_color"]
        facade_socket, height_socket = self._facade_nodes(
            nodes,
            node_tree,
            coordinate,
            profile,
            base,
        )
        roof_mix.inputs[2].default_value = roof
        node_tree.links.new(facade_socket, roof_mix.inputs[1])
        node_tree.links.new(geometry.outputs["Normal"], separate_normal.inputs[0])
        node_tree.links.new(separate_normal.outputs["Z"], roof_mask.inputs[0])
        node_tree.links.new(roof_mask.outputs[0], roof_mix.inputs[0])
        node_tree.links.new(roof_mix.outputs["Color"], shader.inputs["Base Color"])
        if height_socket is not None:
            node_tree.links.new(height_socket, bump.inputs["Height"])
            node_tree.links.new(bump.outputs["Normal"], shader.inputs["Normal"])
        node_tree.links.new(shader.outputs["BSDF"], output.inputs["Surface"])
        self._set_input(bump, ("Strength",), 0.18)
        self._set_input(bump, ("Distance",), 0.035)
        self._set_building_response(shader, profile)

    def _facade_nodes(
        self,
        nodes: bpy.types.Nodes,
        node_tree: bpy.types.NodeTree,
        coordinate: bpy.types.Node,
        profile: str,
        base: tuple[float, float, float, float],
    ) -> tuple[bpy.types.NodeSocket, bpy.types.NodeSocket | None]:
        """Return facade color and relief sockets for one profile."""
        if profile == "commercial_glass":
            brick = nodes.new("ShaderNodeTexBrick")
            brick.location = (-650.0, 100.0)
            brick.offset = 0.0
            brick.offset_frequency = 1
            self._set_input(brick, ("Color1",), self._shift_value(base, 0.70))
            self._set_input(brick, ("Color2",), self._shift_value(base, 1.08))
            self._set_input(brick, ("Mortar",), (0.025, 0.035, 0.045, 1.0))
            self._set_input(brick, ("Scale",), 7.0)
            self._set_input(brick, ("Mortar Size",), 0.08)
            self._set_input(brick, ("Row Height",), 0.22)
            node_tree.links.new(coordinate.outputs["Generated"], brick.inputs["Vector"])
            return brick.outputs["Color"], brick.outputs.get("Fac")

        if profile in {"residential_brick", "historic_brick", "institutional_stone"}:
            brick = nodes.new("ShaderNodeTexBrick")
            brick.location = (-650.0, 100.0)
            self._set_input(brick, ("Color1",), self._shift_value(base, 0.82))
            self._set_input(brick, ("Color2",), self._shift_value(base, 1.15))
            self._set_input(brick, ("Mortar",), self._shift_value(base, 0.45))
            self._set_input(brick, ("Scale",), 5.0 if profile == "institutional_stone" else 12.0)
            self._set_input(brick, ("Mortar Size",), 0.025)
            node_tree.links.new(coordinate.outputs["Generated"], brick.inputs["Vector"])
            return brick.outputs["Color"], brick.outputs.get("Fac")

        noise = nodes.new("ShaderNodeTexNoise")
        ramp = nodes.new("ShaderNodeValToRGB")
        noise.location = (-690.0, 100.0)
        ramp.location = (-430.0, 100.0)
        scale = 3.0 if profile == "industrial_concrete" else 1.25
        self._set_input(noise, ("Scale",), scale)
        self._set_input(noise, ("Detail",), 4.0)
        self._set_input(noise, ("Roughness",), 0.66)
        ramp.color_ramp.elements[0].color = self._shift_value(base, 0.74)
        ramp.color_ramp.elements[1].color = self._shift_value(base, 1.22)
        node_tree.links.new(coordinate.outputs["Generated"], noise.inputs["Vector"])
        node_tree.links.new(noise.outputs["Fac"], ramp.inputs["Fac"])
        return ramp.outputs["Color"], noise.outputs["Fac"]

    def _set_building_response(self, shader: bpy.types.Node, profile: str) -> None:
        roughness = {
            "commercial_glass": 0.28,
            "metal_tower": 0.34,
            "industrial_concrete": 0.82,
            "historic_brick": 0.76,
            "residential_brick": 0.72,
            "institutional_stone": 0.67,
            "worship_stone": 0.62,
        }.get(profile, 0.66)
        metallic = 0.22 if profile == "metal_tower" else 0.08 if profile == "commercial_glass" else 0.0
        self._set_input(shader, ("Roughness",), roughness)
        self._set_input(shader, ("Metallic",), metallic)
        if profile == "commercial_glass":
            self._set_input(shader, ("Coat Weight", "Clearcoat"), 0.22)
            self._set_input(shader, ("Coat Roughness", "Clearcoat Roughness"), 0.12)

    def _configure_accuracy(
        self,
        material: bpy.types.Material,
        color: tuple[float, float, float, float],
        emission: bool = True,
    ) -> None:
        self._configure_basic(material, FeatureType.BUILDING, color)
        node_tree = material.node_tree
        if node_tree is None:
            return
        shader = node_tree.nodes.get("Principled BSDF")
        if shader is None:
            return
        self._set_input(shader, ("Roughness",), 0.36)
        if emission:
            self._set_input(shader, ("Emission Color", "Emission"), color)
            self._set_input(shader, ("Emission Strength",), 0.18)

    def _configure_enhanced(
        self,
        material: bpy.types.Material,
        category: FeatureType,
        base_color: tuple[float, float, float, float] | None = None,
    ) -> None:
        node_tree = material.node_tree
        if node_tree is None:
            return
        nodes = node_tree.nodes
        nodes.clear()
        output = nodes.new("ShaderNodeOutputMaterial")
        shader = nodes.new("ShaderNodeBsdfPrincipled")
        texture_coordinate = nodes.new("ShaderNodeTexCoord")
        noise = nodes.new("ShaderNodeTexNoise")
        detail_noise = nodes.new("ShaderNodeTexNoise")
        ramp = nodes.new("ShaderNodeValToRGB")
        bump = nodes.new("ShaderNodeBump")

        texture_coordinate.location = (-900.0, 0.0)
        noise.location = (-680.0, 80.0)
        detail_noise.location = (-680.0, -180.0)
        ramp.location = (-420.0, 100.0)
        bump.location = (-180.0, -150.0)
        shader.location = (70.0, 20.0)
        output.location = (390.0, 20.0)

        base = base_color or self._COLORS[category]
        ramp.color_ramp.elements[0].color = self._shift_value(base, 0.62)
        ramp.color_ramp.elements[1].color = self._shift_value(base, 1.34)
        ramp.color_ramp.elements[0].position = 0.24
        ramp.color_ramp.elements[1].position = 0.78
        scale = self._noise_scale(category)
        detail = 1.8 if self._quality is QualityPreset.LOW else 4.0
        if self._quality is QualityPreset.HIGH:
            detail = 6.0
        self._set_input(noise, ("Scale",), scale)
        self._set_input(noise, ("Detail",), detail)
        self._set_input(noise, ("Roughness",), 0.64)
        self._set_input(detail_noise, ("Scale",), scale * 8.0)
        self._set_input(detail_noise, ("Detail",), 2.0)
        self._set_input(detail_noise, ("Roughness",), 0.70)
        node_tree.links.new(texture_coordinate.outputs["Generated"], noise.inputs["Vector"])
        node_tree.links.new(texture_coordinate.outputs["Generated"], detail_noise.inputs["Vector"])
        node_tree.links.new(noise.outputs["Fac"], ramp.inputs["Fac"])
        node_tree.links.new(ramp.outputs["Color"], shader.inputs["Base Color"])
        node_tree.links.new(detail_noise.outputs["Fac"], bump.inputs["Height"])
        node_tree.links.new(bump.outputs["Normal"], shader.inputs["Normal"])
        node_tree.links.new(shader.outputs["BSDF"], output.inputs["Surface"])
        strength = {
            FeatureType.WATER: 0.10,
            FeatureType.ROAD: 0.24,
            FeatureType.BRIDGE: 0.18,
            FeatureType.BUILDING: 0.16,
            FeatureType.GREEN: 0.30,
            FeatureType.TERRAIN: 0.34,
            FeatureType.TREE: 0.28,
        }[category]
        distance = {
            FeatureType.WATER: 0.10,
            FeatureType.ROAD: 0.08,
            FeatureType.BRIDGE: 0.06,
            FeatureType.BUILDING: 0.045,
            FeatureType.GREEN: 0.12,
            FeatureType.TERRAIN: 0.16,
            FeatureType.TREE: 0.10,
        }[category]
        self._set_input(bump, ("Strength",), strength)
        self._set_input(bump, ("Distance",), distance)
        self._set_input(shader, ("Roughness",), self._roughness(category))
        self._set_input(shader, ("Metallic",), 0.04 if category is FeatureType.WATER else 0.0)
        if category is FeatureType.WATER:
            self._set_input(shader, ("Coat Weight", "Clearcoat"), 0.34)
            self._set_input(shader, ("Coat Roughness", "Clearcoat Roughness"), 0.10)
            self._set_input(shader, ("Transmission Weight", "Transmission"), 0.12)
            self._set_input(shader, ("IOR",), 1.333)

    def _parse_variant(self, category: FeatureType, variant: str) -> dict[str, object]:
        if variant.startswith("minecraft|"):
            color = self._MINECRAFT_COLORS[category]
            return {
                "kind": "category",
                "base_color": color,
                "roof_color": color,
                "profile": "minecraft_block",
                "detail": "plain",
            }
        if variant.startswith("classic_voxel|"):
            color = self._CLASSIC_VOXEL_COLORS[category]
            return {
                "kind": "category",
                "base_color": color,
                "roof_color": color,
                "profile": "classic_voxel",
                "detail": "plain",
            }
        if variant.startswith("architectural|"):
            color = self._ARCHITECTURAL_COLORS[category]
            return {
                "kind": "category",
                "base_color": color,
                "roof_color": color,
                "profile": "architectural_model",
                "detail": "plain",
            }
        if variant.startswith("accuracy|"):
            confidence = variant.split("|", 1)[1]
            return {
                "kind": "accuracy",
                "base_color": self._ACCURACY_COLORS.get(confidence, self._ACCURACY_COLORS["low"]),
                "profile": "accuracy",
                "roof_color": self._ACCURACY_COLORS.get(confidence, self._ACCURACY_COLORS["low"]),
                "detail": "plain",
            }
        if category is FeatureType.BUILDING and variant.startswith("building|"):
            parts = variant.split("|")
            profile = parts[1] if len(parts) > 1 else "generic_plaster"
            facade = self._hex_color(parts[2] if len(parts) > 2 else "") or self._COLORS[category]
            roof = self._hex_color(parts[3] if len(parts) > 3 else "") or self._shift_value(facade, 0.68)
            detail = parts[4] if len(parts) > 4 else "plain"
            return {
                "kind": "building",
                "base_color": facade,
                "roof_color": roof,
                "profile": profile,
                "detail": detail,
            }
        return {
            "kind": "category",
            "base_color": self._COLORS[category],
            "roof_color": self._COLORS[category],
            "profile": "",
            "detail": "plain",
        }

    @staticmethod
    def _hex_color(value: str) -> tuple[float, float, float, float] | None:
        if not _HEX_PATTERN.match(value):
            return None
        return (
            int(value[0:2], 16) / 255.0,
            int(value[2:4], 16) / 255.0,
            int(value[4:6], 16) / 255.0,
            1.0,
        )

    @staticmethod
    def _safe_variant(value: str) -> str:
        if not value:
            return ""
        return re.sub(r"[^A-Za-z0-9_]+", "_", value).strip("_")[:72]

    @staticmethod
    def _set_input(node: object, names: tuple[str, ...], value: object) -> None:
        inputs = getattr(node, "inputs", None)
        if inputs is None:
            return
        for name in names:
            socket = inputs.get(name)
            if socket is not None:
                socket.default_value = value
                return

    @staticmethod
    def _shift_value(
        color: tuple[float, float, float, float],
        multiplier: float,
    ) -> tuple[float, float, float, float]:
        hue, saturation, value = rgb_to_hsv(color[0], color[1], color[2])
        red, green, blue = hsv_to_rgb(
            hue,
            min(1.0, saturation * 0.96),
            min(1.0, max(0.0, value * multiplier)),
        )
        return red, green, blue, color[3]

    @staticmethod
    def _roughness(category: FeatureType) -> float:
        return {
            FeatureType.WATER: 0.24,
            FeatureType.ROAD: 0.92,
            FeatureType.BRIDGE: 0.78,
            FeatureType.BUILDING: 0.68,
            FeatureType.GREEN: 0.88,
            FeatureType.TERRAIN: 0.90,
            FeatureType.TREE: 0.86,
        }[category]

    @staticmethod
    def _noise_scale(category: FeatureType) -> float:
        return {
            FeatureType.WATER: 0.18,
            FeatureType.ROAD: 1.8,
            FeatureType.BRIDGE: 1.4,
            FeatureType.BUILDING: 0.55,
            FeatureType.GREEN: 0.40,
            FeatureType.TERRAIN: 0.28,
            FeatureType.TREE: 0.85,
        }[category]
