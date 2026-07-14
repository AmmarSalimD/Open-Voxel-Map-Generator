"""Persistent, non-destructive building corrections stored inside the blend file.

Generated map buildings are optimized into chunk meshes, so a single source
building cannot be safely edited in-place.  This module keeps an explicit
correction registry keyed by source feature id.  Replacements and additions
remain separate Blender objects while suppressed source ids are omitted during
the next regeneration.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from typing import Any, Iterable

import bpy

from ...core.naming import building_corrections_name, building_metadata_name

_SCHEMA = "ovmg-building-corrections-1.0"


@dataclass(frozen=True, slots=True)
class CorrectionSummary:
    """Compact correction counts used by the Blender panel."""

    suppressed_sources: int = 0
    replacements: int = 0
    added_buildings: int = 0


class BuildingCorrectionStore:
    """Read and update persistent building corrections in a Blender Text block."""

    @classmethod
    def empty_payload(cls, project_name: str) -> dict[str, Any]:
        """Return a valid empty registry payload."""
        return {
            "schema": _SCHEMA,
            "project": project_name,
            "updated_utc": "",
            "suppressed_source_ids": [],
            "replacements": {},
            "added_objects": [],
        }

    @classmethod
    def load(cls, project_name: str) -> dict[str, Any]:
        """Load the registry, repairing malformed optional fields defensively."""
        text = bpy.data.texts.get(building_corrections_name(project_name))
        if text is None:
            return cls.empty_payload(project_name)
        try:
            payload = json.loads(text.as_string() or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            return cls.empty_payload(project_name)
        if not isinstance(payload, dict):
            return cls.empty_payload(project_name)
        result = cls.empty_payload(project_name)
        result.update(payload)
        result["project"] = project_name
        suppressed = result.get("suppressed_source_ids", [])
        result["suppressed_source_ids"] = sorted(
            {str(value) for value in suppressed if str(value).strip()}
        )
        replacements = result.get("replacements", {})
        result["replacements"] = (
            {str(key): str(value) for key, value in replacements.items()}
            if isinstance(replacements, dict)
            else {}
        )
        added = result.get("added_objects", [])
        result["added_objects"] = sorted(
            {str(value) for value in added if str(value).strip()}
        )
        return result

    @classmethod
    def save(cls, project_name: str, payload: dict[str, Any]) -> None:
        """Write one normalized correction registry into the blend file."""
        normalized = cls.empty_payload(project_name)
        normalized.update(deepcopy(payload))
        normalized["schema"] = _SCHEMA
        normalized["project"] = project_name
        normalized["updated_utc"] = datetime.now(timezone.utc).isoformat()
        normalized["suppressed_source_ids"] = sorted(
            {
                str(value)
                for value in normalized.get("suppressed_source_ids", [])
                if str(value).strip()
            }
        )
        replacements = normalized.get("replacements", {})
        normalized["replacements"] = (
            {str(key): str(value) for key, value in replacements.items()}
            if isinstance(replacements, dict)
            else {}
        )
        normalized["added_objects"] = sorted(
            {
                str(value)
                for value in normalized.get("added_objects", [])
                if str(value).strip()
            }
        )
        name = building_corrections_name(project_name)
        text = bpy.data.texts.get(name)
        if text is None:
            text = bpy.data.texts.new(name)
        else:
            text.clear()
        text.write(json.dumps(normalized, ensure_ascii=False, indent=2, sort_keys=True))
        text["ovmg_project"] = project_name
        text["ovmg_building_corrections"] = True

    @classmethod
    def clear(cls, project_name: str) -> None:
        """Remove the persistent correction registry for one project."""
        text = bpy.data.texts.get(building_corrections_name(project_name))
        if text is not None:
            bpy.data.texts.remove(text)

    @classmethod
    def suppressed_ids(cls, project_name: str) -> tuple[str, ...]:
        """Return source feature ids omitted during regeneration."""
        return tuple(cls.load(project_name)["suppressed_source_ids"])

    @classmethod
    def suppress(
        cls,
        project_name: str,
        source_id: str,
        replacement_object: str = "",
    ) -> None:
        """Suppress one generated source building and optionally link its replacement."""
        source_id = str(source_id).strip()
        if not source_id:
            return
        payload = cls.load(project_name)
        suppressed = set(payload["suppressed_source_ids"])
        suppressed.add(source_id)
        payload["suppressed_source_ids"] = sorted(suppressed)
        if replacement_object:
            payload["replacements"][source_id] = replacement_object
        cls.save(project_name, payload)

    @classmethod
    def restore(cls, project_name: str, source_id: str) -> str:
        """Restore one generated source and return the linked replacement name."""
        source_id = str(source_id).strip()
        payload = cls.load(project_name)
        payload["suppressed_source_ids"] = [
            item for item in payload["suppressed_source_ids"] if item != source_id
        ]
        replacement_name = str(payload["replacements"].pop(source_id, ""))
        cls.save(project_name, payload)
        return replacement_name

    @classmethod
    def register_added_object(cls, project_name: str, object_name: str) -> None:
        """Register one user-created building that has no generated source id."""
        if not object_name:
            return
        payload = cls.load(project_name)
        added = set(payload["added_objects"])
        added.add(object_name)
        payload["added_objects"] = sorted(added)
        cls.save(project_name, payload)

    @classmethod
    def unregister_object(cls, project_name: str, obj: bpy.types.Object) -> None:
        """Remove one deleted object from the registry and restore its source if needed."""
        payload = cls.load(project_name)
        name = obj.name
        payload["added_objects"] = [
            item for item in payload["added_objects"] if item != name
        ]
        source_id = str(obj.get("ovmg_replaces_source_id", ""))
        suppressed_ids: set[str] = set()
        raw_ids = str(obj.get("ovmg_suppressed_source_ids_json", ""))
        if raw_ids:
            try:
                suppressed_ids.update(
                    str(value) for value in json.loads(raw_ids) if str(value)
                )
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
        if source_id:
            suppressed_ids.add(source_id)
        if suppressed_ids:
            payload["suppressed_source_ids"] = [
                item
                for item in payload["suppressed_source_ids"]
                if item not in suppressed_ids
            ]
            for item in suppressed_ids:
                payload["replacements"].pop(item, None)
        else:
            stale_sources = [
                key
                for key, value in payload["replacements"].items()
                if value == name
            ]
            for key in stale_sources:
                payload["replacements"].pop(key, None)
                payload["suppressed_source_ids"] = [
                    item for item in payload["suppressed_source_ids"] if item != key
                ]
        cls.save(project_name, payload)

    @classmethod
    def summary(cls, project_name: str) -> CorrectionSummary:
        """Return correction counts from live root objects and registry state."""
        payload = cls.load(project_name)
        roots = [
            obj
            for obj in iter_project_user_buildings(project_name)
            if obj.get("ovmg_user_building_root")
        ]
        replacements = sum(
            1 for obj in roots if obj.get("ovmg_correction_kind") == "REPLACEMENT"
        )
        additions = sum(
            1 for obj in roots if obj.get("ovmg_correction_kind") != "REPLACEMENT"
        )
        return CorrectionSummary(
            suppressed_sources=len(payload["suppressed_source_ids"]),
            replacements=replacements,
            added_buildings=additions,
        )

    @classmethod
    def replacement_object(
        cls,
        project_name: str,
        source_id: str,
    ) -> bpy.types.Object | None:
        """Return an existing replacement object for one source id."""
        name = cls.load(project_name)["replacements"].get(source_id, "")
        found = bpy.data.objects.get(name) if name else None
        if found is not None:
            return found
        for obj in iter_project_user_buildings(project_name):
            if (
                obj.get("ovmg_user_building_root")
                and obj.get("ovmg_replaces_source_id") == source_id
            ):
                return obj
        return None


class BuildingMetadataReader:
    """Read the generated per-building metadata used by the correction studio."""

    @staticmethod
    def load(project_name: str) -> dict[str, Any]:
        """Return the building metadata payload or an empty compatible structure."""
        text = bpy.data.texts.get(building_metadata_name(project_name))
        if text is None:
            return {"buildings": []}
        try:
            payload = json.loads(text.as_string() or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            return {"buildings": []}
        return payload if isinstance(payload, dict) else {"buildings": []}

    @classmethod
    def find(cls, project_name: str, source_id: str) -> dict[str, Any] | None:
        """Return one source building record by exact id."""
        for record in cls.load(project_name).get("buildings", []):
            if str(record.get("source_id", "")) == source_id:
                return record
        return None

    @classmethod
    def all(cls, project_name: str) -> tuple[dict[str, Any], ...]:
        """Return all valid building metadata records."""
        records = cls.load(project_name).get("buildings", [])
        return tuple(item for item in records if isinstance(item, dict))


def mark_user_building(
    obj: bpy.types.Object,
    project_name: str,
    source_id: str = "",
    correction_kind: str = "ADDED",
) -> None:
    """Apply stable OVMG custom properties to an editable building object."""
    obj["ovmg_generated"] = True
    obj["ovmg_user_building"] = True
    obj["ovmg_project"] = project_name
    obj["ovmg_category"] = "building"
    obj["ovmg_correction_kind"] = correction_kind
    if source_id:
        obj["ovmg_replaces_source_id"] = source_id


def iter_project_user_buildings(project_name: str) -> Iterable[bpy.types.Object]:
    """Yield all editable user building objects for one project."""
    for obj in bpy.data.objects:
        if obj.get("ovmg_user_building") and obj.get("ovmg_project") == project_name:
            yield obj
