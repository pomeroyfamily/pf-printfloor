"""Automatic power cycle (2026-11-04/05, docs/SPEC.md "Auto power cycle").

Per printer, OFF by default, only with a Govee plug: once a day turn the plug off, wait 15 s, turn it on.

When (all of):
  * a print has happened since the last reboot: dashboard power cycle (manual or automatic) or, for printers
    that report uptime (U1 / Klipper), the printer's own boot time,
  * the printer is online and Finished or Idle (never Failed / Cancelled / Paused / Error - someone should look),
    not printing / preparing / downloading, nozzle under 50 C, no firmware update or AMS drying in progress,
  * the plug is not known to be off (never switches a plug ON that someone turned off),
  * it has been in that state for `idle_min` minutes (the countdown; "Restart countdown" starts it again),
  * the cycle is due: at the daily time (local), unless a power cycle happened < 12 h before it; or overdue
    (> 24 h since the last power cycle) - then at the first eligible moment,
  * not paused (a pause lasts until the next daily time after it was set, then clears itself).

Safety: before switching off, "cycle in progress" is saved; at startup an unfinished cycle turns the plug back on.
The "on" step retries for a few minutes. Printers are cycled at least STAGGER_S apart.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, time as dtime, timedelta, timezone
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .models import ConnState, JobState, PrinterStatus
from .plugs import HOT_NOZZLE_C, OFF_COOLDOWN_S, GoveeError

log = logging.getLogger(__name__)

TICK_S = 30
STAGGER_S = 60
SKIP_IF_WITHIN_H = 12
OVERDUE_H = 24
ON_RETRY_S = (10, 20, 30, 60, 60)          # "on" retries after the first try (~3 min in all)
FAILED_BACKOFF_S = 30 * 60
DEFAULTS = {"enabled": False, "time": "06:00", "idle_min": 30}
SETTLED = (JobState.FINISHED, JobState.IDLE)
STATE_KEY = "autocycle_state"


def _utc() -> datetime:
    return datetime.now(timezone.utc)


def _iso(d: Optional[datetime]) -> Optional[str]:
    return d.isoformat(timespec="seconds") if d else None


def _dt(s: Any) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(s) if s else None
    except (TypeError, ValueError):
        return None


def settings_of(cfg) -> dict:
    s = {**DEFAULTS, **(cfg.settings.get("autocycle") or {})}
    try:
        hh, mm = (int(x) for x in str(s["time"]).split(":"))
        s["time"] = f"{hh:02d}:{mm:02d}" if 0 <= hh < 24 and 0 <= mm < 60 else DEFAULTS["time"]
    except ValueError:
        s["time"] = DEFAULTS["time"]
    s["idle_min"] = max(1, min(24 * 60, int(s.get("idle_min") or DEFAULTS["idle_min"])))
    s["enabled"] = bool(s.get("enabled"))
    return s


def blocked_by(st: Optional[PrinterStatus]) -> Optional[str]:
    """Why the printer can't be power cycled right now (None = it can)."""
    if st is None or st.connection != ConnState.ONLINE:
        return "printer not online"
    if st.state not in SETTLED:
        return {JobState.PRINTING: "printing", JobState.PREPARING: "preparing", JobState.PAUSED: "paused"}.get(
            st.state, f"{st.state.value} - needs a look")
    if st.job.transferring:
        return "receiving a file"
    if st.busy_with:
        return st.busy_with
    for t in st.temps:
        if t.key.startswith(("nozzle", "extruder")) and (t.current or 0) > HOT_NOZZLE_C:
            return "nozzle still hot"
    return None


class AutoCycle:
    def __init__(self, store, plugs, status_of: Callable[[str], Optional[PrinterStatus]],
                 on_change: Callable[[], None], now: Callable[[], datetime] = _utc):
        self.store, self.plugs, self.status_of, self.on_change, self.now = store, plugs, status_of, on_change, now
        self._task: Optional[asyncio.Task] = None
        self._busy: set[str] = set()                 # cycling right now
        self._last_start: Optional[datetime] = None
        self._views: dict[str, dict] = {}
        self._prev_state: dict[str, JobState] = {}

    # ------------------------------------------------------------------ persistence
    def _all(self) -> dict:
        return dict(self.store.get_app_setting(STATE_KEY) or {})

    def _get(self, pid: str) -> dict:
        return dict(self._all().get(pid) or {})

    def _put(self, pid: str, **kv) -> dict:
        allst = self._all()
        cur = {**(allst.get(pid) or {}), **kv}
        allst[pid] = {k: v for k, v in cur.items() if v is not None}
        self.store.set_app_setting(STATE_KEY, allst)
        return allst[pid]

    def tz(self) -> ZoneInfo:
        name = (self.store.get_app_setting("dashboard") or {}).get("tz") or os.environ.get("TZ") or "UTC"
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            return ZoneInfo("UTC")

    # ------------------------------------------------------------------ events from the rest of the app
    def observe(self, st: PrinterStatus) -> None:
        """Every status update: remember that a print happened, and when it settled (countdown start)."""
        pid = st.printer_id
        prev = self._prev_state.get(pid)
        self._prev_state[pid] = st.state
        if st.connection != ConnState.ONLINE or prev == st.state:
            return
        if st.state in (JobState.PRINTING, JobState.PREPARING):
            self._put(pid, printed_at=_iso(self.now()))
        if st.state in SETTLED and (prev is not None or not self._get(pid).get("settled_at")):
            self._put(pid, settled_at=_iso(self.now()))

    def manual_cycle(self, pid: str) -> None:
        """Turned back on from the dashboard after being off: counts as a power cycle (dashboard only)."""
        self._put(pid, last_cycle=_iso(self.now()))
        self.on_change()

    def configure(self, cfg, enabled: bool, at: str, idle_min: int) -> None:
        if enabled and not self._get(cfg.id).get("last_cycle"):
            self._put(cfg.id, last_cycle=_iso(self.now()))      # baseline: first cycle at a daily time >= 12 h away
        self.on_change()

    def resume(self, pid: str) -> None:
        allst = self._all()
        rec = dict(allst.get(pid) or {})
        rec.pop("paused_until", None)
        allst[pid] = rec
        self.store.set_app_setting(STATE_KEY, allst)

    def restart_countdown(self, pid: str) -> None:
        self._put(pid, settled_at=_iso(self.now()))

    # ------------------------------------------------------------------ schedule
    def _daily_at(self, day_local: datetime, hhmm: str) -> datetime:
        hh, mm = (int(x) for x in hhmm.split(":"))
        return datetime.combine(day_local.date(), dtime(hh, mm), tzinfo=day_local.tzinfo)

    def _next_daily(self, after_local: datetime, hhmm: str) -> datetime:
        """First daily time strictly after `after_local`."""
        t = self._daily_at(after_local, hhmm)
        return t if t > after_local else self._daily_at(after_local + timedelta(days=1), hhmm)

    def _due(self, s: dict, last: datetime, now: datetime) -> tuple[bool, datetime]:
        """(due now?, when the next cycle becomes due). Daily time unless a power cycle happened < 12 h before
        it; overdue (> 24 h since the last one) means due right away."""
        tz = self.tz()
        overdue_at = last + timedelta(hours=OVERDUE_H)
        t = self._daily_at(now.astimezone(tz), s["time"])
        if t > now:
            t -= timedelta(days=1)                       # the most recent daily time (today's or yesterday's)
        due_daily = last < t - timedelta(hours=SKIP_IF_WITHIN_H)
        if due_daily or now >= overdue_at:
            return True, min(t, overdue_at) if due_daily else overdue_at
        nxt = t + timedelta(days=1)
        while last >= nxt - timedelta(hours=SKIP_IF_WITHIN_H):
            nxt += timedelta(days=1)
        return False, min(nxt, overdue_at)

    def plan(self, cfg) -> dict:
        """What happens next. phase: off | none | scheduled | waiting | countdown | paused | cycling | failed.
        `at` (ISO UTC) = when it's scheduled / when the countdown ends."""
        s = settings_of(cfg)
        if not s["enabled"] or not (cfg.settings.get("plug") or {}).get("device"):
            return {"phase": "off"}
        if cfg.id in self._busy:
            return {"phase": "cycling"}
        rec = self._get(cfg.id)
        now = self.now()
        until = _dt(rec.get("paused_until"))
        if until and now < until:
            return {"phase": "paused", "until": _iso(until)}
        last = self.last_reboot(cfg.id, rec) or now
        printed = _dt(rec.get("printed_at"))
        if not printed or printed <= last:
            return {"phase": "none"}                     # nothing printed since the last reboot
        due, when = self._due(s, last, now)
        if not due:
            return {"phase": "scheduled", "at": _iso(when.astimezone(timezone.utc))}
        failed = _dt(rec.get("failed_at"))
        if failed and now - failed < timedelta(seconds=FAILED_BACKOFF_S):
            return {"phase": "failed", "note": rec.get("note")}
        why = blocked_by(self.status_of(cfg.id))
        if (self.plugs.states.get(cfg.settings["plug"]["device"]) or {}).get("on") is False:
            why = "plug is off"
        if why:
            return {"phase": "waiting", "why": why}
        settled = _dt(rec.get("settled_at")) or now
        return {"phase": "countdown", "at": _iso(max(settled + timedelta(minutes=s["idle_min"]), now))}

    def last_reboot(self, pid: str, rec: Optional[dict] = None) -> Optional[datetime]:
        """Latest of: the dashboard's last power cycle, and the printer's own boot time (U1 uptime) - so a
        reboot at the printer counts too. Bambu / CC don't report uptime: dashboard power cycles only."""
        rec = rec if rec is not None else self._get(pid)
        st = self.status_of(pid)
        times = [t for t in (_dt(rec.get("last_cycle")), st.booted_at if st else None) if t]
        return max(times) if times else None

    def pause(self, cfg) -> None:
        """Skip the next cycle: hold until the daily time after the one it belongs to, then clear itself."""
        s, rec, now = settings_of(cfg), self._get(cfg.id), self.now()
        last = self.last_reboot(cfg.id, rec) or now
        due, when = self._due(s, last, now)
        anchor = now if due else when                   # due now: today's is skipped; else the upcoming one
        until = self._next_daily(anchor.astimezone(self.tz()), s["time"])
        self._put(cfg.id, paused_until=_iso(until.astimezone(timezone.utc)))

    # ------------------------------------------------------------------ loop
    def start(self, configs: Callable[[], list]) -> None:
        self._configs = configs
        self._task = asyncio.create_task(self._loop())

    def stop(self) -> None:
        if self._task:
            self._task.cancel()

    async def _loop(self) -> None:
        await self.finish_interrupted()
        while True:
            try:
                await self.tick()
            except Exception:  # noqa: BLE001 - never let the scheduler die
                log.exception("auto power cycle tick failed")
            await asyncio.sleep(TICK_S)

    def view(self, cfg) -> dict:
        s = settings_of(cfg)
        p = self._views.get(cfg.id) or self.plan(cfg)
        rec = self._get(cfg.id)
        return {**s, **p, "last_cycle": rec.get("last_cycle")}

    async def tick(self) -> None:
        changed = False
        for cfg in self._configs():
            p = self.plan(cfg)
            if p != self._views.get(cfg.id):
                self._views[cfg.id] = p
                changed = True
            if p.get("phase") != "countdown" or _dt(p["at"]) > self.now() or self._busy:
                continue
            if self._last_start and (self.now() - self._last_start).total_seconds() < STAGGER_S:
                continue
            self._last_start = self.now()
            asyncio.create_task(self.cycle(cfg))
        if changed:
            self.on_change()

    async def cycle(self, cfg) -> bool:
        pid = cfg.id
        self._busy.add(pid)
        self._views[pid] = {"phase": "cycling"}
        self.on_change()
        try:
            why = blocked_by(self.status_of(pid))            # last look right before cutting power
            if why:
                log.info("auto power cycle %s: skipped at the last moment (%s)", cfg.name, why)
                return False
            self._put(pid, in_progress=_iso(self.now()))
            try:
                await self.plugs.set_power(cfg, self.status_of(pid), False, None, note="automatic power cycle")
            except GoveeError as e:
                self._put(pid, in_progress=None, failed_at=_iso(self.now()), note=f"couldn't turn the plug off: {e}")
                return False
            await asyncio.sleep(OFF_COOLDOWN_S)
            ok = await self._turn_on(cfg)
            if ok:
                self._put(pid, in_progress=None, last_cycle=_iso(self.now()), failed_at=None,
                          note=None, paused_until=None)
            return ok
        finally:
            self._busy.discard(pid)
            self._views.pop(pid, None)
            self.on_change()

    async def _turn_on(self, cfg) -> bool:
        err = None
        for wait in (0, *ON_RETRY_S):
            await asyncio.sleep(wait)
            try:
                r = await self.plugs.set_power(cfg, None, True, None, note="automatic power cycle")
                if r.get("wait_s"):
                    await asyncio.sleep(r["wait_s"])
                    r = await self.plugs.set_power(cfg, None, True, None, note="automatic power cycle")
                if r.get("ok"):
                    return True
            except GoveeError as e:
                err = e
                log.warning("auto power cycle %s: turning the plug back on failed (%s) - retrying", cfg.name, e)
        self._put(cfg.id, in_progress=None, failed_at=_iso(self.now()),
                  note=f"plug did not turn back on: {err}" if err else "plug did not turn back on")
        return False

    async def finish_interrupted(self) -> None:
        """Dashboard restarted between OFF and ON: turn those plugs back on."""
        for cfg in self._configs():
            if self._get(cfg.id).get("in_progress") and (cfg.settings.get("plug") or {}).get("device"):
                log.warning("auto power cycle %s: was interrupted - turning the plug back on", cfg.name)
                self._busy.add(cfg.id)
                try:
                    if await self._turn_on(cfg):
                        self._put(cfg.id, in_progress=None, last_cycle=_iso(self.now()))
                finally:
                    self._busy.discard(cfg.id)
