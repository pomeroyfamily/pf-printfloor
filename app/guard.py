"""Request guard (security audit 2026-11-17). The dashboard has no login (keep it on the home network or behind an
authenticating proxy), but it must not be drivable by OTHER web pages a user visits:

* Host check: only names that are clearly this machine on the local network (IP addresses, localhost, single-label
  names, *.local / *.lan / *.home.arpa / ...) plus PRINTER_DASH_ALLOWED_HOSTS. Stops DNS-rebinding, where a web
  page re-points its own domain at the dashboard's address to read it as "same origin".
* Cross-site writes: POST / PUT / PATCH / DELETE from another site are refused (Sec-Fetch-Site, or Origin vs Host
  for browsers that don't send it). Stops a page you visit from switching plugs or changing settings.
* Security headers on every response: no framing (clickjacking), nosniff, a content security policy for the page.

Pure ASGI (not BaseHTTPMiddleware), so the live-update and camera streams aren't buffered or delayed.
"""
from __future__ import annotations

import base64
import hashlib
import ipaddress
import os
import re
from typing import Iterable
from urllib.parse import urlsplit

LOCAL_SUFFIXES = (".local", ".lan", ".home", ".home.arpa", ".internal", ".localdomain", ".intranet", ".private",
                  ".localhost")
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def allowed_hosts_from_env() -> list[str]:
    return [h.strip().lower() for h in os.environ.get("PRINTER_DASH_ALLOWED_HOSTS", "").split(",") if h.strip()]


def split_host(host: str) -> str:
    """Host header -> bare lowercase hostname (no port, no IPv6 brackets)."""
    host = (host or "").strip().lower()
    if host.startswith("["):
        return host[1:host.find("]")] if "]" in host else host
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def host_allowed(host: str, extra: Iterable[str] = ()) -> bool:
    h = split_host(host)
    if not h:
        return False
    for pat in extra:
        if pat == "*" or pat == h or (pat.startswith("*.") and h.endswith(pat[1:])):
            return True
    try:
        ipaddress.ip_address(h)
        return True
    except ValueError:
        pass
    return h == "localhost" or "." not in h or h.endswith(LOCAL_SUFFIXES)


def script_hashes(html: str) -> list[str]:
    """CSP hashes for the page's inline <script> blocks (the early theme switch)."""
    return [f"'sha256-{base64.b64encode(hashlib.sha256(m.encode()).digest()).decode()}'"
            for m in re.findall(r"<script>(.*?)</script>", html, re.S)]


def csp(hashes: list[str]) -> str:
    return "; ".join([
        "default-src 'self'",
        "script-src 'self' " + " ".join(hashes),
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
        "font-src https://fonts.gstatic.com",
        "img-src 'self' data: blob:",
        "connect-src 'self'",
        "frame-src http: https:",              # U1 extended firmware "Smooth video" player on the printer itself
        "frame-ancestors 'none'",
        "base-uri 'none'",
        "form-action 'self'",
        "object-src 'none'",
    ])


class Guard:
    def __init__(self, app, page_csp: str, extra_hosts: Iterable[str] = ()):
        self.app = app
        self.page_csp = page_csp.encode()
        self.extra = [h.lower() for h in extra_hosts]

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        host = headers.get("host", "")
        if not host_allowed(host, self.extra):
            return await self._refuse(send, 421, (
                f"This dashboard doesn't answer to the name {split_host(host)!r}. If that's your own address (for "
                f"example a domain behind Cloudflare Access), add it to PRINTER_DASH_ALLOWED_HOSTS - see the README, "
                f"Configuration."))
        if scope["method"] not in SAFE_METHODS and self._cross_site(headers):
            return await self._refuse(send, 403, "Refused: a request from another website.")

        async def send_with_headers(msg):
            if msg["type"] == "http.response.start":
                h = list(msg.get("headers", []))
                names = {k.lower() for k, _ in h}
                h += [(k, v) for k, v in (
                    (b"x-content-type-options", b"nosniff"),
                    (b"x-frame-options", b"DENY"),
                    (b"referrer-policy", b"same-origin"),
                ) if k not in names]
                ctype = next((v for k, v in h if k.lower() == b"content-type"), b"")
                if ctype.startswith(b"text/html") and b"content-security-policy" not in names:
                    h.append((b"content-security-policy", self.page_csp))
                msg = {**msg, "headers": h}
            await send(msg)
        await self.app(scope, receive, send_with_headers)

    @staticmethod
    def _cross_site(headers: dict) -> bool:
        sfs = headers.get("sec-fetch-site")
        if sfs:
            return sfs not in ("same-origin", "none")
        origin = headers.get("origin")
        if not origin or origin == "null":
            return origin == "null"
        return urlsplit(origin).netloc.lower() != headers.get("host", "").lower()

    @staticmethod
    async def _refuse(send, status: int, text: str):
        body = text.encode()
        await send({"type": "http.response.start", "status": status, "headers": [
            (b"content-type", b"text/plain; charset=utf-8"), (b"content-length", str(len(body)).encode()),
            (b"x-content-type-options", b"nosniff")]})
        await send({"type": "http.response.body", "body": body})
