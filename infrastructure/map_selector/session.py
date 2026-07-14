"""Secure localhost session for the browser-based map area selector."""

from __future__ import annotations

from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import base64
import binascii
import json
from pathlib import Path
import secrets
import threading
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse

from ...core.exceptions import OVMGError, RemoteServiceError, ValidationError
from ...domain.models import BoundingBox
from ..geocoding.nominatim import NominatimGeocoder
from ..network.http_client import JsonHttpClient
from .metrics import AreaMetricsCalculator, quality_generation_multiplier


@dataclass(frozen=True, slots=True)
class MapSelectorConfig:
    """Immutable configuration sent to one localhost browser session."""

    scene_name: str
    area_name: str
    bounds: BoundingBox
    voxel_size: float
    max_voxel_cells: int
    quality_name: str
    nominatim_endpoint: str
    user_agent: str
    network_timeout: int
    split_large_area: bool = False
    tile_url: str = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
    tile_attribution: str = "© OpenStreetMap contributors"


@dataclass(frozen=True, slots=True)
class MapSelectorResult:
    """Bounds chosen in the browser and waiting for Blender to consume them."""

    scene_name: str
    bounds: BoundingBox | None
    display_name: str
    cancelled: bool = False
    thumbnail_bytes: bytes | None = None
    thumbnail_mime_type: str = ""


@dataclass(slots=True)
class _SessionState:
    """Mutable resources owned by one running localhost selector."""

    config: MapSelectorConfig
    token: str
    server: ThreadingHTTPServer
    thread: threading.Thread
    result: MapSelectorResult | None = None
    url: str = ""


class MapSelectorSessionManager:
    """Own at most one localhost selector and its thread-safe result."""

    _lock = threading.RLock()
    _state: _SessionState | None = None

    @classmethod
    def start(cls, config: MapSelectorConfig) -> str:
        """Start a selector session and return its browser URL."""
        config.bounds.validate()
        cls.stop()
        token = secrets.token_urlsafe(24)
        html_path = Path(__file__).with_name("web") / "index.html"
        if not html_path.is_file():
            raise OVMGError("The visual map selector web interface is missing.")
        html = html_path.read_bytes()

        manager = cls
        handler_class = cls._make_handler(config, token, html, manager)
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_class)
        server.daemon_threads = True
        thread = threading.Thread(
            target=server.serve_forever,
            name="OVMG-MapSelector",
            daemon=True,
        )
        host, port = server.server_address[:2]
        url = f"http://{host}:{port}/?token={token}"
        state = _SessionState(
            config=config,
            token=token,
            server=server,
            thread=thread,
            url=url,
        )
        with cls._lock:
            cls._state = state
        thread.start()
        return url

    @classmethod
    def stop(cls) -> None:
        """Stop the active selector without touching Blender data."""
        with cls._lock:
            state = cls._state
            cls._state = None
        if state is None:
            return
        state.server.shutdown()
        state.server.server_close()
        if state.thread.is_alive() and state.thread is not threading.current_thread():
            state.thread.join(timeout=2.0)

    @classmethod
    def consume_result(cls) -> MapSelectorResult | None:
        """Return and clear a completed browser result."""
        with cls._lock:
            state = cls._state
            if state is None or state.result is None:
                return None
            result = state.result
            state.result = None
            return result

    @classmethod
    def has_active_session(cls) -> bool:
        """Return whether a localhost selector is currently running."""
        with cls._lock:
            return cls._state is not None


    @classmethod
    def active_url(cls) -> str | None:
        """Return the current selector URL so Blender can reopen the browser."""
        with cls._lock:
            return cls._state.url if cls._state is not None else None

    @classmethod
    def _store_result(cls, result: MapSelectorResult) -> None:
        with cls._lock:
            if cls._state is not None:
                cls._state.result = result

    @classmethod
    def _make_handler(
        cls,
        config: MapSelectorConfig,
        token: str,
        html: bytes,
        manager: type["MapSelectorSessionManager"],
    ) -> type[BaseHTTPRequestHandler]:
        """Create a request handler closed over one session configuration."""

        class SelectorHandler(BaseHTTPRequestHandler):
            server_version = "OVMGMapSelector/1.0"
            protocol_version = "HTTP/1.1"

            def log_message(self, _format: str, *_args: object) -> None:
                return

            def do_GET(self) -> None:  # noqa: N802 - standard library API
                parsed = urlparse(self.path)
                if parsed.path == "/":
                    self._send_bytes(html, "text/html; charset=utf-8")
                    return
                if not self._authorized(parsed.query):
                    self._send_json(
                        {"error": "Invalid or expired selector token."},
                        HTTPStatus.FORBIDDEN,
                    )
                    return
                if parsed.path == "/config":
                    self._send_json(self._config_payload(config))
                    return
                if parsed.path == "/geocode":
                    query = parse_qs(parsed.query).get("q", [""])[0].strip()
                    if len(query) < 3:
                        self._send_json(
                            {"error": "Enter at least three characters."},
                            HTTPStatus.BAD_REQUEST,
                        )
                        return
                    try:
                        self._send_json(self._search_places(config, query))
                    except (OVMGError, ValueError) as exc:
                        self._send_json(
                            {"error": str(exc)},
                            HTTPStatus.BAD_GATEWAY,
                        )
                    return
                if parsed.path == "/reverse":
                    values = parse_qs(parsed.query)
                    try:
                        latitude = float(values.get("lat", [""])[0])
                        longitude = float(values.get("lon", [""])[0])
                        self._send_json(
                            self._reverse_place(config, latitude, longitude)
                        )
                    except (OVMGError, TypeError, ValueError) as exc:
                        self._send_json(
                            {"error": str(exc)},
                            HTTPStatus.BAD_GATEWAY,
                        )
                    return
                self._send_json({"error": "Not found."}, HTTPStatus.NOT_FOUND)

            def do_POST(self) -> None:  # noqa: N802 - standard library API
                parsed = urlparse(self.path)
                if not self._authorized(parsed.query):
                    self._send_json(
                        {"error": "Invalid or expired selector token."},
                        HTTPStatus.FORBIDDEN,
                    )
                    return
                if parsed.path == "/cancel":
                    manager._store_result(
                        MapSelectorResult(
                            scene_name=config.scene_name,
                            bounds=None,
                            display_name=config.area_name,
                            cancelled=True,
                        )
                    )
                    self._send_json({"ok": True})
                    return
                if parsed.path != "/selection":
                    self._send_json({"error": "Not found."}, HTTPStatus.NOT_FOUND)
                    return
                try:
                    payload = self._read_json_body()
                    bounds = BoundingBox(
                        south=float(payload["south"]),
                        west=float(payload["west"]),
                        north=float(payload["north"]),
                        east=float(payload["east"]),
                    )
                    bounds.validate()
                    display_name = str(payload.get("display_name", "")).strip()
                    thumbnail_bytes, thumbnail_mime_type = self._decode_thumbnail(
                        payload.get("thumbnail")
                    )
                    manager._store_result(
                        MapSelectorResult(
                            scene_name=config.scene_name,
                            bounds=bounds,
                            display_name=display_name or config.area_name,
                            thumbnail_bytes=thumbnail_bytes,
                            thumbnail_mime_type=thumbnail_mime_type,
                        )
                    )
                    self._send_json({"ok": True})
                except (KeyError, TypeError, ValueError, ValidationError) as exc:
                    self._send_json(
                        {"error": f"Invalid selected bounds: {exc}"},
                        HTTPStatus.BAD_REQUEST,
                    )

            def _authorized(self, raw_query: str) -> bool:
                supplied = parse_qs(raw_query).get("token", [""])[0]
                return secrets.compare_digest(supplied, token)

            def _read_json_body(self) -> Mapping[str, Any]:
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError as exc:
                    raise ValueError("Invalid content length.") from exc
                if not 1 <= length <= 1_048_576:
                    raise ValueError("Selection payload is empty or too large.")
                value = json.loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(value, Mapping):
                    raise ValueError("Selection payload must be a JSON object.")
                return value

            @staticmethod
            def _decode_thumbnail(value: object) -> tuple[bytes | None, str]:
                """Decode one browser-generated PNG/JPEG thumbnail safely."""
                if value in (None, ""):
                    return None, ""
                if not isinstance(value, str):
                    raise ValueError("Thumbnail must be a data URL string.")
                header, separator, encoded = value.partition(",")
                if not separator or ";base64" not in header:
                    raise ValueError("Thumbnail is not a base64 data URL.")
                mime_type = header[5:].split(";", 1)[0].lower()
                if mime_type not in {"image/jpeg", "image/png"}:
                    raise ValueError("Only JPEG or PNG thumbnails are accepted.")
                try:
                    decoded = base64.b64decode(encoded, validate=True)
                except (binascii.Error, ValueError) as exc:
                    raise ValueError("Thumbnail data is invalid.") from exc
                if not 32 <= len(decoded) <= 700_000:
                    raise ValueError("Thumbnail data is empty or too large.")
                if mime_type == "image/png" and not decoded.startswith(
                    b"\x89PNG\r\n\x1a\n"
                ):
                    raise ValueError("PNG thumbnail signature is invalid.")
                if mime_type == "image/jpeg" and not decoded.startswith(b"\xff\xd8"):
                    raise ValueError("JPEG thumbnail signature is invalid.")
                return decoded, mime_type

            def _send_json(
                self,
                payload: object,
                status: HTTPStatus = HTTPStatus.OK,
            ) -> None:
                data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self._send_bytes(data, "application/json; charset=utf-8", status)

            def _send_bytes(
                self,
                data: bytes,
                content_type: str,
                status: HTTPStatus = HTTPStatus.OK,
            ) -> None:
                self.send_response(int(status))
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header(
                    "Content-Security-Policy",
                    "default-src 'self'; script-src 'self' 'unsafe-inline' "
                    "https://unpkg.com; style-src 'self' 'unsafe-inline' "
                    "https://unpkg.com; img-src 'self' data: "
                    "https://tile.openstreetmap.org https://unpkg.com; "
                    "connect-src 'self'; font-src 'self' data:;",
                )
                self.end_headers()
                self.wfile.write(data)

            @staticmethod
            def _config_payload(value: MapSelectorConfig) -> dict[str, object]:
                metrics = AreaMetricsCalculator.calculate(
                    value.bounds,
                    value.voxel_size,
                    value.max_voxel_cells,
                    quality_generation_multiplier(value.quality_name),
                )
                return {
                    "area_name": value.area_name,
                    "quality": value.quality_name,
                    "voxel_size": value.voxel_size,
                    "max_voxel_cells": value.max_voxel_cells,
                    "split_large_area": value.split_large_area,
                    "tile_url": value.tile_url,
                    "tile_attribution": value.tile_attribution,
                    "bounds": {
                        "south": value.bounds.south,
                        "west": value.bounds.west,
                        "north": value.bounds.north,
                        "east": value.bounds.east,
                    },
                    "initial_metrics": {
                        "width_km": metrics.width_km,
                        "height_km": metrics.height_km,
                        "area_square_km": metrics.area_square_km,
                        "surface_cells": metrics.estimated_surface_cells,
                        "load_level": metrics.load_level.value,
                    },
                }

            @staticmethod
            def _search_places(
                value: MapSelectorConfig,
                query: str,
            ) -> dict[str, object]:
                client = JsonHttpClient(
                    user_agent=value.user_agent,
                    timeout_seconds=value.network_timeout,
                    retry_count=1,
                )
                geocoder = NominatimGeocoder(value.nominatim_endpoint, client)
                payload = client.get_json(
                    value.nominatim_endpoint,
                    query={
                        "q": query,
                        "format": "jsonv2",
                        "limit": "8",
                        "addressdetails": "1",
                        "namedetails": "1",
                        "extratags": "1",
                        "accept-language": "en-US,en",
                    },
                )
                if not isinstance(payload, list):
                    raise RemoteServiceError(
                        "Nominatim returned an unexpected response format."
                    )
                candidates = [item for item in payload if isinstance(item, Mapping)]
                candidates.sort(
                    key=lambda item: geocoder._candidate_score(query, item),
                    reverse=True,
                )
                results: list[dict[str, object]] = []
                for candidate in candidates[:8]:
                    try:
                        bounds = geocoder._ensure_useful_extent(
                            geocoder._bounds_from_candidate(candidate),
                            candidate,
                        )
                        bounds.validate()
                    except OVMGError:
                        continue
                    results.append(
                        {
                            "display_name": str(candidate.get("display_name", query)),
                            "category": str(
                                candidate.get("category", candidate.get("class", ""))
                            ),
                            "type": str(
                                candidate.get(
                                    "addresstype",
                                    candidate.get("type", ""),
                                )
                            ),
                            "bounds": {
                                "south": bounds.south,
                                "west": bounds.west,
                                "north": bounds.north,
                                "east": bounds.east,
                            },
                        }
                    )
                if not results:
                    raise RemoteServiceError(
                        f'No useful geographic result was found for "{query}".'
                    )
                return {"results": results}

            @staticmethod
            def _reverse_place(
                value: MapSelectorConfig,
                latitude: float,
                longitude: float,
            ) -> dict[str, str]:
                """Resolve the current rectangle center to a concise current place name."""
                if not -90.0 <= latitude <= 90.0:
                    raise ValueError("Latitude is outside the valid range.")
                if not -180.0 <= longitude <= 180.0:
                    raise ValueError("Longitude is outside the valid range.")

                client = JsonHttpClient(
                    user_agent=value.user_agent,
                    timeout_seconds=value.network_timeout,
                    retry_count=1,
                )
                endpoint = value.nominatim_endpoint.rstrip("/")
                if endpoint.endswith("/search"):
                    endpoint = endpoint[: -len("/search")] + "/reverse"
                else:
                    endpoint = endpoint.rsplit("/", 1)[0] + "/reverse"
                payload = client.get_json(
                    endpoint,
                    query={
                        "lat": f"{latitude:.7f}",
                        "lon": f"{longitude:.7f}",
                        "format": "jsonv2",
                        "zoom": "14",
                        "addressdetails": "1",
                        "namedetails": "1",
                        "accept-language": "en-US,en",
                    },
                )
                if not isinstance(payload, Mapping):
                    raise RemoteServiceError(
                        "Nominatim returned an unexpected reverse-geocoding response."
                    )

                address = payload.get("address")
                parts: list[str] = []
                if isinstance(address, Mapping):
                    for key in (
                        "neighbourhood",
                        "suburb",
                        "quarter",
                        "city_district",
                        "city",
                        "town",
                        "village",
                        "municipality",
                        "state",
                        "country",
                    ):
                        candidate = str(address.get(key, "")).strip()
                        if candidate and candidate.casefold() not in {
                            item.casefold() for item in parts
                        }:
                            parts.append(candidate)
                        if len(parts) >= 3:
                            break
                display_name = ", ".join(parts)
                if not display_name:
                    display_name = str(payload.get("display_name", "")).strip()
                if not display_name:
                    display_name = "Selected Map Area"
                return {"display_name": display_name[:160]}

        return SelectorHandler
