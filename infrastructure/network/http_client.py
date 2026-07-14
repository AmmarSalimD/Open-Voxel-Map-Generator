"""Small standard-library HTTP client suitable for Blender extensions."""

from __future__ import annotations

import gzip
import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Mapping

from ...core.exceptions import RemoteServiceError
from ...core.logging_utils import get_logger

_LOGGER = get_logger(__name__)


class JsonHttpClient:
    """Perform resilient JSON HTTP requests without third-party dependencies."""

    def __init__(
        self,
        user_agent: str,
        timeout_seconds: int = 180,
        retry_count: int = 2,
    ) -> None:
        self._user_agent = user_agent
        self._timeout_seconds = timeout_seconds
        self._retry_count = retry_count

    def get_json(
        self,
        url: str,
        query: Mapping[str, str] | None = None,
        extra_headers: Mapping[str, str] | None = None,
    ) -> Any:
        """Send an HTTPS GET request and decode the JSON response."""
        encoded_query = urllib.parse.urlencode(query or {})
        request_url = f"{url}?{encoded_query}" if encoded_query else url
        request = urllib.request.Request(
            request_url,
            headers=self._headers(extra_headers),
            method="GET",
        )
        return self._execute_json(request)

    def post_form_json(
        self,
        url: str,
        form: Mapping[str, str],
        extra_headers: Mapping[str, str] | None = None,
    ) -> Any:
        """Send an HTTPS form POST request and decode the JSON response."""
        data = urllib.parse.urlencode(form).encode("utf-8")
        headers = self._headers(extra_headers)
        headers["Content-Type"] = "application/x-www-form-urlencoded; charset=utf-8"
        request = urllib.request.Request(url, data=data, headers=headers, method="POST")
        return self._execute_json(request)

    def _headers(self, extra_headers: Mapping[str, str] | None) -> dict[str, str]:
        headers = {
            "User-Agent": self._user_agent,
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
        }
        if extra_headers:
            headers.update(extra_headers)
        return headers

    def _execute_json(self, request: urllib.request.Request) -> Any:
        last_error: Exception | None = None
        attempts = self._retry_count + 1
        for attempt in range(attempts):
            try:
                with urllib.request.urlopen(
                    request,
                    timeout=self._timeout_seconds,
                ) as response:
                    raw = response.read()
                    if response.headers.get("Content-Encoding") == "gzip":
                        raw = gzip.decompress(raw)
                    return json.loads(raw.decode("utf-8"))
            except (
                urllib.error.HTTPError,
                urllib.error.URLError,
                socket.timeout,
                TimeoutError,
                json.JSONDecodeError,
            ) as exc:
                last_error = exc
                _LOGGER.warning(
                    "HTTP attempt %s/%s failed for %s: %s",
                    attempt + 1,
                    attempts,
                    request.full_url,
                    exc,
                )
                if attempt + 1 < attempts:
                    time.sleep(1.5 * (attempt + 1))

        raise RemoteServiceError(
            f"Remote GIS request failed after {attempts} attempts: {last_error}"
        ) from last_error
