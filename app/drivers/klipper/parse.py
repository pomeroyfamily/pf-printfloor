"""Moonraker object status -> normalized PrinterStatus. Pure functions (tested with fixtures).

Sources (docs/SPEC.md 5.2; U1 values verified with u1_probe on a real U1-A, 2026-10-28):
  print_stats      state, filename, print_duration, message, info.current_layer/total_layer
                   U1 also: exception {id, index (toolhead, 0-based), code, message, level} - the pause reason
                   (verified on U1-C 2026-11-02: code 38 "detect filament tangled!", index 1)
  virtual_sdcard   progress (file position, 0..1)
  toolhead         extruder (active tool: "extruder", "extruder1", ...)
  webhooks         klippy state / state_message
  heaters          available_sensors -> each sensor object's temperature/target
  fans             fan, fan_generic X, heater_fan X, controller_fan X -> speed 0..1
  U1 print_task_config  filament_vendor/type/sub_type/color_rgba/exist/official per slot
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from ...models import (ConnState, Fan, FilamentSlot, FilamentUnit, Job, JobState, PrinterError,
                       PrinterStatus, Temperature)

STATE = {"standby": JobState.IDLE, "printing": JobState.PRINTING, "paused": JobState.PAUSED,
         "complete": JobState.FINISHED, "cancelled": JobState.CANCELLED, "error": JobState.ERROR}
FAN_TYPES = ("fan", "fan_generic", "heater_fan", "controller_fan", "temperature_fan")
_EXTRUDER = re.compile(r"^extruder(\d*)$")
_TOOL_FAN = re.compile(r"^e(\d+)_(nozzle_)?fan$")


def exception_reason(exc: Any) -> Optional[str]:
    """U1 print_stats.exception -> "filament tangled (Toolhead 2)". None when there is no message."""
    if not isinstance(exc, dict) or not str(exc.get("message") or "").strip():
        return None
    msg = re.sub(r"^(detect(ed)?|found)\s+", "", str(exc["message"]).strip(), flags=re.I).rstrip("!. ")
    idx = exc.get("index")
    return msg + (f" (Toolhead {int(idx) + 1})" if isinstance(idx, int) and 0 <= idx < 16 else "")


def tool_index(name: str) -> Optional[int]:
    """"extruder" -> 0, "extruder3" -> 3, anything else -> None."""
    m = _EXTRUDER.match(name or "")
    if not m:
        return None
    return int(m[1]) if m[1] else 0


def fan_objects(objects: list[str]) -> list[str]:
    return [o for o in objects if o == "fan" or o.split(" ")[0] in FAN_TYPES[1:]]


def fan_label(obj: str) -> tuple[str, str]:
    """(key, label) for a Klipper fan object."""
    if obj == "fan":
        return "part", "Part cooling"
    short = obj.split(" ", 1)[-1]
    m = _TOOL_FAN.match(short)
    if m:
        return short, f"T{int(m[1]) + 1} {'hotend' if m[2] else 'part cooling'}"
    known = {"cavity_fan": "Chamber", "chamber_fan": "Chamber", "power_fan": "Power supply",
             "controller_fan": "Electronics", "exhaust_fan": "Exhaust", "hotend_fan": "Hotend"}
    return short, known.get(short, short.replace("_", " ").capitalize())


def rgba_hex(v: Any) -> Optional[str]:
    if isinstance(v, str) and re.fullmatch(r"#?[0-9a-fA-F]{6}([0-9a-fA-F]{2})?", v.strip()):
        return "#" + v.strip().lstrip("#")[:6].upper()
    if isinstance(v, int) and v >= 0:
        return f"#{(v >> 8) & 0xFFFFFF:06X}"
    return None


def _num(v) -> Optional[float]:
    return float(v) if isinstance(v, (int, float)) else None


def temps(st: dict, sensors: list[str], chamber_sensor: Optional[str], active_tool: int) -> list[Temperature]:
    """Card order: Nozzle (active tool), Bed, Chamber; then every toolhead and other sensor."""
    def t(obj, key, label):
        s = st.get(obj) or {}
        tgt = _num(s.get("target"))
        return Temperature(key=key, label=label, current=_num(s.get("temperature")), target=tgt or None)

    tools = sorted((tool_index(n), n) for n in sensors if tool_index(n) is not None)
    chamber = chamber_sensor if chamber_sensor in sensors else next(
        (n for n in sensors if "chamber" in n or "cavity" in n), None)
    active_obj = next((n for i, n in tools if i == active_tool), tools[0][1] if tools else "extruder")
    out = [t(active_obj, "nozzle", "Nozzle"), t("heater_bed", "bed", "Bed"),
           t(chamber, "chamber", "Chamber") if chamber else Temperature(key="chamber", label="Chamber")]
    if len(tools) > 1:
        out += [t(n, f"tool{i}", f"T{i + 1} nozzle") for i, n in tools]
    used = {active_obj, "heater_bed", chamber} | {n for _, n in tools}
    for n in sensors:
        if n not in used:
            label = n.split(" ", 1)[-1].replace("_", " ").capitalize()
            out.append(t(n, n.replace(" ", "_"), label))
    return out


def fans(st: dict, objects: list[str]) -> list[Fan]:
    out = []
    for o in fan_objects(objects):
        sp = _num((st.get(o) or {}).get("speed"))
        key, label = fan_label(o)
        out.append(Fan(key=key, label=label, percent=None if sp is None else round(sp * 100)))
    return out


def u1_filament(st: dict, active_tool: int, job_active: bool) -> list[FilamentUnit]:
    ptc = st.get("print_task_config")
    if not isinstance(ptc, dict):
        return []
    n = max(len(ptc.get("filament_type") or []), len(ptc.get("filament_exist") or []))

    def g(key, i):
        v = ptc.get(key) or []
        return v[i] if i < len(v) else None

    slots = []
    for i in range(n):
        present = bool(g("filament_exist", i))
        slots.append(FilamentSlot(
            index=i, source="toolhead", present=present, active=present and i == active_tool and job_active,
            material=(g("filament_type", i) or None) if present else None,
            sub_type=(g("filament_sub_type", i) or None) if present else None,
            brand=(g("filament_vendor", i) or None) if present else None,
            color_hex=rgba_hex(g("filament_color_rgba", i)) if present else None,
            rfid=bool(g("filament_official", i)) if present else None))
    return [FilamentUnit(id="toolheads", kind="Toolheads", slots=slots)] if slots else []


def slicer_filament(meta: Optional[dict]) -> list[FilamentUnit]:
    """Generic Klipper (O3): what the sliced file says, labeled 'From slicer'."""
    if not meta:
        return []
    def split(v):
        return [x.strip().strip('"') for x in str(v).split(";")] if v else []
    types, colors, names = split(meta.get("filament_type")), split(meta.get("filament_colors")), \
        split(meta.get("filament_name"))
    n = max(len(types), len(colors), len(names))
    slots = [FilamentSlot(index=i, source="slicer", present=True,
                          material=types[i] if i < len(types) else None,
                          color_hex=rgba_hex(colors[i]) if i < len(colors) else None,
                          sub_type=None, brand=None) for i in range(n)]
    return [FilamentUnit(id="slicer", kind="From slicer", slots=slots)] if slots else []


def remaining(progress: Optional[float], duration: Optional[float], estimate: Optional[float]) -> Optional[float]:
    """Blend slicer estimate (trusted early) with file-progress extrapolation (trusted late)."""
    if progress is None or progress <= 0:
        return estimate
    by_file = duration / progress - duration if duration and progress > 0.01 else None
    by_slicer = max(0.0, estimate - duration) if estimate and duration is not None else None
    if by_file is None:
        return by_slicer
    if by_slicer is None:
        return by_file
    return (1 - progress) * by_slicer + progress * by_file


def to_status(printer_id: str, st: dict, *, objects: list[str], sensors: list[str],
              meta: Optional[dict], chamber_sensor: Optional[str], u1: bool,
              firmware: Optional[str], now: Optional[datetime] = None) -> PrinterStatus:
    now = now or datetime.now(timezone.utc)
    ps = st.get("print_stats") or {}
    wh = st.get("webhooks") or {}
    active_tool = tool_index((st.get("toolhead") or {}).get("extruder") or "extruder") or 0

    klippy = wh.get("state", "ready")
    state = STATE.get(ps.get("state"), JobState.UNKNOWN)
    errors: list[PrinterError] = []
    if klippy != "ready":
        state = JobState.ERROR
        errors.append(PrinterError(code=f"klippy_{klippy}", message=wh.get("state_message")))
    elif state == JobState.ERROR and ps.get("message"):
        errors.append(PrinterError(code="print_error", message=ps.get("message")))
    reason = None
    if state in (JobState.PAUSED, JobState.ERROR):           # a stale exception must not label a running print
        why = exception_reason(ps.get("exception"))
        if why:
            exc = ps["exception"]
            errors.append(PrinterError(code=f"u1_{exc.get('code')}", message=why))
            reason = ("Paused: " if state == JobState.PAUSED else "") + why
        elif state == JobState.PAUSED and ps.get("message"):
            reason = "Paused: " + str(ps["message"]).strip()

    job_active = state in (JobState.PRINTING, JobState.PAUSED)
    job = Job()
    fname = ps.get("filename") or None
    if fname and state != JobState.IDLE:
        progress = _num((st.get("virtual_sdcard") or {}).get("progress"))
        duration = _num(ps.get("print_duration"))
        info = ps.get("info") or {}
        est = _num((meta or {}).get("estimated_time"))
        rem = remaining(progress, duration, est) if job_active else None
        if state == JobState.FINISHED:
            progress = 1.0
        layer_h = _num((meta or {}).get("layer_height"))
        total = info.get("total_layer") or (
            round(_num(meta.get("object_height")) / layer_h) if meta and layer_h and _num(meta.get("object_height"))
            else None)
        job = Job(name=re.sub(r"\.(gcode|g|gco)$", "", fname.rsplit("/", 1)[-1], flags=re.I),
                  file_name=fname, progress_pct=None if progress is None else round(progress * 100, 1),
                  elapsed_s=None if duration is None else int(duration),
                  remaining_s=None if rem is None else int(rem),
                  eta=now + timedelta(seconds=rem) if rem is not None and state == JobState.PRINTING else None,
                  layer=info.get("current_layer"), total_layers=total)

    filament = u1_filament(st, active_tool, job_active) if u1 else slicer_filament(meta if job_active else None)
    return PrinterStatus(printer_id=printer_id, connection=ConnState.ONLINE, state=state, job=job,
                         temps=temps(st, sensors, chamber_sensor, active_tool), fans=fans(st, objects),
                         filament=filament, errors=errors, firmware=firmware, updated_at=now, reason=reason)
