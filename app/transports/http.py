"""Tiny blocking HTTP/JSON GET client (standard library) with driver error mapping.
Call from asyncio.to_thread. Read-only: GET only."""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

from ..errors import AuthFailed, DriverError, PrinterUnreachable


class HttpJson:
    def __init__(self, host: str, port: int = 80, api_key: Optional[str] = None, timeout: float = 6):
        self.base = f"http://{host}" + ("" if port == 80 else f":{port}")
        self.api_key, self.timeout = api_key, timeout

    def _open(self, path: str):
        req = urllib.request.Request(self.base + path, method="GET")
        if self.api_key:
            req.add_header("X-Api-Key", self.api_key)
        try:
            return urllib.request.urlopen(req, timeout=self.timeout)
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise AuthFailed(f"HTTP {e.code}") from e
            raise DriverError(f"HTTP {e.code} for {path}") from e
        except (urllib.error.URLError, OSError) as e:
            reason = getattr(e, "reason", e)
            raise PrinterUnreachable(f"{self.base} not reachable ({reason})") from e

    def get(self, path: str) -> Any:
        with self._open(path) as r:
            body = r.read()
        try:
            return json.loads(body.decode("utf-8", "replace"))
        except ValueError as e:
            raise DriverError(f"not JSON from {path}") from e

    def get_bytes(self, path: str, max_bytes: int = 5 * 1024 * 1024) -> bytes:
        with self._open(path) as r:
            return r.read(max_bytes)


def quote(s: str) -> str:
    return urllib.parse.quote(s, safe="/")
