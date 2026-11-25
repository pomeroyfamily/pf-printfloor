"""Read-only MCP server, so Claude (or any MCP client) can look at the dashboard.

Off until a token is created in Settings -> Claude connector. Then POST /mcp/<token> (secret URL), or POST /mcp with
`Authorization: Bearer <token>` / `X-API-Key: <token>`, speaks MCP over Streamable HTTP, answered as plain JSON (no
streaming, no sessions: every tool is a quick read).

Read-only by design: nothing here can switch a plug, change a setting or talk to a printer. Printer addresses,
serials, secrets and camera URLs are never included in answers (they would leave the house: a connector added in
Claude is called from Anthropic's servers).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from datetime import datetime, timezone
from typing import Any, Optional

from . import __version__
from .cameras import CameraUnavailable, FRAME_TIMEOUT_S
from .history import usage_key

TOKEN_KEY = "mcp_token"
TOKEN_PREFIX = "pfp_"
PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")   # newest first
LAST_USED_WRITE_S = 300          # don't write "last used" to the database on every call
FAIL_WINDOW_S, FAIL_LIMIT = 60, 20
MAX_JOBS = 50


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class ToolError(Exception):
    """Shown to the model as a tool result with isError (bad printer name, no camera ...)."""


class Connector:
    """Token handling. Only a hash of the token is stored; the token itself is shown once, when created."""

    def __init__(self, store):
        self.store = store
        self._fails: list[float] = []

    def _get(self) -> Optional[dict]:
        v = self.store.get_app_setting(TOKEN_KEY)
        return v if isinstance(v, dict) and v.get("hash") else None

    def view(self) -> dict[str, Any]:
        v = self._get()
        return {"enabled": bool(v), "hint": v.get("hint") if v else None,
                "created_at": v.get("created_at") if v else None, "last_used_at": v.get("last_used_at") if v else None}

    def create(self) -> str:
        """New token (replaces any old one, which stops working at once)."""
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        self.store.set_app_setting(TOKEN_KEY, {"hash": _hash(token), "hint": token[-4:], "created_at": time.time(),
                                               "last_used_at": None})
        return token

    def disable(self) -> None:
        self.store.delete_app_setting(TOKEN_KEY)

    @property
    def enabled(self) -> bool:
        return self._get() is not None

    def throttled(self) -> bool:
        now = time.monotonic()
        self._fails = [t for t in self._fails if now - t < FAIL_WINDOW_S]
        return len(self._fails) >= FAIL_LIMIT

    @staticmethod
    def token_from(authorization: Optional[str], api_key: Optional[str], path_token: Optional[str]) -> str:
        """Bearer header, X-API-Key header, or the secret URL /mcp/<token> (for Claude accounts that can't add
        request headers to a custom connector)."""
        scheme, _, tok = (authorization or "").partition(" ")
        if scheme.lower() == "bearer" and tok.strip():
            return tok.strip()
        return (api_key or path_token or "").strip()

    def check(self, token: str) -> bool:
        v = self._get()
        if not v:
            return False
        ok = bool(token) and hmac.compare_digest(_hash(token), v["hash"])
        if not ok:
            self._fails.append(time.monotonic())
            return False
        if not v.get("last_used_at") or time.time() - v["last_used_at"] > LAST_USED_WRITE_S:
            self.store.set_app_setting(TOKEN_KEY, {**v, "last_used_at": time.time()})
        return True


# ---------------------------------------------------------------------------------------------- tools
_PRINTER_ARG = {"type": "string", "description": "Printer name (as shown on the dashboard) or id. "
                                                 "Case doesn't matter; a unique part of the name is enough."}
_RO = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}

TOOLS = [
    {"name": "list_printers",
     "title": "List printers",
     "description": "Every printer on the dashboard with its live status: connection, state (printing, paused, "
                    "finished, failed, idle, offline ...), the reason when the printer gave one, current job, "
                    "progress, time left and finish time, smart-plug power, location and tags.",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
     "annotations": _RO},
    {"name": "get_printer",
     "title": "Printer details",
     "description": "Full live detail for one printer: everything in list_printers plus temperatures, fans, "
                    "loaded filament (AMS / toolheads), printer error codes, firmware and update notices.",
     "inputSchema": {"type": "object", "properties": {"printer": _PRINTER_ARG}, "required": ["printer"],
                     "additionalProperties": False},
     "annotations": _RO},
    {"name": "recent_jobs",
     "title": "Print history",
     "description": "Recent print jobs, newest first: printer, job name, start and end time, result "
                    "(finished, failed, cancelled, or running) and print time.",
     "inputSchema": {"type": "object", "properties": {
         "printer": {**_PRINTER_ARG, "description": "Only this printer (name or id). Leave out for all printers."},
         "result": {"type": "string", "enum": ["finished", "failed", "cancelled", "running"],
                    "description": "Only jobs with this result."},
         "limit": {"type": "integer", "minimum": 1, "maximum": MAX_JOBS, "default": 20}},
         "additionalProperties": False},
     "annotations": _RO},
    {"name": "print_stats",
     "title": "Usage and success rate",
     "description": "Lifetime print hours and job counts, jobs tracked by the dashboard, finished vs failed and "
                    "success rate - for one printer, or every printer plus fleet totals.",
     "inputSchema": {"type": "object", "properties": {
         "printer": {**_PRINTER_ARG, "description": "Only this printer (name or id). Leave out for all printers."}},
         "additionalProperties": False},
     "annotations": _RO},
    {"name": "camera_snapshot",
     "title": "Camera snapshot",
     "description": "A current picture from a printer's camera (JPEG). Use it to look at a print in progress "
                    "or check what happened after a failure.",
     "inputSchema": {"type": "object", "properties": {"printer": _PRINTER_ARG}, "required": ["printer"],
                     "additionalProperties": False},
     "annotations": _RO},
]


def _status_brief(st: Optional[dict]) -> dict[str, Any]:
    if not st:
        return {"connection": "unknown", "state": "unknown"}
    job = st.get("job") or {}
    out = {"connection": st.get("connection"), "state": st.get("state"), "reason": st.get("reason"),
           "connection_detail": st.get("connection_detail")}
    if job.get("name") or job.get("file_name") or job.get("progress_pct") is not None:
        out["job"] = {k: job.get(k) for k in ("name", "progress_pct", "elapsed_s", "remaining_s", "eta", "layer",
                                              "total_layers", "plate", "plate_count", "sub_state") if job.get(k) is not None}
    if st.get("connection") != "online" and st.get("last_seen_at"):
        out["last_seen_at"] = st.get("last_seen_at")
    if st.get("busy_with"):
        out["busy_with"] = st["busy_with"]
    return {k: v for k, v in out.items() if v not in (None, "", [], {})}


def _power(p: Optional[dict]) -> Optional[dict]:
    if not p:
        return None
    return {k: p.get(k) for k in ("name", "on", "online", "starting") if p.get(k) is not None} or None


class Tools:
    def __init__(self, mgr):
        self.mgr = mgr

    # -- helpers
    def _views(self) -> list[dict]:
        return self.mgr.printers()

    def _find(self, query: Any) -> dict:
        q = str(query or "").strip().lower()
        if not q:
            raise ToolError("Say which printer (its name or id).")
        views = self._views()
        names = ", ".join(v["name"] for v in views) or "none"
        for test in (lambda v: v["id"].lower() == q, lambda v: v["name"].lower() == q):
            hit = [v for v in views if test(v)]
            if len(hit) == 1:
                return hit[0]
        hit = [v for v in views if q in v["name"].lower()]
        if len(hit) == 1:
            return hit[0]
        if hit:
            raise ToolError(f"{query!r} matches more than one printer: {', '.join(v['name'] for v in hit)}.")
        raise ToolError(f"No printer called {query!r}. Printers: {names}.")

    def _summary(self, v: dict, loc_names: Optional[dict] = None) -> dict:
        if loc_names is None:
            loc_names = {x["id"]: x["name"] for x in self.mgr.locations()["items"]}
        loc = v.get("location")
        d = {"id": v["id"], "name": v["name"], "model": " ".join(x for x in (v.get("maker"), v.get("model")) if x),
             **_status_brief(v.get("status")), "location": loc_names.get(loc, loc) or None, "tags": v.get("tags") or None,
             "power": _power(v.get("power"))}
        return {k: x for k, x in d.items() if x not in (None, "", [], {})}

    def _cfg(self, v: dict):
        cfg = self.mgr.store.get(v["id"])
        if not cfg:
            raise ToolError("That printer was just removed.")
        return cfg

    # -- tools
    async def list_printers(self, args: dict) -> list[dict]:
        d = self.mgr.dashboard()
        names = {x["id"]: x["name"] for x in self.mgr.locations()["items"]}
        return _json({"dashboard": d["name"], "time_zone": d.get("tz"),
                      "now": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                      "printers": [self._summary(v, names) for v in self._views()]})

    async def get_printer(self, args: dict) -> list[dict]:
        v = self._find(args.get("printer"))
        st = v.get("status") or {}
        d = self._summary(v)
        d.update({
            "temperatures": [{k: t.get(k) for k in ("label", "current", "target") if t.get(k) is not None}
                             for t in st.get("temps") or []] or None,
            "fans": [{"label": f.get("label"), "percent": f.get("percent")} for f in st.get("fans") or []] or None,
            "filament": [{"unit": u.get("kind"), "humidity_pct": u.get("humidity_pct"),
                          "slots": [{k: s.get(k) for k in ("index", "active", "material", "sub_type", "brand",
                                                           "color_hex", "remaining_pct") if s.get(k) not in (None, False)}
                                    for s in u.get("slots") or [] if s.get("present") is not False]}
                         for u in st.get("filament") or []] or None,
            "errors": st.get("errors") or None,
            "firmware": st.get("firmware"), "update_available": st.get("update_available"),
            "link": st.get("link"), "updated_at": st.get("updated_at"),
            "auto_power_off": (v.get("autocycle") or {}).get("enabled") or None,
            "has_camera": bool((v.get("camera") or {}).get("kind") or (v.get("camera") or {}).get("url")),
        })
        return _json({k: x for k, x in d.items() if x not in (None, "", [], {})})

    async def recent_jobs(self, args: dict) -> list[dict]:
        keys = [usage_key(self._cfg(self._find(args["printer"])))] if args.get("printer") else None
        result = args.get("result")
        if result not in (None, "finished", "failed", "cancelled", "running"):
            raise ToolError("result must be finished, failed, cancelled or running.")
        try:
            limit = min(max(int(args.get("limit") or 20), 1), MAX_JOBS)
        except (TypeError, ValueError):
            raise ToolError("limit must be a number.")
        names = {usage_key(c): c.name for c in self.mgr.store.list()}
        rows = []
        for r in self.mgr.history.jobs(keys, result, limit):
            rows.append({k: x for k, x in {
                "printer": names.get(r["usage_key"]) or r.get("printer_name"), "job": r.get("job_name") or r.get("file_name"),
                "started_at": r.get("started_at"), "ended_at": r.get("ended_at"),
                "result": r.get("result") or ("running" if not r.get("ended_at") else None),
                "print_seconds": r.get("print_seconds"), "source": r.get("source")}.items() if x is not None})
        return _json({"jobs": rows})

    async def print_stats(self, args: dict) -> list[dict]:
        h = self.mgr.history
        cfgs = [self._cfg(self._find(args["printer"]))] if args.get("printer") else self.mgr.store.list()
        rows = []
        for cfg in cfgs:
            u = h.usage(usage_key(cfg))
            rows.append({"printer": cfg.name, **{k: u.get(k) for k in (
                "total_seconds", "total_jobs", "tracked_seconds", "tracked_jobs", "finished", "failed", "success_rate")}})
        out: dict[str, Any] = {"printers": rows}
        if not args.get("printer"):
            fin, fail = sum(r["finished"] or 0 for r in rows), sum(r["failed"] or 0 for r in rows)
            out["fleet"] = {"total_seconds": sum(r["total_seconds"] or 0 for r in rows),
                            "total_jobs": sum(r["total_jobs"] or 0 for r in rows), "finished": fin, "failed": fail,
                            "success_rate": round(100 * fin / (fin + fail)) if fin + fail else None}
        return _json(out)

    async def camera_snapshot(self, args: dict) -> list[dict]:
        v = self._find(args.get("printer"))
        f = self.mgr.cameras.feed(self._cfg(v))
        try:
            f.lease()
        except CameraUnavailable as e:
            raise ToolError(f"{v['name']}: {e}")
        got = await f.next_frame(0, FRAME_TIMEOUT_S)
        if got is None:
            raise ToolError(f"{v['name']}: {f.error or 'the camera did not send a picture'}")
        return [{"type": "text", "text": f"{v['name']} camera, {datetime.now(timezone.utc).isoformat(timespec='seconds')}"},
                {"type": "image", "data": base64.b64encode(got[1]).decode(), "mimeType": "image/jpeg"}]


def _json(obj: Any) -> list[dict]:
    import json
    return [{"type": "text", "text": json.dumps(obj, default=str, separators=(",", ":"))}]


# ---------------------------------------------------------------------------------------------- JSON-RPC
def _err(id_, code: int, msg: str) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": msg}}


async def handle(msg: Any, tools: Tools, name: str) -> Optional[dict]:
    """One JSON-RPC message -> response (None for notifications)."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
        return _err(msg.get("id") if isinstance(msg, dict) else None, -32600, "Invalid request")
    id_, method, params = msg.get("id"), msg["method"], msg.get("params") or {}
    if "id" not in msg:
        return None                                  # notification (initialized, cancelled ...): nothing to say
    if not isinstance(params, dict):
        return _err(id_, -32602, "params must be an object")
    if method == "initialize":
        asked = params.get("protocolVersion")
        return {"jsonrpc": "2.0", "id": id_, "result": {
            "protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "pf-printfloor", "title": name, "version": __version__},
            "instructions": f"Read-only view of the 3D printers on the {name} dashboard. Start with list_printers. "
                            "Times are UTC (ISO 8601); time_zone in list_printers is the dashboard's own zone. "
                            "These tools can't control printers or plugs."}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": id_, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": id_, "result": {"tools": TOOLS}}
    if method == "tools/call":
        tool, args = params.get("name"), params.get("arguments") or {}
        fn = getattr(tools, tool, None) if tool in {t["name"] for t in TOOLS} else None
        if fn is None:
            return _err(id_, -32602, f"Unknown tool: {tool}")
        if not isinstance(args, dict):
            return _err(id_, -32602, "arguments must be an object")
        try:
            content = await fn(args)
            return {"jsonrpc": "2.0", "id": id_, "result": {"content": content, "isError": False}}
        except ToolError as e:
            return {"jsonrpc": "2.0", "id": id_, "result": {"content": [{"type": "text", "text": str(e)}],
                                                             "isError": True}}
    return _err(id_, -32601, f"Method not found: {method}")
