"""Govee smart plugs (Platform API) - manual power on/off for printers, with safeguards.

Verified on a real account 2026-10-30 (tools/govee_probe.py): 8 x H5086 sockets, each exposing only
devices.capabilities.on_off / powerSwitch (1 = on, 0 = off); state read also returns `online`.
API: base https://openapi.api.govee.com, header Govee-API-Key.
  GET  /router/api/v1/user/devices   30/min per account
  POST /router/api/v1/device/state   30/min per device   {"requestId", "payload": {"sku", "device"}}
  POST /router/api/v1/device/control 120/min per device  {"requestId", "payload": {"sku", "device",
                                                           "capability": {"type", "instance", "value"}}}
Safeguards : turning a plug off while the printer is printing / preparing / paused / downloading, or while the
nozzle is hot, is allowed - it is the emergency stop - but only with the printer's name typed as confirmation. The
server enforces this; the page only asks. Every on/off is logged.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from .models import JobState, PrinterStatus

log = logging.getLogger(__name__)

# Power commands get their own threads, so they never queue behind printer polling or plug-state reads
# (those share asyncio's default thread pool).
_POWER_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="govee-power")

BASE = "https://openapi.api.govee.com"
# Govee allows ~10,000 requests per ACCOUNT per DAY (all endpoints share it) [secondary: govee-api2 README; v1 docs
# list the same daily number + X-RateLimit-* headers]. Polling must never use up what an emergency stop needs:
STATE_EVERY_S = 300          # every 5 min , but only plugs whose printer is NOT online - an online printer's
                             # plug is on by definition. Worst case all 8 offline all day: ~2,300 requests/day.
TRUST_PRINTER_AFTER_OFF_S = 300   # after we cut power the printer can still look online for a while: ask the plug
OFF_COOLDOWN_S = 15
# Govee can answer "success" to a control request that the plug never carries out (seen on a real U1-C plug
# 2026-10-30: first "on" accepted, plug stayed off). So every on/off is read back and re-sent if needed.
VERIFY_READS = (1.5, 2.5, 3.5)   # seconds to wait before each read-back
SEND_TRIES = 2                   # send the command at most this many times          # after turning a plug off, "on" waits this long (power supply must discharge first)
DEVICES_EVERY_S = 6 * 3600   # device list 4x/day
DAILY_LIMIT = 10_000
POLL_BUDGET = 3_000          # background polling stops for the day after this many of OUR requests ...
RESERVE = 2_000              # ... or when Govee says fewer than this remain. Power on/off is never held back.
ON_DEMAND_GAP_S = 60         # Details "refresh" at most once a minute per plug
HOT_NOZZLE_C = 50
STARTING_UP_S = 240          # after power-on, show "Starting up" until the printer reconnects (max 4 min)
ACTIVE = {JobState.PRINTING, JobState.PREPARING, JobState.PAUSED}

LOG_SCHEMA = """
CREATE TABLE IF NOT EXISTS power_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    printer_id TEXT,
    printer    TEXT,
    plug       TEXT,
    at         TEXT NOT NULL,
    action     TEXT NOT NULL,      -- on | off
    ok         INTEGER NOT NULL,
    note       TEXT                -- e.g. "emergency stop while printing", error text
);
"""


class GoveeError(Exception):
    pass


class GoveeAuthError(GoveeError):
    pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Usage:
    """Requests we sent today (UTC) + what Govee's headers say is left. Shared by every client of one Plugs."""

    def __init__(self):
        self.day = ""
        self.sent = 0
        self.remaining: Optional[int] = None
        self.limited_until = 0.0          # wall time; set on HTTP 429
        self._lock = threading.Lock()

    def count(self, headers=None) -> None:
        with self._lock:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if today != self.day:
                self.day, self.sent, self.remaining = today, 0, None
            self.sent += 1
            if headers is not None:
                v = headers.get("X-RateLimit-Remaining")
                if v is not None and str(v).strip().lstrip("-").isdigit():
                    self.remaining = int(v)

    def polling_allowed(self) -> bool:
        if time.time() < self.limited_until:
            return False
        if self.remaining is not None and self.remaining < RESERVE:
            return False
        return self.sent < POLL_BUDGET

    def view(self) -> dict:
        return {"sent_today": self.sent, "remaining": self.remaining, "daily_limit": DAILY_LIMIT,
                "polling": self.polling_allowed(),
                "limited_until": datetime.fromtimestamp(self.limited_until, timezone.utc).isoformat(timespec="seconds")
                if self.limited_until > time.time() else None}


class GoveeClient:
    def __init__(self, key: str, base: Optional[str] = None, usage: Optional[Usage] = None):
        self.key, self.base = key, base or BASE
        self.usage = usage or Usage()

    def _call(self, method: str, path: str, body: Optional[dict] = None) -> dict:
        req = urllib.request.Request(self.base + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Govee-API-Key": self.key, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                self.usage.count(r.headers)
                data = json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            self.usage.count(e.headers)
            text = e.read().decode(errors="replace")[:200]
            if e.code in (401, 403):
                raise GoveeAuthError("Govee rejected the API key") from e
            if e.code == 429:
                ra = (e.headers or {}).get("Retry-After")
                wait = int(ra) if ra and str(ra).isdigit() else 3600
                self.usage.limited_until = time.time() + wait
                raise GoveeError("Govee's daily request limit is used up. Use the Govee app or the plug's button "
                                 "until it resets.") from e
            raise GoveeError(f"Govee HTTP {e.code}: {text}") from e
        except (urllib.error.URLError, OSError) as e:
            raise GoveeError(f"can't reach Govee ({e})") from e
        code = data.get("code")
        if code in (401, 403):
            raise GoveeAuthError("Govee rejected the API key")
        if code not in (200, None):
            raise GoveeError(f"Govee error {code}: {data.get('message') or data.get('msg')}")
        return data

    def plugs(self) -> list[dict]:
        """Sockets with on/off only (lights etc. are ignored)."""
        out = []
        for d in self._call("GET", "/router/api/v1/user/devices").get("data") or []:
            caps = d.get("capabilities") or []
            if not any(c.get("type") == "devices.capabilities.on_off" and c.get("instance") == "powerSwitch"
                       for c in caps):
                continue
            if not str(d.get("type", "")).endswith("socket"):
                continue
            out.append({"sku": d.get("sku"), "device": d.get("device"), "name": d.get("deviceName") or d.get("sku")})
        return out

    def state(self, sku: str, device: str) -> dict:
        r = self._call("POST", "/router/api/v1/device/state",
                       {"requestId": str(uuid.uuid4()), "payload": {"sku": sku, "device": device}})
        st = {}
        for c in ((r.get("payload") or {}).get("capabilities") or []):
            st[c.get("instance")] = (c.get("state") or {}).get("value")
        on = st.get("powerSwitch")
        return {"on": True if on == 1 else False if on == 0 else None, "online": st.get("online")}

    def set_power(self, sku: str, device: str, on: bool) -> None:
        self._call("POST", "/router/api/v1/device/control", {
            "requestId": str(uuid.uuid4()),
            "payload": {"sku": sku, "device": device, "capability": {
                "type": "devices.capabilities.on_off", "instance": "powerSwitch", "value": 1 if on else 0}}})


def needs_confirmation(st: Optional[PrinterStatus]) -> Optional[str]:
    """Why turning this printer off needs the typed confirmation, or None if it's safe."""
    if st is None or st.connection.value != "online":
        return None
    if st.state in ACTIVE or st.job.transferring:
        word = "paused mid-print" if st.state == JobState.PAUSED else "printing"
        return f"It is {word}. Cutting power ends the print and it can't be resumed."
    if st.busy_with == "firmware update":
        return "It is installing a firmware update. Cutting power now can leave the printer unable to start."
    if st.busy_with:
        return f"It is busy ({st.busy_with}). Cutting power stops that."
    for t in st.temps:
        if t.key.startswith(("nozzle", "extruder")) and (t.current or 0) > HOT_NOZZLE_C:
            return f"The nozzle is still hot ({round(t.current)}°C). Without power the hotend fan stops, which can clog it."
    return None


def match_plug(printer_name: str, plugs: list[dict], taken: set[str]) -> Optional[dict]:
    """Suggest a plug by name: exact, else the plug name is the start of the printer name (X1 -> X1C)."""
    norm = lambda s: "".join(ch for ch in (s or "").lower() if ch.isalnum())
    pn = norm(printer_name)
    free = [p for p in plugs if p["device"] not in taken]
    for p in free:
        if norm(p["name"]) == pn:
            return p
    pref = [p for p in free if norm(p["name"]) and pn.startswith(norm(p["name"]))]
    return pref[0] if len(pref) == 1 else None


def suggest_plugs(printers: list[tuple[str, str]], plugs: list[dict], taken_plugs: set[str]) -> dict[str, str]:
    """printer id -> plug device for printers without a plug. Each plug goes to at most one printer: an exact name
    match wins, else the closest name it starts (plug "A1" -> printer "A1-1", not "A1 mini-1")."""
    norm = lambda s: "".join(ch for ch in (s or "").lower() if ch.isalnum())
    best: dict[str, tuple[int, str]] = {}            # device -> (extra characters, printer id)
    for pid, name in printers:
        p = match_plug(name, plugs, taken_plugs)
        if p:
            score = len(norm(name)) - len(norm(p["name"]))
            if p["device"] not in best or score < best[p["device"]][0]:
                best[p["device"]] = (score, pid)
    return {pid: dev for dev, (_, pid) in best.items()}


class Plugs:
    """Account link, plug list + state cache, power actions and the log."""

    def __init__(self, store, base: Optional[str] = None):
        self.store, self.base = store, base
        self.plug_list: list[dict] = []
        self.states: dict[str, dict] = {}        # device -> {"on", "online", "at"}
        self.error: Optional[str] = None
        self.powered_on_at: dict[str, float] = {}   # printer_id -> monotonic time of the last power-on
        self.powered_off_at: dict[str, float] = {}  # printer_id -> monotonic time of the last power-off
        self._status_of = lambda pid: None
        self._task: Optional[asyncio.Task] = None
        self._wake = asyncio.Event()
        self._devices_at = 0.0
        self.usage = Usage()
        self._state_at: dict[str, float] = {}
        with store._lock:
            store._db.executescript(LOG_SCHEMA)
            store._db.commit()

    # ------------------------------------------------------------------ account
    def key(self) -> Optional[str]:
        s = self.store.get_app_setting("govee") or {}
        return s.get("key") or None

    def client(self) -> GoveeClient:
        k = self.key()
        if not k:
            raise GoveeError("no Govee API key saved")
        return GoveeClient(k, self.base, self.usage)

    async def link(self, key: str) -> list[dict]:
        plugs = await asyncio.to_thread(GoveeClient(key.strip(), self.base, self.usage).plugs)   # validates the key
        self.store.set_app_setting("govee", {"key": key.strip(), "linked_at": _now_iso()})
        self.plug_list, self.error, self._devices_at = plugs, None, time.monotonic()
        self._wake.set()
        return plugs

    def unlink(self) -> None:
        self.store.set_app_setting("govee", None)
        self.plug_list, self.states, self.error = [], {}, None

    # ------------------------------------------------------------------ background refresh
    def start(self, on_change, status_of=None) -> None:
        self._on_change = on_change
        if status_of:
            self._status_of = status_of
        self._task = asyncio.create_task(self._loop())

    def stop(self) -> None:
        if self._task:
            self._task.cancel()

    async def _loop(self) -> None:
        while True:
            try:
                await self.refresh()
            except Exception as e:  # noqa: BLE001
                log.info("Govee refresh: %s", e)
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), STATE_EVERY_S)
            except asyncio.TimeoutError:
                pass

    def assigned(self) -> dict[str, dict]:
        """printer_id -> plug {sku, device, name}."""
        return {c.id: c.settings["plug"] for c in self.store.list() if (c.settings.get("plug") or {}).get("device")}

    async def refresh(self, only: Optional[str] = None) -> None:
        """Background poll (or one plug on demand). Skipped entirely once the polling budget is spent, so the
        daily allowance stays available for turning plugs on/off."""
        if not self.key() or not self.usage.polling_allowed():
            return
        try:
            if only is None and (time.monotonic() - self._devices_at > DEVICES_EVERY_S or not self.plug_list):
                self.plug_list = await asyncio.to_thread(self.client().plugs)
                self._devices_at = time.monotonic()
            changed = False
            for pid, plug in self.assigned().items():
                if only and plug["device"] != only:
                    continue
                pst = self._status_of(pid)
                off_at = self.powered_off_at.get(pid)
                just_cut = off_at is not None and time.monotonic() - off_at < TRUST_PRINTER_AFTER_OFF_S
                if pst is not None and pst.connection.value == "online" and not just_cut:
                    old = self.states.get(plug["device"], {})
                    if old.get("on") is not True:                  # printer is talking to us -> its plug is on
                        self.states[plug["device"]] = {"on": True, "online": old.get("online"), "at": _now_iso(),
                                                       "from_printer": True}
                        changed = True
                    continue
                if not self.usage.polling_allowed():
                    break
                st = await asyncio.to_thread(self.client().state, plug["sku"], plug["device"])
                self._state_at[plug["device"]] = time.monotonic()
                old = self.states.get(plug["device"], {})
                self.states[plug["device"]] = {**st, "at": _now_iso()}
                changed |= old.get("on") != st["on"] or old.get("online") != st["online"]
            self.error = None
            if changed:
                self._on_change()
        except GoveeAuthError as e:
            self.error = str(e)
        except GoveeError as e:
            self.error = str(e)

    # ------------------------------------------------------------------ views
    def view(self, printer_id: str, plug: Optional[dict], st: Optional[PrinterStatus]) -> Optional[dict]:
        if not plug or not plug.get("device"):
            return None
        s = self.states.get(plug["device"], {})
        started = self.powered_on_at.get(printer_id)
        starting = bool(started and time.monotonic() - started < STARTING_UP_S and s.get("on") is not False
                        and not (st and st.connection.value == "online"))
        off_at = self.powered_off_at.get(printer_id)
        wait = max(0, round(OFF_COOLDOWN_S - (time.monotonic() - off_at))) if off_at else 0
        return {"name": plug.get("name"), "device": plug.get("device"), "on": s.get("on"), "online": s.get("online"),
                "checked_at": s.get("at"), "from_printer": bool(s.get("from_printer")), "starting": starting,
                "on_wait_s": wait}

    async def refresh_one(self, device: str) -> None:
        """Details opened: read this plug's state now, at most once a minute (skipped if its printer is online)."""
        if time.monotonic() - self._state_at.get(device, 0.0) >= ON_DEMAND_GAP_S:
            await self.refresh(only=device)

    def account_view(self) -> dict:
        assigned = {p["device"]: pid for pid, p in self.assigned().items()}
        return {"linked": bool(self.key()), "error": self.error, "usage": self.usage.view(),
                "plugs": [{**p, **{k: v for k, v in self.states.get(p["device"], {}).items() if k != "at"},
                           "printer_id": assigned.get(p["device"])} for p in self.plug_list]}

    def log_rows(self, printer_id: str, limit: int = 5) -> list[dict]:
        with self.store._lock:
            rows = self.store._db.execute("SELECT at, action, ok, note, plug FROM power_log WHERE printer_id = ? "
                                          "ORDER BY id DESC LIMIT ?", (printer_id, limit)).fetchall()
        return [{"at": r[0], "action": r[1], "ok": bool(r[2]), "note": r[3], "plug": r[4]} for r in rows]

    def _log(self, printer_id, printer, plug, action, ok, note):
        with self.store._lock:
            self.store._db.execute("INSERT INTO power_log (printer_id, printer, plug, at, action, ok, note) "
                                   "VALUES (?, ?, ?, ?, ?, ?, ?)",
                                   (printer_id, printer, plug, _now_iso(), action, int(ok), note))
            self.store._db.commit()

    # ------------------------------------------------------------------ action
    def _after_switch(self, printer_id: str, on: bool) -> None:
        if on:
            self.powered_on_at[printer_id] = time.monotonic()
            self.powered_off_at.pop(printer_id, None)
        else:
            self.powered_on_at.pop(printer_id, None)
            self.powered_off_at[printer_id] = time.monotonic()

    async def set_power(self, cfg, st: Optional[PrinterStatus], on: bool, confirm: Optional[str],
                        note: Optional[str] = None) -> dict:
        plug = cfg.settings.get("plug") or {}
        if not plug.get("device"):
            raise GoveeError("no plug assigned to this printer")
        if on:
            off_at = self.powered_off_at.get(cfg.id)
            if off_at and time.monotonic() - off_at < OFF_COOLDOWN_S:
                left = round(OFF_COOLDOWN_S - (time.monotonic() - off_at))
                return {"wait_s": max(1, left)}
        if not on:
            why = needs_confirmation(st)
            if why:
                if (confirm or "").strip().lower() != cfg.name.strip().lower():
                    return {"needs_confirm": True, "reason": why, "type": cfg.name}
                note = "; ".join(x for x in (note, "emergency stop: " + why.split(".")[0]) if x)
        loop = asyncio.get_running_loop()
        client = self.client()
        confirmed, last = False, None
        try:
            for attempt in range(SEND_TRIES):
                await loop.run_in_executor(_POWER_POOL, client.set_power, plug["sku"], plug["device"], on)
                for wait in VERIFY_READS:
                    await asyncio.sleep(wait)
                    try:
                        last = await loop.run_in_executor(_POWER_POOL, client.state, plug["sku"], plug["device"])
                    except GoveeError:
                        continue                           # a failed read-back isn't a failed switch: keep checking
                    if last.get("on") is on:
                        confirmed = True
                        break
                if confirmed or last is None:              # can't read the plug at all: don't guess, don't re-send
                    break
                log.warning("printer %s: plug %s did not switch %s after send %d - sending again", cfg.id,
                            plug.get("name"), "on" if on else "off", attempt + 1)
        except GoveeError as e:
            self._log(cfg.id, cfg.name, plug.get("name"), "on" if on else "off", False, str(e))
            raise
        if last is None:                                   # command accepted, read-back impossible (e.g. limit)
            self.states[plug["device"]] = {**self.states.get(plug["device"], {}), "on": on, "at": _now_iso()}
            self._log(cfg.id, cfg.name, plug.get("name"), "on" if on else "off", True,
                      "; ".join(x for x in (note, "sent - couldn't read the plug back to confirm") if x))
            self._after_switch(cfg.id, on)
            return {"ok": True, "on": on, "unconfirmed": True}
        self.states[plug["device"]] = {**last, "at": _now_iso()}
        self._state_at[plug["device"]] = time.monotonic()
        if not confirmed:
            seen = "on" if (last or {}).get("on") else "off" if (last or {}).get("on") is False else "unknown"
            msg = (f"Govee accepted the command but the plug still reports {seen}. Try again, or use the Govee app "
                   f"or the plug's button.")
            self._log(cfg.id, cfg.name, plug.get("name"), "on" if on else "off", False, "not confirmed by the plug")
            raise GoveeError(msg)
        self._log(cfg.id, cfg.name, plug.get("name"), "on" if on else "off", True,
                  "; ".join(x for x in (note, "sent twice" if attempt else None) if x) or None)
        self._after_switch(cfg.id, on)
        log.warning("printer %s (%s): plug %s turned %s%s", cfg.id, cfg.name, plug.get("name"),
                    "ON" if on else "OFF", f" - {note}" if note else "")
        return {"ok": True, "on": on}
