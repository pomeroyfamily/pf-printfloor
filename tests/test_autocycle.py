"""Automatic power cycle rules (2026-11-04/05). Fake clock, fake plugs, real store."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app import autocycle as ac
from app.autocycle import AutoCycle
from app.models import ConnState, JobState, PrinterStatus, Temperature
from app.plugs import GoveeError
from app.store import PrinterStore

TZ = "America/Los_Angeles"
PLUG = {"sku": "H5083", "device": "AA:BB", "name": "U1-C plug"}


def la(y, mo, d, h, mi=0):
    """Pacific local time -> UTC datetime (November: UTC-8)."""
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc) + timedelta(hours=8)


class FakePlugs:
    def __init__(self):
        self.states = {PLUG["device"]: {"on": True}}
        self.calls: list[tuple[str, bool, str]] = []
        self.fail_on = 0          # how many "on" calls fail before one works
        self.fail_off = False

    async def set_power(self, cfg, st, on, confirm, note=None):
        if not on and self.fail_off:
            raise GoveeError("off refused")
        if on and self.fail_on > 0:
            self.fail_on -= 1
            raise GoveeError("Govee down")
        self.calls.append((cfg.id, on, note))
        self.states[PLUG["device"]] = {"on": on}
        return {"ok": True, "on": on}


class Env:
    def __init__(self, tmp_path, n=1):
        self.store = PrinterStore(tmp_path / "d.db")
        self.store.set_app_setting("dashboard", {"tz": TZ})
        self.cfgs = [self.store.add(f"U1-{i}", "snapmaker.u1", f"10.0.0.{i}", settings={
            "plug": {**PLUG, "device": f"D{i}"},
            "autocycle": {"enabled": True, "time": "06:00", "idle_min": 30}}) for i in range(n)]
        self.plugs = FakePlugs()
        self.plugs.states = {f"D{i}": {"on": True} for i in range(n)}
        self.status: dict[str, PrinterStatus] = {}
        self.clock = la(2026, 11, 5, 3)
        self.changes = 0
        self.ac = AutoCycle(self.store, self.plugs, self.status.get, self._changed, now=lambda: self.clock)
        self.ac._configs = lambda: self.cfgs

    def _changed(self):
        self.changes += 1

    def st(self, i=0, state=JobState.FINISHED, nozzle=25.0, conn=ConnState.ONLINE, busy=None):
        s = PrinterStatus(printer_id=self.cfgs[i].id, connection=conn, state=state,
                          temps=[Temperature(key="nozzle", label="Nozzle", current=nozzle)], busy_with=busy)
        self.status[s.printer_id] = s
        self.ac.observe(s)
        return s

    def plan(self, i=0):
        return self.ac.plan(self.cfgs[i])

    def at(self, t):
        self.clock = t


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(ac, "OFF_COOLDOWN_S", 0)
    monkeypatch.setattr(ac, "ON_RETRY_S", (0, 0, 0))
    real = asyncio.sleep
    monkeypatch.setattr(ac.asyncio, "sleep", lambda s: real(0))


async def run_tick(e):
    await e.ac.tick()
    for _ in range(10):
        await asyncio.sleep(0)


def printed_then_finished(e, i=0, at=None):
    if at:
        e.at(at)
    e.st(i, JobState.PRINTING)
    e.st(i, JobState.FINISHED)


def test_nothing_printed_since_last_cycle_means_nothing_to_do(tmp_path):
    e = Env(tmp_path)
    e.ac._put(e.cfgs[0].id, last_cycle=ac._iso(la(2026, 11, 4, 6)))
    e.st(0, JobState.IDLE)
    e.at(la(2026, 11, 5, 9))
    assert e.plan()["phase"] == "none"


def test_daily_time_then_countdown_then_cycle(tmp_path):
    e = Env(tmp_path)
    e.ac._put(e.cfgs[0].id, last_cycle=ac._iso(la(2026, 11, 4, 6)))
    printed_then_finished(e, at=la(2026, 11, 5, 3))            # finished at 3:00 AM
    p = e.plan()
    assert p["phase"] == "scheduled" and ac._dt(p["at"]) == la(2026, 11, 5, 6)
    e.at(la(2026, 11, 5, 6))                                    # 6:00: due; it has been idle 3 h -> fires now
    p = e.plan()
    assert p["phase"] == "countdown" and ac._dt(p["at"]) == la(2026, 11, 5, 6)
    asyncio.run(run_tick(e))
    assert [(on, n) for _, on, n in e.plugs.calls] == [(False, "automatic power cycle"), (True, "automatic power cycle")]
    rec = e.ac._get(e.cfgs[0].id)
    assert ac._dt(rec["last_cycle"]) == la(2026, 11, 5, 6) and not rec.get("in_progress")
    assert e.plan()["phase"] == "none"


def test_countdown_runs_idle_minutes_after_finishing(tmp_path):
    e = Env(tmp_path)
    e.ac._put(e.cfgs[0].id, last_cycle=ac._iso(la(2026, 11, 4, 6)))
    e.at(la(2026, 11, 5, 7))
    printed_then_finished(e)                                     # finished 7:00, after the daily time
    p = e.plan()
    assert p["phase"] == "countdown" and ac._dt(p["at"]) == la(2026, 11, 5, 7, 30)
    e.ac.restart_countdown(e.cfgs[0].id)
    e.at(la(2026, 11, 5, 7, 10))
    e.ac.restart_countdown(e.cfgs[0].id)
    assert ac._dt(e.plan()["at"]) == la(2026, 11, 5, 7, 40)    # back to the full 30 minutes


def test_power_cycle_less_than_12h_before_skips_the_day(tmp_path):
    e = Env(tmp_path)
    e.ac._put(e.cfgs[0].id, last_cycle=ac._iso(la(2026, 11, 5, 3)))   # someone cycled it at 3 AM
    printed_then_finished(e, at=la(2026, 11, 5, 4))
    e.at(la(2026, 11, 5, 6, 30))
    p = e.plan()
    assert p["phase"] == "scheduled" and ac._dt(p["at"]) == la(2026, 11, 6, 3)   # min(tomorrow 6:00, 24 h overdue)


def test_long_print_over_the_daily_time_cycles_after_it_finishes(tmp_path):
    e = Env(tmp_path)
    e.ac._put(e.cfgs[0].id, last_cycle=ac._iso(la(2026, 11, 4, 6)))
    e.st(0, JobState.PRINTING)
    e.at(la(2026, 11, 5, 6))
    assert e.plan() == {"phase": "waiting", "why": "printing"}
    e.at(la(2026, 11, 5, 14))
    e.st(0, JobState.FINISHED)                                   # done at 2 PM (32 h since the last cycle)
    assert ac._dt(e.plan()["at"]) == la(2026, 11, 5, 14, 30)


@pytest.mark.parametrize("kw,why", [
    ({"state": JobState.FAILED}, "failed - needs a look"),
    ({"state": JobState.CANCELLED}, "cancelled - needs a look"),
    ({"state": JobState.PAUSED}, "paused"),
    ({"nozzle": 120.0}, "nozzle still hot"),
    ({"busy": "firmware update"}, "firmware update"),
    ({"busy": "AMS drying"}, "AMS drying"),
    ({"conn": ConnState.OFFLINE}, "printer not online"),
])
def test_never_cycles_when_someone_should_look_or_its_busy(tmp_path, kw, why):
    e = Env(tmp_path)
    e.ac._put(e.cfgs[0].id, last_cycle=ac._iso(la(2026, 11, 4, 6)))
    e.st(0, JobState.PRINTING)
    e.st(0, **kw)
    e.at(la(2026, 11, 5, 9))
    assert e.plan() == {"phase": "waiting", "why": why}


def test_plug_already_off_is_left_off(tmp_path):
    e = Env(tmp_path)
    e.ac._put(e.cfgs[0].id, last_cycle=ac._iso(la(2026, 11, 4, 6)))
    printed_then_finished(e)
    e.plugs.states["D0"] = {"on": False}
    e.at(la(2026, 11, 5, 9))
    assert e.plan() == {"phase": "waiting", "why": "plug is off"}


def test_pause_skips_todays_cycle_then_clears_itself(tmp_path):
    e = Env(tmp_path)
    e.ac._put(e.cfgs[0].id, last_cycle=ac._iso(la(2026, 11, 4, 6)))
    e.at(la(2026, 11, 5, 6, 5))
    printed_then_finished(e)
    e.ac.pause(e.cfgs[0])
    p = e.plan()
    assert p["phase"] == "paused" and ac._dt(p["until"]) == la(2026, 11, 6, 6)
    e.at(la(2026, 11, 6, 6))
    assert e.plan()["phase"] == "countdown"                      # tomorrow's runs normally
    e.ac.pause(e.cfgs[0])
    e.ac.resume(e.cfgs[0].id)
    assert e.plan()["phase"] == "countdown"


def test_pause_before_the_daily_time_skips_that_one(tmp_path):
    e = Env(tmp_path)
    e.ac._put(e.cfgs[0].id, last_cycle=ac._iso(la(2026, 11, 4, 6)))
    e.at(la(2026, 11, 5, 5))
    printed_then_finished(e)
    e.ac.pause(e.cfgs[0])
    assert ac._dt(e.plan()["until"]) == la(2026, 11, 6, 6)


def test_restart_mid_cycle_turns_the_plug_back_on(tmp_path):
    e = Env(tmp_path)
    e.ac._put(e.cfgs[0].id, in_progress=ac._iso(e.clock))
    e.plugs.states["D0"] = {"on": False}
    asyncio.run(e.ac.finish_interrupted())
    assert e.plugs.calls == [(e.cfgs[0].id, True, "automatic power cycle")]
    assert not e.ac._get(e.cfgs[0].id).get("in_progress")


def test_on_retries_then_reports_failure(tmp_path):
    e = Env(tmp_path)
    e.ac._put(e.cfgs[0].id, last_cycle=ac._iso(la(2026, 11, 4, 6)))
    printed_then_finished(e)
    e.at(la(2026, 11, 5, 9))
    e.plugs.fail_on = 2                                          # Govee hiccups twice, third try works
    assert asyncio.run(e.ac.cycle(e.cfgs[0])) is True
    (tmp_path / "b").mkdir()
    e2 = Env(tmp_path / "b")
    e2.ac._put(e2.cfgs[0].id, last_cycle=ac._iso(la(2026, 11, 4, 6)))
    printed_then_finished(e2)
    e2.at(la(2026, 11, 5, 9))
    e2.plugs.fail_on = 99
    assert asyncio.run(e2.ac.cycle(e2.cfgs[0])) is False
    rec = e2.ac._get(e2.cfgs[0].id)
    assert not rec.get("in_progress") and "did not turn back on" in rec["note"]
    assert e2.plan()["phase"] == "failed"


def test_manual_dashboard_cycle_counts(tmp_path):
    e = Env(tmp_path)
    e.ac._put(e.cfgs[0].id, last_cycle=ac._iso(la(2026, 11, 4, 6)))
    printed_then_finished(e)
    e.ac.manual_cycle(e.cfgs[0].id)                              # 3 AM, from the dashboard
    e.at(la(2026, 11, 5, 6))
    assert e.plan()["phase"] == "none"                           # no print since


def test_printers_are_staggered(tmp_path):
    e = Env(tmp_path, n=2)
    for i in range(2):
        e.ac._put(e.cfgs[i].id, last_cycle=ac._iso(la(2026, 11, 4, 6)))
        printed_then_finished(e, i)
    e.at(la(2026, 11, 5, 9))

    async def go():
        await e.ac.tick()
        await asyncio.sleep(0); await asyncio.sleep(0); await asyncio.sleep(0)
        first = {c[0] for c in e.plugs.calls}
        await e.ac.tick()                                        # same minute: the second waits
        for _ in range(5):
            await asyncio.sleep(0)
        same_minute = {c[0] for c in e.plugs.calls}
        e.at(la(2026, 11, 5, 9, 2))
        await e.ac.tick()
        for _ in range(5):
            await asyncio.sleep(0)
        return first, same_minute, {c[0] for c in e.plugs.calls}
    first, same_minute, later = asyncio.run(go())
    assert len(first) == 1 and same_minute == first and len(later) == 2


def test_u1_boot_time_counts_as_a_reboot(tmp_path):
    """Rebooted at the printer at 3 AM (U1 reports uptime): no 6 AM cycle; and a print before that boot doesn't count."""
    e = Env(tmp_path)
    e.ac._put(e.cfgs[0].id, last_cycle=ac._iso(la(2026, 11, 4, 6)))
    e.at(la(2026, 11, 5, 1))
    printed_then_finished(e)                                     # printed at 1 AM ...
    e.status[e.cfgs[0].id].booted_at = la(2026, 11, 5, 3)        # ... then someone rebooted it at 3 AM
    e.at(la(2026, 11, 5, 6))
    assert e.plan()["phase"] == "none"                           # no print since that reboot
    e.at(la(2026, 11, 5, 8))
    printed_then_finished(e)                                     # prints again after the reboot
    e.status[e.cfgs[0].id].booted_at = la(2026, 11, 5, 3)
    p = e.plan()
    assert p["phase"] == "scheduled" and ac._dt(p["at"]) == la(2026, 11, 6, 3)   # 3 AM reboot < 12 h before 6 AM
    e.status[e.cfgs[0].id].booted_at = la(2026, 11, 4, 2)        # boot older than the dashboard's cycle: ignored
    assert e.ac.last_reboot(e.cfgs[0].id) == la(2026, 11, 4, 6)


def test_off_by_default_and_needs_a_plug(tmp_path):
    e = Env(tmp_path)
    cfg = e.store.add("Other", "snapmaker.u1", "10.0.0.99")
    assert e.ac.plan(cfg) == {"phase": "off"}
