"""SDCP v3 status -> normalized PrinterStatus. Pure functions, tested with a real CC dump.

Verified on the Centauri Carbon (fw V1.4.49, 2026-10-28/29), docs/SPEC.md 5.4:
  * card state comes from Status.CurrentStatus (0 idle, 1 printing, ...); PrintInfo.Status changes
    mid-print (e.g. 20 "preheat done" during a Canvas color change) -> shown as the sub-state
  * ticks are seconds: remaining = TotalTicks - CurrentTicks
  * temps are scalars (older firmware: [target, actual]); fans already in %
  * ErrorNumber absent on this firmware -> treat as 0
Canvas (Cmd 324) verified on a real CC 2026-10-30 (fixture cc1_fw1449_idle_canvas.json): connected is 1/0, active_tray_id -1 when idle;
mid-print it answers too (active_tray_id = tray in use). Tray status 0 = unloaded (old color/material still reported),
1 = loaded, 2 = loaded + feeding.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from ...models import (ConnState, Fan, FilamentSlot, FilamentUnit, Job, JobState, PrinterError,
                       PrinterStatus, Temperature)

MACHINE = {0: "idle", 1: "printing", 2: "receiving file", 3: "calibrating", 4: "device testing"}
PRINT = {0: "idle", 1: "homing", 5: "pausing", 6: "paused", 7: "stopping", 8: "stopped", 9: "completed",
         10: "checking file", 11: "checking printer", 12: "resuming", 13: "printing", 14: "error",
         15: "auto leveling", 16: "preheating", 17: "resonance testing", 18: "starting", 19: "leveling done",
         20: "preheat done", 21: "homing done", 22: "resonance test done"}
_PREPARING = {1, 10, 11, 15, 16, 17, 18, 19, 20, 21, 22}


def _temp(v) -> tuple[Optional[float], Optional[float]]:
    """(current, target-from-pair). Current fw sends scalars; older fw [target, actual]."""
    if isinstance(v, (int, float)):
        return float(v), None
    if isinstance(v, (list, tuple)) and len(v) >= 2:
        return float(v[1]), float(v[0])
    return None, None


def _t(status: dict, key: str, label: str, cur_key: str, tgt_key: str) -> Temperature:
    cur, tgt = _temp(status.get(cur_key))
    if tgt is None and isinstance(status.get(tgt_key), (int, float)):
        tgt = float(status[tgt_key])
    return Temperature(key=key, label=label, current=cur, target=tgt or None)


def _color(v: Any) -> Optional[str]:
    if isinstance(v, str):
        h = v.strip().lstrip("#")
        if len(h) >= 6 and all(c in "0123456789abcdefABCDEF" for c in h[:6]):
            return "#" + h[:6].upper()
    if isinstance(v, int) and v >= 0:
        return f"#{v & 0xFFFFFF:06X}"
    return None


def canvas_units(canvas: Optional[dict], job_active: bool) -> list[FilamentUnit]:
    if not canvas:
        return []
    active_canvas, active_tray = canvas.get("active_canvas_id"), canvas.get("active_tray_id")
    units = []
    for c in canvas.get("canvas_list") or []:
        if c.get("connected", 1) in (0, False):          # real fw sends 1/0
            continue
        slots = []
        for i, t in enumerate(c.get("tray_list") or []):
            # tray status (a real CC 2026-10-30): 0 = unloaded (the Canvas keeps the old color/material!),
            # 1 = loaded, 2 = loaded and feeding. Without a status, fall back to "has a material".
            stv = t.get("status")
            present = stv in (1, 2) if isinstance(stv, int) and not isinstance(stv, bool) else bool(t.get("filament_type"))
            tray_id = t.get("tray_id", i)
            slots.append(FilamentSlot(
                index=i, source="canvas", present=present,
                active=present and job_active and c.get("canvas_id") == active_canvas and tray_id == active_tray,
                material=(t.get("filament_type") or None) if present else None,
                sub_type=(t.get("filament_name") or None) if present else None,
                brand=(t.get("brand") or None) if present else None,
                color_hex=_color(t.get("filament_color")) if present else None))
        if slots:
            units.append(FilamentUnit(id=f"canvas{c.get('canvas_id', len(units))}", kind="Canvas", slots=slots))
    return units


def to_status(printer_id: str, status: dict, attrs: dict, canvas: Optional[dict] = None,
              now: Optional[datetime] = None) -> PrinterStatus:
    now = now or datetime.now(timezone.utc)
    cs = status.get("CurrentStatus") or status.get("CurrenStatus") or [0]
    machine = cs[0] if cs else 0
    pi = status.get("PrintInfo") or {}
    pstat = pi.get("Status")

    errors: list[PrinterError] = []
    if pi.get("ErrorNumber"):
        errors.append(PrinterError(code=f"E{pi['ErrorNumber']}", message=None))

    sub = None
    if machine == 1:
        if pstat in (5, 6):
            state = JobState.PAUSED
        elif pstat == 14:
            state = JobState.ERROR
        elif pstat in _PREPARING and not pi.get("CurrentLayer"):
            state = JobState.PREPARING
        else:
            state = JobState.PRINTING
        if pstat not in (13, None) and state != JobState.PAUSED:
            sub = PRINT.get(pstat)                     # e.g. "preheat done" mid-print
    elif machine == 0:
        state = {9: JobState.FINISHED, 8: JobState.CANCELLED, 14: JobState.ERROR}.get(pstat, JobState.IDLE)
    else:
        state, sub = JobState.PREPARING, MACHINE.get(machine)   # file transfer, calibrating, testing
    if state == JobState.ERROR and not errors:
        errors.append(PrinterError(code="print_error", message=PRINT.get(pstat)))

    job = Job()
    fname = pi.get("Filename") or None
    if fname and state not in (JobState.IDLE,):
        cur, tot = pi.get("CurrentTicks"), pi.get("TotalTicks")
        active = state in (JobState.PRINTING, JobState.PAUSED, JobState.PREPARING)
        rem = tot - cur if active and isinstance(cur, (int, float)) and isinstance(tot, (int, float)) \
            and tot >= cur else None
        prog = pi.get("Progress")
        if state == JobState.FINISHED:
            prog = 100
        name = fname.rsplit("/", 1)[-1]
        for ext in (".gcode", ".g", ".gco"):
            if name.lower().endswith(ext):
                name = name[: -len(ext)]
        job = Job(name=name, file_name=fname, progress_pct=float(prog) if isinstance(prog, (int, float)) else None,
                  elapsed_s=int(cur) if isinstance(cur, (int, float)) else None,
                  remaining_s=None if rem is None else int(rem),
                  eta=now + timedelta(seconds=rem) if rem is not None and state == JobState.PRINTING else None,
                  layer=pi.get("CurrentLayer"), total_layers=pi.get("TotalLayer") or None, sub_state=sub,
                  task_id=pi.get("TaskId") or None)
    elif sub:
        job.sub_state = sub
    job.transferring = machine == 2

    fans_raw = status.get("CurrentFanSpeed") or {}
    fans = [Fan(key="part", label="Part cooling", percent=fans_raw.get("ModelFan")),
            Fan(key="aux", label="Aux", percent=fans_raw.get("AuxiliaryFan")),
            Fan(key="chamber", label="Chamber", percent=fans_raw.get("BoxFan"))]
    temps = [_t(status, "nozzle", "Nozzle", "TempOfNozzle", "TempTargetNozzle"),
             _t(status, "bed", "Bed", "TempOfHotbed", "TempTargetHotbed"),
             _t(status, "chamber", "Chamber", "TempOfBox", "TempTargetBox")]
    job_active = state in (JobState.PRINTING, JobState.PAUSED, JobState.PREPARING)
    return PrinterStatus(printer_id=printer_id, connection=ConnState.ONLINE, state=state, job=job, temps=temps,
                         fans=fans, filament=canvas_units(canvas, job_active), errors=errors,
                         firmware=attrs.get("FirmwareVersion"), device_name=attrs.get("Name") or None,
                         updated_at=now, unhomed=unhomed(status.get("CurrenCoord")))


def unhomed(coord) -> Optional[bool]:
    """CurrenCoord exactly 0,0,0 right after power-on; any real position once it has moved or homed
    (a real CC 2026-11-05: parked 202.00,264.50,224.40 -> reboot 0.00,0.00,0.00 -> homed 202.00,264.50,14.35)."""
    try:
        vals = [float(x) for x in str(coord).split(",")]
    except ValueError:
        return None
    return len(vals) == 3 and all(v == 0 for v in vals) if vals else None


# CC1 history (Cmd 321) TaskStatus: 1 = completed, 3 = stopped [pycentauri 0.9.1; a real dump: 1 on 143/143-layer jobs]
_TASK_RESULT = {1: "finished", 3: "cancelled"}


def history_items(details: list[dict]) -> list[dict]:
    """Cmd 321 HistoryDetailList -> finished jobs for History.import_printer_jobs. Skips running/incomplete rows."""
    out = []
    for d in details or []:
        tid, begin, end = d.get("TaskId"), d.get("BeginTime"), d.get("EndTime")
        if not tid or not isinstance(begin, (int, float)) or not isinstance(end, (int, float)) or end <= begin or begin <= 0:
            continue
        secs = (d.get("SliceInformation") or {}).get("print_time")
        if not isinstance(secs, (int, float)) or secs <= 0:
            secs = end - begin
        result = "failed" if d.get("ErrorStatusReason") else _TASK_RESULT.get(d.get("TaskStatus"), "unknown")
        path = d.get("TaskName") or ""
        name = path.rsplit("/", 1)[-1]
        for ext in (".gcode", ".g", ".gco"):
            if name.lower().endswith(ext):
                name = name[: -len(ext)]
        out.append({"external_id": tid, "job_name": name or None, "file_name": path or None,
                    "started_at": datetime.fromtimestamp(begin, timezone.utc),
                    "ended_at": datetime.fromtimestamp(end, timezone.utc),
                    "result": result, "print_seconds": float(secs)})
    return out
