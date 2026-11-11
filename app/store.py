"""SQLite storage for configured printers (add / remove / reorder).

Secrets (Bambu access code, Moonraker API key) live in their own column and are
never returned by ``to_public()``, so they can't leak to the browser.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

SCHEMA = """
CREATE TABLE IF NOT EXISTS printers (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    driver      TEXT NOT NULL,          -- registry key, e.g. "bambu.x1c"
    host        TEXT NOT NULL,
    serial      TEXT,                   -- Bambu serial / U1 serial / CC MainboardID
    settings    TEXT NOT NULL DEFAULT '{}',   -- non-secret driver settings (JSON)
    secrets     TEXT NOT NULL DEFAULT '{}',   -- access code, api key (JSON)
    sort_order  INTEGER NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_printers_driver_serial
    ON printers(driver, serial) WHERE serial IS NOT NULL;
CREATE TABLE IF NOT EXISTS app_settings (
    key    TEXT PRIMARY KEY,               -- e.g. "bambu_cloud" (holds a token: never sent to the UI)
    value  TEXT NOT NULL                   -- JSON
);
"""


class PrinterConfig(BaseModel):
    id: str
    name: str
    driver: str
    host: str
    serial: Optional[str] = None
    settings: dict[str, Any] = Field(default_factory=dict)
    secrets: dict[str, str] = Field(default_factory=dict)
    sort_order: int = 0
    created_at: Optional[str] = None

    def to_public(self) -> dict[str, Any]:
        """Safe for the UI: secret values replaced by a set/not-set flag."""
        d = self.model_dump(exclude={"secrets"})
        d["secrets_set"] = {k: bool(v) for k, v in self.secrets.items()}
        return d


class DuplicatePrinter(ValueError):
    pass


class PrinterStore:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(SCHEMA)
            self._db.commit()

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _row(r: sqlite3.Row) -> PrinterConfig:
        return PrinterConfig(
            id=r["id"], name=r["name"], driver=r["driver"], host=r["host"], serial=r["serial"],
            settings=json.loads(r["settings"]), secrets=json.loads(r["secrets"]),
            sort_order=r["sort_order"], created_at=r["created_at"])

    # ------------------------------------------------------------------ queries
    def list(self) -> list[PrinterConfig]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM printers ORDER BY sort_order, created_at").fetchall()
        return [self._row(r) for r in rows]

    def get(self, printer_id: str) -> Optional[PrinterConfig]:
        with self._lock:
            r = self._db.execute("SELECT * FROM printers WHERE id = ?", (printer_id,)).fetchone()
        return self._row(r) if r else None

    def find(self, driver: str, serial: Optional[str] = None, host: Optional[str] = None) -> Optional[PrinterConfig]:
        """Used by discovery to mark printers as already added (serial first, then host)."""
        with self._lock:
            r = None
            if serial:
                r = self._db.execute("SELECT * FROM printers WHERE driver = ? AND serial = ?",
                                     (driver, serial)).fetchone()
            if r is None and host:
                r = self._db.execute("SELECT * FROM printers WHERE driver = ? AND host = ?",
                                     (driver, host)).fetchone()
        return self._row(r) if r else None

    # ------------------------------------------------------------------ changes
    def add(self, name: str, driver: str, host: str, serial: Optional[str] = None,
            settings: Optional[dict] = None, secrets: Optional[dict] = None) -> PrinterConfig:
        pid = uuid.uuid4().hex[:12]
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._lock:
            nxt = self._db.execute("SELECT COALESCE(MAX(sort_order), -1) + 1 FROM printers").fetchone()[0]
            try:
                self._db.execute(
                    "INSERT INTO printers (id, name, driver, host, serial, settings, secrets, sort_order, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (pid, name, driver, host, serial or None, json.dumps(settings or {}),
                     json.dumps(secrets or {}), nxt, now))
            except sqlite3.IntegrityError as e:
                raise DuplicatePrinter(f"{driver} printer with serial {serial} is already added") from e
            self._db.commit()
        return self.get(pid)  # type: ignore[return-value]

    def update(self, printer_id: str, **fields: Any) -> PrinterConfig:
        allowed = {"name", "host", "serial", "settings", "secrets"}
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"cannot update {sorted(bad)}")
        sets, vals = [], []
        for k, v in fields.items():
            sets.append(f"{k} = ?")
            vals.append(json.dumps(v) if k in ("settings", "secrets") else v)
        with self._lock:
            cur = self._db.execute(f"UPDATE printers SET {', '.join(sets)} WHERE id = ?", (*vals, printer_id))
            self._db.commit()
        if cur.rowcount == 0:
            raise KeyError(printer_id)
        return self.get(printer_id)  # type: ignore[return-value]

    def remove(self, printer_id: str) -> bool:
        with self._lock:
            cur = self._db.execute("DELETE FROM printers WHERE id = ?", (printer_id,))
            self._db.commit()
        return cur.rowcount > 0

    def reorder(self, ordered_ids: list[str]) -> None:
        """Persist card order (drag and drop). Must contain every printer id exactly once."""
        with self._lock:
            existing = {r[0] for r in self._db.execute("SELECT id FROM printers")}
            if set(ordered_ids) != existing or len(ordered_ids) != len(existing):
                raise ValueError("reorder list must contain every printer id exactly once")
            self._db.executemany("UPDATE printers SET sort_order = ? WHERE id = ?",
                                 [(i, pid) for i, pid in enumerate(ordered_ids)])
            self._db.commit()

    # ------------------------------------------------------------------ app settings
    def get_app_setting(self, key: str) -> Optional[Any]:
        with self._lock:
            r = self._db.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
        return json.loads(r[0]) if r else None

    def set_app_setting(self, key: str, value: Any) -> None:
        with self._lock:
            self._db.execute("INSERT INTO app_settings (key, value) VALUES (?, ?)"
                             " ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, json.dumps(value)))
            self._db.commit()

    def delete_app_setting(self, key: str) -> bool:
        with self._lock:
            cur = self._db.execute("DELETE FROM app_settings WHERE key = ?", (key,))
            self._db.commit()
        return cur.rowcount > 0

    def close(self) -> None:
        with self._lock:
            self._db.close()
