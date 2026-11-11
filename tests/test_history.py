import sqlite3
import threading
from datetime import datetime, timedelta, timezone

import pytest

from app import history as H
from app.models import ConnState, FilamentSlot, FilamentUnit, Job, JobState, PrinterStatus
from app.store import PrinterConfig


class Clock:
    def __init__(self):
        self.t = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.t

    def tick(self, s):
        self.t += timedelta(seconds=s)


@pytest.fixture
def env(monkeypatch):
    clk = Clock()
    monkeypatch.setattr(H, "_now", clk)
    h = H.History(sqlite3.connect(":memory:", check_same_thread=False), threading.Lock())
    cfg = PrinterConfig(id="p1", name="P2", driver="bambu.p1p", host="x", serial="01S099000000014")
    return h, cfg, clk


def st(state, name="Benchy", conn=ConnState.ONLINE, elapsed=None, color="#FF0000"):
    slot = FilamentSlot(index=0, source="ams", present=True, active=state == JobState.PRINTING, material="PLA",
                        color_hex=color)
    return PrinterStatus(printer_id="p1", connection=conn, state=state,
                         job=Job(name=name, file_name=f"{name}.3mf", elapsed_s=elapsed) if name else Job(),
                         filament=[FilamentUnit(id="ams0", kind="AMS", slots=[slot])])


def run(h, cfg, clk, states, step=10):
    events = []
    for s in states:
        events += h.observe(cfg, s)
        clk.tick(step)
    return events


def test_job_lifecycle_counts_print_time_not_pause_or_offline(env):
    h, cfg, clk = env
    seq = [st(JobState.PREPARING)] + [st(JobState.PRINTING)] * 6 + [st(JobState.PAUSED)] * 5 \
        + [st(JobState.PRINTING, conn=ConnState.OFFLINE)] * 3 + [st(JobState.PRINTING)] * 3 + [st(JobState.FINISHED)] * 4
    ev = run(h, cfg, clk, seq)
    assert [e["type"] for e in ev] == ["job_started", "job_ended"] and ev[1]["result"] == "finished"
    (job,) = h.jobs()
    # running updates every 10 s: 7 preparing/printing intervals before pause, 1 interval resuming after the
    # pause, then offline gap (40 s, < MAX_GAP) counts once printing resumes, 2 more intervals
    assert job["result"] == "finished" and job["filament"] == ["PLA #FF0000"]
    assert 90 <= job["print_seconds"] <= 140
    u = h.usage(H.usage_key(cfg))
    assert u["tracked_jobs"] == 1 and u["finished"] == 1 and u["success_rate"] == 100


def test_finished_state_lingers_no_duplicate_and_reprint_is_new_job(env):
    h, cfg, clk = env
    run(h, cfg, clk, [st(JobState.PRINTING)] * 3 + [st(JobState.FINISHED)] * 10 + [st(JobState.PREPARING), st(JobState.PRINTING),
                                                                                    st(JobState.FAILED)])
    assert [j["result"] for j in h.jobs()] == ["failed", "finished"]


def test_new_job_while_one_open_closes_old_as_unknown(env):
    h, cfg, clk = env
    run(h, cfg, clk, [st(JobState.PRINTING, "A")] * 3 + [st(JobState.PRINTING, "B")] * 3)
    jobs = h.jobs()
    assert [(j["job"], j["result"]) for j in jobs] == [("B", "running"), ("A", "unknown")]


def test_restart_mid_print_resumes_same_row(env, monkeypatch):
    h, cfg, clk = env
    run(h, cfg, clk, [st(JobState.PRINTING)] * 10, step=10)       # flushes after 60 s
    h2 = H.History(h.db, h.lock)                                    # dashboard restarted
    ev = run(h2, cfg, clk, [st(JobState.PRINTING)] * 3 + [st(JobState.FINISHED)])
    assert [e["type"] for e in ev] == ["job_ended"]                 # no second "started"
    (job,) = h2.jobs()
    assert job["result"] == "finished" and job["print_seconds"] >= 80


def test_printer_elapsed_wins_when_reported(env):
    h, cfg, clk = env
    run(h, cfg, clk, [st(JobState.PRINTING, elapsed=3600)] * 2 + [st(JobState.FINISHED, elapsed=3700)])
    assert h.jobs()[0]["print_seconds"] == 3700


def test_starting_value_from_printer_then_audit_then_reset(env):
    h, cfg, clk = env
    key = H.usage_key(cfg)
    run(h, cfg, clk, [st(JobState.PRINTING)] * 7 + [st(JobState.FINISHED)])      # 60 s tracked first
    h.report_lifetime(key, 100 * 3600, 50)                          # first report: becomes the starting value
    u = h.usage(key)
    assert u["baseline_source"] == "printer" and abs(u["total_seconds"] - 100 * 3600) < 1 and u["total_jobs"] == 50
    assert u["audit"]["flagged"] is False
    h.report_lifetime(key, 98 * 3600, 50)                           # printer now says 98 h, we say 100 h
    h.report_lifetime(key, 98.5 * 3600, 50)
    u = h.usage(key)
    assert u["audit"]["flagged"] is False                           # 1.5 h off but within 2 % of 100 h
    h.report_lifetime(key, 90 * 3600, 50)                           # 10 h short - but that's a drop: reset rule
    u = h.usage(key)
    assert abs(u["total_seconds"] - 100 * 3600) < 1                 # our total never changes on a reset
    assert u["audit"]["last_reset_at"] and any(e["field"] == "printer-reset" for e in u["edits"])
    h.report_lifetime(key, 0.5 * 3600, 0)                           # factory reset to ~0
    u = h.usage(key)
    assert abs(u["total_seconds"] - 100 * 3600) < 1 and u["audit"]["flagged"] is False   # offset keeps audit sane


def test_audit_flags_drift_and_accept_reported(env):
    h, cfg, clk = env
    key = H.usage_key(cfg)
    h.set_baseline(key, 100 * 3600, 40)
    h.report_lifetime(key, 112 * 3600, 40)                          # manual start set: no auto-seed
    u = h.usage(key)
    assert u["baseline_source"] == "manual" and u["audit"]["flagged"] and round(u["audit"]["diff_seconds"]) == 12 * 3600
    h.accept_reported(key)
    u = h.usage(key)
    assert abs(u["total_seconds"] - 112 * 3600) < 1 and not u["audit"]["flagged"] and u["baseline_source"] == "printer"
    assert u["edits"][0]["field"] == "audit-accepted"


def test_manual_edit_logged_and_link_previous_history(env):
    h, cfg, clk = env
    old = "elegoo:OLD-MAINBOARD"
    h.set_baseline(old, 50 * 3600, 20)
    new_cfg = PrinterConfig(id="c1", name="CC", driver="elegoo.cc1", host="x", serial="NEW-MAINBOARD")
    run(h, new_cfg, clk, [st(JobState.PRINTING)] * 4 + [st(JobState.FINISHED)])
    assert [o["key"] for o in h.orphan_keys({H.usage_key(new_cfg)})] == [old]
    h.rekey(old, H.usage_key(new_cfg))
    u = h.usage(H.usage_key(new_cfg))
    assert u["baseline_seconds"] == 50 * 3600 and u["total_jobs"] == 21 and u["baseline_source"] == "manual"
    assert any(e["field"] == "linked" for e in u["edits"]) and h.orphan_keys({H.usage_key(new_cfg)}) == []


def test_owner_example_5000h_factory_reset_keep_adding_and_audit(env):
    """5,000 h on the printer -> factory reset shows 0 -> we keep adding to 5,000 and audit against that."""
    h, cfg, clk = env
    key = H.usage_key(cfg)
    h.report_lifetime(key, 5000 * 3600, 3000)                       # first sighting: start = 5,000 h
    assert abs(h.usage(key)["total_seconds"] - 5000 * 3600) < 1
    h.report_lifetime(key, 0, 0)                                    # factory reset
    u = h.usage(key)
    assert abs(u["total_seconds"] - 5000 * 3600) < 1 and u["audit"]["last_reset_at"]
    run(h, cfg, clk, [st(JobState.PRINTING, elapsed=0)] + [st(JobState.PRINTING)] + [st(JobState.FINISHED, elapsed=2 * 3600)])
    h.report_lifetime(key, 2 * 3600, 1)                             # printer counts from 0 again: 2 h
    u = h.usage(key)
    assert abs(u["total_seconds"] - 5002 * 3600) < 1                # we added: 5,002 h
    a = u["audit"]
    assert a["printer_counter_seconds"] == 2 * 3600 and abs(a["printer_seconds"] - 5002 * 3600) < 1
    assert not a["flagged"]                                         # audit = printer 2 h + 5,000 h lost in the reset
    h.report_lifetime(key, 1 * 3600, 1)                             # printer later under-counts by 1 h (dropped < 10 min? no: 1 h)
    assert h.usage(key)["audit"]["last_reset_at"]                   # an hour backwards is treated as another reset


def test_bambu_counter_counts_finished_jobs_only(env):
    """Bambu adds a job's time to its counter only when it finishes OK: a failed print must not flag the audit,
    but it still counts in the dashboard's total hours."""
    h, cfg, clk = env
    key = H.usage_key(cfg)
    h.report_lifetime(key, 11978 * 3600.0, None, basis="finished")
    u = h.usage(key)
    assert u["baseline_source"] == "printer" and u["total_seconds"] == 11978 * 3600 and not u["audit"]["flagged"]
    # a 3 h print that fails: printer counter doesn't move
    run(h, cfg, clk, [st(JobState.PRINTING, "Fail", elapsed=3 * 3600)] * 2 + [st(JobState.FAILED, "Fail", elapsed=3 * 3600)])
    h.report_lifetime(key, 11978 * 3600.0, None, basis="finished")
    u = h.usage(key)
    assert u["total_seconds"] >= (11978 + 3) * 3600                  # dashboard total includes the failed print
    assert u["audit"]["basis"] == "finished" and not u["audit"]["flagged"]
    # a 2 h print that finishes: printer counter grows by 2 h
    run(h, cfg, clk, [st(JobState.PRINTING, "Ok", elapsed=2 * 3600)] * 2 + [st(JobState.FINISHED, "Ok", elapsed=2 * 3600)])
    h.report_lifetime(key, (11978 + 2) * 3600.0, None, basis="finished")
    assert not h.usage(key)["audit"]["flagged"]
    # judged on all jobs, the same counter is 3 h behind the dashboard (the failed print)
    h.report_lifetime(key, (11978 + 2) * 3600.0, None, basis="all")
    assert round(h.usage(key)["audit"]["diff_seconds"] / 3600) == -3


def _item(ext, start, hours, result="finished"):
    return {"external_id": ext, "job_name": f"job {ext}", "file_name": f"/local/{ext}.gcode",
            "started_at": start, "ended_at": start + timedelta(hours=hours), "result": result,
            "print_seconds": hours * 3600.0}


def test_import_printer_history_dedupes_and_matches_tracked_job(env):
    h, cfg, clk = env
    key = H.usage_key(cfg)
    # the dashboard tracked a print itself (before task ids were recorded)
    tracked_start = clk()
    run(h, cfg, clk, [st(JobState.PRINTING, "Dragons")] * 3 + [st(JobState.FINISHED, "Dragons")])
    old = clk() - timedelta(days=2)
    items = [_item("t-tracked", tracked_start + timedelta(minutes=3), 9),     # same print as the tracked one
             _item("t-old1", old, 5), _item("t-old2", old - timedelta(days=1), 2, "cancelled")]
    assert h.import_printer_jobs(cfg, items) == 2
    assert h.import_printer_jobs(cfg, items) == 0                             # second read: nothing new
    u = h.usage(key)
    assert u["tracked_jobs"] == 3 and u["finished"] == 2 and u["failed"] == 0
    assert round(u["tracked_seconds"] / 3600) == 7                            # 5 + 2 imported + ~0 tracked
    jobs = h.jobs([key])
    assert [j["job"] for j in jobs] == ["Dragons", "job t-old1", "job t-old2"]   # newest first by start time
    assert [j["source"] for j in jobs] == ["dashboard", "printer", "printer"]
    page2 = h.jobs([key], limit=2, before_id=jobs[1]["id"])
    assert [j["job"] for j in page2] == ["job t-old2"]


def test_average_job_time_ignores_the_running_job(env):
    h, cfg, clk = env
    key = H.usage_key(cfg)
    assert h.usage(key)["avg_job_seconds"] is None
    run(h, cfg, clk, [st(JobState.PRINTING, name="A")] * 4 + [st(JobState.FINISHED, name="A")]
        + [st(JobState.PRINTING, name="B")] * 10 + [st(JobState.FAILED, name="B")])
    done = [j["print_seconds"] for j in h.jobs()]
    u = h.usage(key)
    assert u["avg_jobs"] == 2 and u["avg_job_seconds"] == pytest.approx(sum(done) / 2)
    run(h, cfg, clk, [st(JobState.PRINTING, name="C")] * 3)          # still printing: not in the average
    u = h.usage(key)
    assert u["tracked_jobs"] == 3 and u["avg_jobs"] == 2 and u["avg_job_seconds"] == pytest.approx(sum(done) / 2)
