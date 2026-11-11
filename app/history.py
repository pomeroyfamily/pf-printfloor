"""Job history + usage totals (print hours, job counts) per physical printer.

Usage is keyed by the printer's hardware identity (Bambu/U1 serial, Centauri MainboardID), so totals
survive removing/re-adding a printer and printer factory resets. Printers without a serial use their
dashboard entry until one is learned.

Total hours = starting value ("before tracking") + hours tracked by the dashboard. The starting value is
taken from the printer's own lifetime counter when it reports one (minus anything already tracked, so
nothing is counted twice); otherwise the user types it in. It is always editable, and every edit is logged.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from .models import ConnState, JobState, PrinterStatus
from .store import PrinterConfig

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    usage_key     TEXT NOT NULL,
    printer_id    TEXT,
    printer_name  TEXT,
    job_name      TEXT,
    file_name     TEXT,
    started_at    TEXT NOT NULL,
    ended_at      TEXT,                     -- NULL while the job is running
    result        TEXT,                     -- finished | failed | cancelled | unknown
    print_seconds REAL NOT NULL DEFAULT 0,
    filament      TEXT NOT NULL DEFAULT '[]'
    -- migrated: external_id (printer's own job id), source ('printer' = imported from the printer's history)
);
CREATE INDEX IF NOT EXISTS ix_jobs_key ON jobs(usage_key, started_at);
CREATE TABLE IF NOT EXISTS usage (
    key              TEXT PRIMARY KEY,      -- "<family>:<serial>" or "printer:<id>"
    baseline_seconds REAL NOT NULL DEFAULT 0,
    baseline_jobs    INTEGER NOT NULL DEFAULT 0,
    baseline_source  TEXT,                  -- printer | manual | NULL (not set)
    reported_seconds REAL,                  -- latest lifetime print time the printer reported
    reported_jobs    INTEGER,
    reported_at      TEXT,
    created_at       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS usage_edits (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    key   TEXT NOT NULL,
    at    TEXT NOT NULL,
    field TEXT NOT NULL,
    old   TEXT,
    new   TEXT
);
"""
ACTIVE = {JobState.PRINTING, JobState.PREPARING, JobState.PAUSED}
RUNNING = {JobState.PRINTING, JobState.PREPARING}          # paused time doesn't count as print time
END = {JobState.FINISHED: "finished", JobState.FAILED: "failed", JobState.ERROR: "failed",
       JobState.CANCELLED: "cancelled"}
RESET_DROP_S = 600       # printer counter dropping by more than 10 min = it was reset
AUDIT_MIN_S = 3600       # flag when dashboard and printer disagree by more than 1 h ...
AUDIT_PCT = 0.02         # ... and more than 2 %
AUDIT_JOBS = 2           # or by more than 2 jobs
MAX_GAP_S = 120          # longer gaps between updates (offline, dashboard down) aren't counted
FLUSH_S = 60


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(t: datetime) -> str:
    return t.isoformat(timespec="seconds")


def usage_key(cfg: PrinterConfig) -> str:
    return f"{cfg.driver.split('.')[0]}:{cfg.serial}" if cfg.serial else f"printer:{cfg.id}"


class _Open:
    def __init__(self, row_id: int, sig: str, seconds: float):
        self.row_id, self.sig, self.seconds = row_id, sig, seconds
        self.last: Optional[datetime] = None
        self.flushed = _now()
        self.filament: list[str] = []


class History:
    def __init__(self, db: sqlite3.Connection, lock: threading.Lock):
        self.db, self.lock = db, lock
        with self.lock:
            self.db.executescript(SCHEMA)
            cols = {r[1] for r in self.db.execute("PRAGMA table_info(usage)")}
            jcols = {r[1] for r in self.db.execute("PRAGMA table_info(jobs)")}
            for col in ("external_id", "source"):
                if col not in jcols:
                    self.db.execute(f"ALTER TABLE jobs ADD COLUMN {col} TEXT")
            for col, typ in (("reset_offset_seconds", "REAL NOT NULL DEFAULT 0"),
                             ("reset_offset_jobs", "INTEGER NOT NULL DEFAULT 0"), ("last_reset_at", "TEXT"),
                             ("counter_basis", "TEXT")):         # 'all' | 'finished' (Bambu counts finished jobs only)
                if col not in cols:
                    self.db.execute(f"ALTER TABLE usage ADD COLUMN {col} {typ}")
            self.db.commit()
        self.open: dict[str, _Open] = {}          # printer_id -> running job

    # ------------------------------------------------------------------ usage rows
    def _ensure_usage(self, key: str) -> None:
        with self.lock:
            self.db.execute("INSERT OR IGNORE INTO usage (key, created_at) VALUES (?, ?)", (key, _iso(_now())))
            self.db.commit()

    def _tracked(self, key: str) -> tuple[float, int, int, int]:
        """(seconds, jobs, finished, failed) tracked by the dashboard, incl. the running job."""
        with self.lock:
            r = self.db.execute(
                "SELECT COALESCE(SUM(print_seconds),0), COUNT(*), SUM(result='finished'), SUM(result='failed') "
                "FROM jobs WHERE usage_key = ?", (key,)).fetchone()
        return float(r[0] or 0), int(r[1] or 0), int(r[2] or 0), int(r[3] or 0)

    def _tracked_basis(self, key: str, basis: Optional[str]) -> tuple[float, int]:
        """What the printer's counter should contain of the dashboard's jobs: all of them, or (Bambu) only
        successfully finished ones - Bambu adds a job's time only when it completes OK."""
        if basis != "finished":
            return self._tracked(key)[:2]
        with self.lock:
            r = self.db.execute("SELECT COALESCE(SUM(print_seconds),0), COUNT(*) FROM jobs "
                                "WHERE usage_key = ? AND result = 'finished'", (key,)).fetchone()
        return float(r[0] or 0), int(r[1] or 0)

    def report_lifetime(self, key: str, seconds: Optional[float], jobs: Optional[int], basis: str = "all") -> None:
        """Printer-reported lifetime counters. The first report becomes the starting value (minus what the
        dashboard already tracked) unless a starting value was already set. basis 'finished' = the counter
        only includes successfully finished jobs (Bambu)."""
        if seconds is None:
            return
        self._ensure_usage(key)
        tracked_s, tracked_jobs = self._tracked_basis(key, basis)
        with self.lock:
            self.db.execute("UPDATE usage SET counter_basis = ? WHERE key = ?", (basis, key))
            row = self.db.execute("SELECT baseline_source, reported_seconds, reported_jobs FROM usage WHERE key = ?",
                                  (key,)).fetchone()
            prev_s, prev_j = row[1], row[2]
            if prev_s is not None and seconds < prev_s - RESET_DROP_S:
                # Printer's counter went backwards (factory reset): keep our totals, offset future comparisons.
                lost_s, lost_j = prev_s - seconds, max(0, (prev_j or 0) - (jobs or 0))
                self.db.execute("UPDATE usage SET reset_offset_seconds = reset_offset_seconds + ?, "
                                "reset_offset_jobs = reset_offset_jobs + ?, last_reset_at = ? WHERE key = ?",
                                (lost_s, lost_j, _iso(_now()), key))
                self.db.execute("INSERT INTO usage_edits (key, at, field, old, new) VALUES (?, ?, 'printer-reset', ?, ?)",
                                (key, _iso(_now()), json.dumps({"seconds": prev_s, "jobs": prev_j}),
                                 json.dumps({"seconds": seconds, "jobs": jobs})))
            self.db.execute("UPDATE usage SET reported_seconds = ?, reported_jobs = ?, reported_at = ? WHERE key = ?",
                            (seconds, jobs, _iso(_now()), key))
            if row and row[0] is None:
                base_s = max(0.0, seconds - tracked_s)
                base_j = max(0, (jobs or 0) - tracked_jobs) if jobs is not None else 0
                self.db.execute("UPDATE usage SET baseline_seconds = ?, baseline_jobs = ?, baseline_source = 'printer' "
                                "WHERE key = ?", (base_s, base_j, key))
                self.db.execute("INSERT INTO usage_edits (key, at, field, old, new) VALUES (?, ?, 'baseline', NULL, ?)",
                                (key, _iso(_now()), json.dumps({"seconds": base_s, "jobs": base_j, "source": "printer"})))
            self.db.commit()

    def set_baseline(self, key: str, seconds: Optional[float], jobs: Optional[int]) -> None:
        self._ensure_usage(key)
        with self.lock:
            old = self.db.execute("SELECT baseline_seconds, baseline_jobs, baseline_source FROM usage WHERE key = ?",
                                  (key,)).fetchone()
            new_s = old[0] if seconds is None else max(0.0, float(seconds))
            new_j = old[1] if jobs is None else max(0, int(jobs))
            self.db.execute("UPDATE usage SET baseline_seconds = ?, baseline_jobs = ?, baseline_source = 'manual' "
                            "WHERE key = ?", (new_s, new_j, key))
            self.db.execute("INSERT INTO usage_edits (key, at, field, old, new) VALUES (?, ?, 'baseline', ?, ?)",
                            (key, _iso(_now()), json.dumps({"seconds": old[0], "jobs": old[1], "source": old[2]}),
                             json.dumps({"seconds": new_s, "jobs": new_j, "source": "manual"})))
            self.db.commit()

    def accept_reported(self, key: str) -> None:
        """Audit fix: make the total match the printer's own counter (starting value = reported - tracked)."""
        with self.lock:
            r = self.db.execute("SELECT reported_seconds + reset_offset_seconds, reported_jobs + reset_offset_jobs, "
                                "reported_seconds, counter_basis FROM usage WHERE key = ?", (key,)).fetchone()
        if not r or r[2] is None:
            raise ValueError("this printer doesn't report its lifetime hours")
        tracked_s, tracked_jobs = self._tracked_basis(key, r[3])
        self.set_baseline(key, max(0.0, r[0] - tracked_s), max(0, (r[1] or 0) - tracked_jobs) if r[1] is not None else None)
        with self.lock:
            self.db.execute("UPDATE usage SET baseline_source = 'printer' WHERE key = ?", (key,))
            self.db.execute("UPDATE usage_edits SET field = 'audit-accepted' WHERE id = (SELECT MAX(id) FROM usage_edits WHERE key = ?)", (key,))
            self.db.commit()

    def rekey(self, old: str, new: str) -> None:
        """Move history to a new identity (serial learned, or 'link to previous history'). Baselines add up."""
        if old == new:
            return
        self._ensure_usage(old)
        self._ensure_usage(new)
        with self.lock:
            o = self.db.execute("SELECT baseline_seconds, baseline_jobs, baseline_source, reported_seconds, "
                                "reported_jobs, reported_at FROM usage WHERE key = ?", (old,)).fetchone()
            n = self.db.execute("SELECT baseline_source FROM usage WHERE key = ?", (new,)).fetchone()
            self.db.execute("UPDATE jobs SET usage_key = ? WHERE usage_key = ?", (new, old))
            src = n[0] or o[2]
            self.db.execute("UPDATE usage SET baseline_seconds = baseline_seconds + ?, baseline_jobs = baseline_jobs + ?, "
                            "baseline_source = ?, reported_seconds = COALESCE(reported_seconds, ?), "
                            "reported_jobs = COALESCE(reported_jobs, ?), reported_at = COALESCE(reported_at, ?) "
                            "WHERE key = ?", (o[0], o[1], src, o[3], o[4], o[5], new))
            self.db.execute("UPDATE usage_edits SET key = ? WHERE key = ?", (new, old))
            self.db.execute("INSERT INTO usage_edits (key, at, field, old, new) VALUES (?, ?, 'linked', ?, ?)",
                            (new, _iso(_now()), old, new))
            self.db.execute("DELETE FROM usage WHERE key = ?", (old,))
            self.db.commit()

    # ------------------------------------------------------------------ job tracking
    def observe(self, cfg: PrinterConfig, st: PrinterStatus) -> list[dict[str, Any]]:
        """Feed every status update. Returns events: job_started / job_ended (for notifications)."""
        if st.connection != ConnState.ONLINE:
            return []                                          # keep the job open; gap isn't counted
        key, now, events = usage_key(cfg), _now(), []
        self._ensure_usage(key)
        job = st.job
        sig = (job.file_name or job.name or "").strip()
        cur = self.open.get(cfg.id)

        if st.state in ACTIVE and sig:
            if cur and cur.sig != sig:                         # a different job started: close the old one
                events.append(self._close(cfg, cur, "unknown", None))
                cur = None
            if not cur:
                cur = self._resume(cfg, key, sig) or self._start(cfg, key, sig, job, now)
                if cur.last is None and not getattr(cur, "resumed", False):
                    events.append({"type": "job_started", "printer_id": cfg.id, "printer": cfg.name,
                                   "job": job.name or sig})
                self.open[cfg.id] = cur
            if cur.last is not None and st.state in RUNNING and not st.job.transferring:   # download time is not print time
                cur.seconds += min(max((now - cur.last).total_seconds(), 0), MAX_GAP_S)
            cur.last = now
            for u in st.filament:
                for s in u.slots:
                    if s.active and s.present:
                        tag = " ".join(x for x in (s.material, s.color_hex) if x)
                        if tag and tag not in cur.filament:
                            cur.filament.append(tag)
            if (now - cur.flushed).total_seconds() >= FLUSH_S:
                self._flush(cur)
        elif cur:
            result = END.get(st.state, "unknown")              # idle/unknown without an end state
            events.append(self._close(cfg, cur, result, job.elapsed_s if sig == cur.sig else None))
        return [e for e in events if e]

    def _start(self, cfg, key, sig, job, now) -> _Open:
        started = now - timedelta(seconds=job.elapsed_s) if job.elapsed_s else now
        with self.lock:
            cur = self.db.execute(
                "INSERT INTO jobs (usage_key, printer_id, printer_name, job_name, file_name, started_at, print_seconds, "
                "external_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (key, cfg.id, cfg.name, job.name, job.file_name, _iso(started), float(job.elapsed_s or 0), job.task_id))
            self.db.commit()
        return _Open(cur.lastrowid, sig, float(job.elapsed_s or 0))

    def _resume(self, cfg, key, sig) -> Optional[_Open]:
        """Dashboard restarted mid-print: continue the open row for the same job."""
        with self.lock:
            r = self.db.execute("SELECT id, print_seconds, filament FROM jobs WHERE usage_key = ? AND ended_at IS NULL "
                                "AND COALESCE(file_name, job_name) = ? ORDER BY id DESC LIMIT 1", (key, sig)).fetchone()
        if not r:
            return None
        o = _Open(r[0], sig, float(r[1]))
        o.filament = json.loads(r[2] or "[]")
        o.resumed = True                                       # type: ignore[attr-defined]
        return o

    def _flush(self, cur: _Open) -> None:
        with self.lock:
            self.db.execute("UPDATE jobs SET print_seconds = ?, filament = ? WHERE id = ?",
                            (cur.seconds, json.dumps(cur.filament), cur.row_id))
            self.db.commit()
        cur.flushed = _now()

    def _close(self, cfg, cur: _Open, result: str, printer_elapsed: Optional[int]) -> dict[str, Any]:
        secs = max(cur.seconds, float(printer_elapsed or 0))   # printer's own elapsed is exact when reported
        with self.lock:
            self.db.execute("UPDATE jobs SET ended_at = ?, result = ?, print_seconds = ?, filament = ? WHERE id = ?",
                            (_iso(_now()), result, secs, json.dumps(cur.filament), cur.row_id))
            row = self.db.execute("SELECT job_name, file_name FROM jobs WHERE id = ?", (cur.row_id,)).fetchone()
            self.db.commit()
        self.open.pop(cfg.id, None)
        return {"type": "job_ended", "printer_id": cfg.id, "printer": cfg.name, "result": result,
                "job": row[0] or row[1], "print_seconds": secs}

    MATCH_WINDOW_S = 900     # an imported job starting within 15 min of a tracked one is the same print

    def import_printer_jobs(self, cfg: PrinterConfig, items: list[dict]) -> int:
        """Add finished jobs from the printer's own history (CC Cmd 321) that the dashboard doesn't have yet.
        Same print = same external id, or a tracked job (no id yet) that started within 15 min; that row just
        gets the id. Imported rows have source 'printer'. No events (no notifications for the past)."""
        key, n = usage_key(cfg), 0
        self._ensure_usage(key)
        with self.lock:
            for it in items:
                ext = it["external_id"]
                if self.db.execute("SELECT 1 FROM jobs WHERE usage_key = ? AND external_id = ?", (key, ext)).fetchone():
                    continue
                st, en = it["started_at"], it["ended_at"]
                lo = _iso(st - timedelta(seconds=self.MATCH_WINDOW_S))
                hi = _iso(st + timedelta(seconds=self.MATCH_WINDOW_S))
                same = self.db.execute("SELECT id FROM jobs WHERE usage_key = ? AND external_id IS NULL "
                                       "AND started_at BETWEEN ? AND ? ORDER BY id LIMIT 1", (key, lo, hi)).fetchone()
                if same:
                    self.db.execute("UPDATE jobs SET external_id = ? WHERE id = ?", (ext, same[0]))
                    continue
                self.db.execute(
                    "INSERT INTO jobs (usage_key, printer_id, printer_name, job_name, file_name, started_at, ended_at, "
                    "result, print_seconds, external_id, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'printer')",
                    (key, cfg.id, cfg.name, it.get("job_name"), it.get("file_name"), _iso(st), _iso(en),
                     it.get("result") or "unknown", float(it.get("print_seconds") or 0), ext))
                n += 1
            self.db.commit()
        return n

    def close_orphans(self, printer_ids: set[str]) -> None:
        """Forget in-memory jobs of removed printers (their rows stay as history)."""
        for pid in list(self.open):
            if pid not in printer_ids:
                self.open.pop(pid, None)

    # ------------------------------------------------------------------ queries
    def usage(self, key: str) -> dict[str, Any]:
        self._ensure_usage(key)
        tracked_s, tracked_jobs, finished, failed = self._tracked(key)
        with self.lock:
            u = self.db.execute("SELECT baseline_seconds, baseline_jobs, baseline_source, reported_seconds, reported_jobs, "
                                "reported_at, created_at, reset_offset_seconds, reset_offset_jobs, last_reset_at, counter_basis "
                                "FROM usage WHERE key = ?", (key,)).fetchone()
            first = self.db.execute("SELECT MIN(started_at) FROM jobs WHERE usage_key = ?", (key,)).fetchone()[0]
            done = self.db.execute("SELECT COALESCE(SUM(print_seconds),0), COUNT(*) FROM jobs "
                                   "WHERE usage_key = ? AND ended_at IS NOT NULL", (key,)).fetchone()
            edits = self.db.execute("SELECT at, field, old, new FROM usage_edits WHERE key = ? ORDER BY id DESC LIMIT 10",
                                    (key,)).fetchall()
        ended = finished + failed
        total_s, total_j = u[0] + tracked_s, u[1] + tracked_jobs
        audit = None
        if u[3] is not None:                                   # printer reports its own lifetime: compare
            printer_s = u[3] + u[7]                            # + what its counter lost in resets
            printer_j = (u[4] + u[8]) if u[4] is not None else None
            b_s, b_j = self._tracked_basis(key, u[10])
            dash_s, dash_j = u[0] + b_s, u[1] + b_j               # same basis as the printer's counter
            diff_s = printer_s - dash_s
            diff_j = (printer_j - dash_j) if printer_j is not None else None
            flagged = abs(diff_s) > max(AUDIT_MIN_S, AUDIT_PCT * max(printer_s, dash_s)) or \
                (diff_j is not None and abs(diff_j) > AUDIT_JOBS)
            audit = {"printer_seconds": printer_s, "printer_counter_seconds": u[3], "dashboard_seconds": dash_s,
                     "basis": u[10] or "all",
                     "diff_seconds": diff_s, "printer_jobs": printer_j, "dashboard_jobs": dash_j, "diff_jobs": diff_j,
                     "flagged": flagged, "checked_at": u[5], "reset_offset_seconds": u[7], "last_reset_at": u[9]}
        return {"audit": audit, "key": key, "baseline_seconds": u[0], "baseline_jobs": u[1], "baseline_source": u[2],
                "reported_seconds": u[3], "reported_jobs": u[4], "reported_at": u[5],
                "tracking_since": u[6], "first_job_at": first,
                "tracked_seconds": tracked_s, "tracked_jobs": tracked_jobs, "finished": finished, "failed": failed,
                "success_rate": round(100 * finished / ended) if ended else None,
                # average over ended jobs only, so a print still running doesn't drag it down
                "avg_job_seconds": (float(done[0]) / done[1]) if done[1] else None, "avg_jobs": int(done[1]),
                "total_seconds": total_s, "total_jobs": total_j,
                "edits": [{"at": e[0], "field": e[1], "old": e[2], "new": e[3]} for e in edits]}

    def jobs(self, keys: Optional[list[str]] = None, result: Optional[str] = None, limit: int = 50,
             before_id: Optional[int] = None) -> list[dict[str, Any]]:
        q, args = "SELECT id, usage_key, printer_id, printer_name, job_name, file_name, started_at, ended_at, result, " \
                  "print_seconds, filament, source FROM jobs WHERE 1=1", []
        if keys:
            q += f" AND usage_key IN ({','.join('?' * len(keys))})"
            args += keys
        if result:
            q += " AND result = ?" if result != "running" else " AND ended_at IS NULL"
            args += [result] if result != "running" else []
        with self.lock:
            if before_id:                                      # newest first by start time (imports are older)
                b = self.db.execute("SELECT started_at FROM jobs WHERE id = ?", (before_id,)).fetchone()
                if b:
                    q += " AND (started_at < ? OR (started_at = ? AND id < ?))"
                    args += [b[0], b[0], before_id]
            q += " ORDER BY started_at DESC, id DESC LIMIT ?"
            args.append(limit)
            rows = self.db.execute(q, args).fetchall()
        return [{"id": r[0], "usage_key": r[1], "printer_id": r[2], "printer_name": r[3], "job": r[4] or r[5],
                 "file_name": r[5], "started_at": r[6], "ended_at": r[7], "result": r[8] or "running",
                 "print_seconds": r[9], "filament": json.loads(r[10] or "[]"), "source": r[11] or "dashboard"}
                for r in rows]

    def orphan_keys(self, live_keys: set[str]) -> list[dict[str, Any]]:
        """Usage records not attached to any current printer (for 'link to previous history')."""
        with self.lock:
            rows = self.db.execute(
                "SELECT u.key, u.baseline_seconds + COALESCE(SUM(j.print_seconds), 0), COUNT(j.id), MAX(j.printer_name) "
                "FROM usage u LEFT JOIN jobs j ON j.usage_key = u.key GROUP BY u.key").fetchall()
        return [{"key": r[0], "total_seconds": r[1], "jobs": r[2], "last_name": r[3]}
                for r in rows if r[0] not in live_keys and (r[1] or r[2])]
