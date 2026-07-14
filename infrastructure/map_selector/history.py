"""Persistent history for visual map selections.

The history is stored as compact JSON in Blender add-on preferences so users can
reuse previous rectangles without manually entering geographic coordinates.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
from typing import Iterable

from ...domain.models import BoundingBox


@dataclass(frozen=True, slots=True)
class AreaHistoryEntry:
    """One previously confirmed browser-map selection."""

    key: str
    name: str
    south: float
    west: float
    north: float
    east: float
    saved_utc: str

    @property
    def bounds(self) -> BoundingBox:
        """Return the stored coordinates as a validated domain object."""
        bounds = BoundingBox(self.south, self.west, self.north, self.east)
        bounds.validate()
        return bounds


class AreaHistoryStore:
    """Read and update prior visual selections in add-on preferences."""

    @staticmethod
    def load(preferences: object) -> list[AreaHistoryEntry]:
        """Return valid history entries, newest first."""
        raw = str(getattr(preferences, "saved_area_history_json", "") or "")
        if not raw.strip():
            return []
        try:
            payload = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return []
        if not isinstance(payload, list):
            return []
        result: list[AreaHistoryEntry] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            try:
                entry = AreaHistoryEntry(
                    key=str(item["key"]),
                    name=str(item.get("name") or "Saved Area"),
                    south=float(item["south"]),
                    west=float(item["west"]),
                    north=float(item["north"]),
                    east=float(item["east"]),
                    saved_utc=str(item.get("saved_utc") or ""),
                )
                entry.bounds.validate()
            except (KeyError, TypeError, ValueError):
                continue
            result.append(entry)
        return result

    @classmethod
    def add(
        cls,
        preferences: object,
        name: str,
        bounds: BoundingBox,
    ) -> AreaHistoryEntry:
        """Add or refresh a confirmed selection and persist the bounded list."""
        bounds.validate()
        key = cls._key(bounds)
        entry = AreaHistoryEntry(
            key=key,
            name=name.strip() or "Saved Area",
            south=bounds.south,
            west=bounds.west,
            north=bounds.north,
            east=bounds.east,
            saved_utc=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        entries = [item for item in cls.load(preferences) if item.key != key]
        entries.insert(0, entry)
        limit = max(1, min(50, int(getattr(preferences, "saved_area_limit", 12))))
        cls._write(preferences, entries[:limit])
        return entry

    @classmethod
    def find(cls, preferences: object, key: str) -> AreaHistoryEntry | None:
        """Return an entry by its stable identifier."""
        return next((item for item in cls.load(preferences) if item.key == key), None)

    @classmethod
    def remove(cls, preferences: object, key: str) -> bool:
        """Remove one entry and return whether it existed."""
        entries = cls.load(preferences)
        filtered = [item for item in entries if item.key != key]
        if len(filtered) == len(entries):
            return False
        cls._write(preferences, filtered)
        return True

    @staticmethod
    def _write(preferences: object, entries: Iterable[AreaHistoryEntry]) -> None:
        preferences.saved_area_history_json = json.dumps(
            [asdict(entry) for entry in entries],
            ensure_ascii=False,
            separators=(",", ":"),
        )

    @staticmethod
    def _key(bounds: BoundingBox) -> str:
        raw = (
            f"{bounds.south:.7f}|{bounds.west:.7f}|"
            f"{bounds.north:.7f}|{bounds.east:.7f}"
        )
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
