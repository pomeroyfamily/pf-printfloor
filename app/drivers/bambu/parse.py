"""Bambu MQTT report -> normalized PrinterStatus.

Pure functions only (no I/O) so they can be tested against real dumps in tests/fixtures/bambu/.
Field notes and per-firmware quirks: docs/SPEC.md section 5.1.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from ...models import (Fan, FilamentSlot, FilamentUnit, Job, JobState, PrinterError, PrinterStatus,
                       Temperature)
from . import errors as bambu_errors
from .filaments import FILAMENTS

GCODE_STATE = {
    "IDLE": JobState.IDLE, "INIT": JobState.IDLE, "OFFLINE": JobState.UNKNOWN,
    "PREPARE": JobState.PREPARING, "SLICING": JobState.PREPARING,
    "RUNNING": JobState.PRINTING, "PAUSE": JobState.PAUSED,
    "FINISH": JobState.FINISHED, "FAILED": JobState.FAILED,
}
ACTIVE = {JobState.PREPARING, JobState.PRINTING, JobState.PAUSED}

# stg_cur -> human label (ids from the community-documented Bambu stage table)
STAGES = {
    0: "Printing", 1: "Auto bed leveling", 2: "Heatbed preheating", 3: "Vibration compensation",
    4: "Changing filament", 5: "M400 pause", 6: "Paused: filament runout", 7: "Heating hotend",
    8: "Calibrating extrusion", 9: "Scanning bed surface", 10: "Inspecting first layer",
    11: "Identifying build plate", 12: "Calibrating micro lidar", 13: "Homing toolhead",
    14: "Cleaning nozzle tip", 15: "Checking extruder temperature", 16: "Paused by user",
    17: "Paused: front cover falling", 18: "Calibrating micro lidar", 19: "Calibrating extrusion flow",
    20: "Paused: nozzle temperature malfunction", 21: "Paused: heatbed temperature malfunction",
    22: "Unloading filament", 23: "Paused: skipped step", 24: "Loading filament",
    25: "Calibrating motor noise", 26: "Paused: AMS lost", 27: "Paused: heatbreak fan speed low",
    28: "Paused: chamber temperature control error", 29: "Cooling chamber", 30: "Paused by G-code",
    31: "Motor noise showoff", 32: "Paused: nozzle filament covered", 33: "Paused: cutter error",
    34: "Paused: first layer error", 35: "Paused: nozzle clog", 49: "Heating chamber",
    50: "Cooling heatbed", 51: "Printing calibration lines", 52: "Checking material",
    54: "Waiting for heatbed temperature", 55: "Checking material position", 57: "Measuring surface",
    58: "Thermal preconditioning", 64: "Preparing hotend", 69: "Cooling nozzle",
    76: "Pre-extrusion before printing", 77: "Preparing AMS",
}
NO_STAGE = {-1, 0, 255}          # 0 = plain printing (same as the main state)

_PLATE_RE = re.compile(r"_plate_(\d+)", re.IGNORECASE)
_AMS_NAME_RE = re.compile(r"^(?P<kind>.+?) \((?P<n>\d+)\)$")


# --------------------------------------------------------------------------- helpers
def _num(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _int(v: Any) -> Optional[int]:
    f = _num(v)
    return None if f is None else int(f)


def _fan_pct(raw: Any) -> Optional[int]:
    """Bambu fans report raw 0-15 steps."""
    v = _int(raw)
    return None if v is None else round(max(0, min(v, 15)) / 15 * 100)


def _hex6(rgba: Any) -> Optional[str]:
    if not isinstance(rgba, str) or len(rgba) < 6:
        return None
    return "#" + rgba[:6].upper()


def _has_tag(tray: dict) -> bool:
    return bool(str(tray.get("tray_uuid", "")).strip("0")) or bool(str(tray.get("tag_uid", "")).strip("0"))


def clean_job_name(name: Optional[str]) -> Optional[str]:
    if not name:
        return None
    for suf in (".gcode.3mf", ".3mf", ".gcode"):
        if name.lower().endswith(suf):
            return name[: -len(suf)]
    return name


def plate_from_name(name: Optional[str]) -> Optional[int]:
    if not name:
        return None
    m = _PLATE_RE.findall(name)
    return int(m[-1]) if m else None


def hms_code(entry: dict) -> str:
    full = f"{int(entry.get('attr', 0)):08X}{int(entry.get('code', 0)):08X}"
    return "HMS_" + "_".join(full[i:i + 4] for i in range(0, 16, 4))


def ams_kinds(version: Optional[dict]) -> dict[int, str]:
    """AMS unit id -> kind, from info.get_version module product names ("AMS 2 Pro (1)" -> id 0)."""
    kinds: dict[int, str] = {}
    for m in (version or {}).get("module", []) or []:
        pn = (m.get("product_name") or "").strip()
        mm = _AMS_NAME_RE.match(pn)
        if mm and "AMS" in mm["kind"] and "Hub" not in mm["kind"]:
            kinds[int(mm["n"]) - 1] = mm["kind"]
    return kinds


def firmware(version: Optional[dict]) -> Optional[str]:
    for m in (version or {}).get("module", []) or []:
        if m.get("name") == "ota":
            return m.get("sw_ver")
    return None


# --------------------------------------------------------------------------- filament
def _slot(tray: dict, index: int, source: str, active: bool) -> FilamentSlot:
    present = bool(tray.get("tray_type"))
    idx = tray.get("tray_info_idx") or None
    preset = FILAMENTS.get(idx) if idx else None
    tagged = _has_tag(tray)
    remain = _int(tray.get("remain"))
    cols = [c for c in (tray.get("cols") or []) if isinstance(c, str)]
    return FilamentSlot(
        index=index, source=source, present=present, active=active and present,
        material=tray.get("tray_type") or None,
        sub_type=(tray.get("tray_sub_brands") or None),
        brand=(preset[1] if preset else None),
        color_hex=_hex6(tray.get("tray_color")) if present else None,
        extra_colors=[_hex6(c) for c in cols[1:] if _hex6(c)] if present else [],
        # Only trust remain for tagged (RFID) spools; -1 / 0 mean unknown on untagged ones.
        remaining_pct=remain if (present and tagged and remain is not None and remain > 0) else None,
        rfid=tagged if present else None,
    )


def filament_units(s: dict, version: Optional[dict]) -> list[FilamentUnit]:
    units: list[FilamentUnit] = []
    ams = s.get("ams") or {}
    tray_now = str(ams.get("tray_now", "255"))
    kinds = ams_kinds(version)
    for u in ams.get("ams") or []:
        uid = _int(u.get("id")) or 0
        hum_raw = _int(u.get("humidity_raw"))
        slots = []
        for t in u.get("tray") or []:
            tid = _int(t.get("id")) or 0
            slots.append(_slot(t, tid, "ams", active=(tray_now == str(uid * 4 + tid))))
        units.append(FilamentUnit(
            id=f"ams{uid}", kind=kinds.get(uid, "AMS"),
            humidity_pct=hum_raw if hum_raw and 0 < hum_raw <= 100 else None,
            humidity_level=_int(u.get("humidity")),
            temperature=_num(u.get("temp")),
            slots=slots))
    # External spool: vir_slot[] (newer fw, may hold two) else vt_tray
    ext_list = s.get("vir_slot") if isinstance(s.get("vir_slot"), list) and s.get("vir_slot") else (
        [s["vt_tray"]] if isinstance(s.get("vt_tray"), dict) else [])
    if ext_list:
        slots = []
        for i, t in enumerate(ext_list):
            ext_id = str(t.get("id", "254"))
            slots.append(_slot(t, i, "external", active=(tray_now == ext_id)))
        units.append(FilamentUnit(id="external", kind="External spool", slots=slots))
    return units


# --------------------------------------------------------------------------- main entry
def to_status(printer_id: str, s: dict, version: Optional[dict], *, has_chamber_sensor: bool,
              has_aux_fan: bool, has_chamber_fan: bool, now: Optional[datetime] = None,
              model: Optional[str] = None) -> PrinterStatus:
    """Build a PrinterStatus from the merged `print` state and the get_version info."""
    now = now or datetime.now(timezone.utc)
    st = GCODE_STATE.get(str(s.get("gcode_state", "")).upper(), JobState.UNKNOWN)

    # ---- job ----
    gfile = s.get("gcode_file") or None
    subtask = s.get("subtask_name") or None
    layers = s.get("3D") or {}
    progress = _num(s.get("mc_percent", s.get("percent")))
    remaining_min = _int(s.get("mc_remaining_time", s.get("remain_time")))
    stage = _int(s.get("stg_cur"))
    job = Job(
        name=clean_job_name(subtask or gfile),
        file_name=(gfile if gfile and not gfile.startswith("/data/") else subtask),
        progress_pct=progress,
        layer=_int(s.get("layer_num", layers.get("layer_num"))),
        total_layers=_int(s.get("total_layer_num", layers.get("total_layer_num"))),
        plate=_int(s.get("plate_idx")) or plate_from_name(subtask) or plate_from_name(gfile),
        plate_count=_int(s.get("plate_cnt")),
        sub_state=STAGES.get(stage) if (st in ACTIVE and stage not in NO_STAGE and stage is not None) else None,
    )
    if st in ACTIVE and remaining_min is not None and remaining_min >= 0:
        job.remaining_s = remaining_min * 60
        job.eta = now + timedelta(minutes=remaining_min)
    prep = _int(s.get("gcode_file_prepare_percent"))
    if st == JobState.PREPARING and prep is not None and 0 <= prep < 100:
        job.transferring, job.transfer_pct = True, float(prep)     # cloud job still downloading to the printer
        job.sub_state = f"downloading file {prep}%"
    if st == JobState.IDLE:
        job = Job()  # nothing to show for an idle printer

    # ---- temps (labels always present; value None when the printer has no sensor) ----
    ctc = (((s.get("device") or {}).get("ctc") or {}).get("info") or {}).get("temp")
    chamber = None
    if has_chamber_sensor:
        chamber = _num(ctc) if ctc is not None else _num(s.get("chamber_temper"))
    temps = [
        Temperature(key="nozzle", label="Nozzle", current=_num(s.get("nozzle_temper")),
                    target=_num(s.get("nozzle_target_temper"))),
        Temperature(key="bed", label="Bed", current=_num(s.get("bed_temper")),
                    target=_num(s.get("bed_target_temper"))),
        Temperature(key="chamber", label="Chamber", current=chamber),
    ]

    # ---- fans ----
    fans = [
        Fan(key="part", label="Part cooling", percent=_fan_pct(s.get("cooling_fan_speed"))),
        Fan(key="aux", label="Aux", percent=_fan_pct(s.get("big_fan1_speed")) if has_aux_fan else None),
        Fan(key="chamber", label="Chamber", percent=_fan_pct(s.get("big_fan2_speed")) if has_chamber_fan else None),
        Fan(key="heatbreak", label="Heatbreak", percent=_fan_pct(s.get("heatbreak_fan_speed"))),
    ]

    # ---- errors ----
    errors = []
    perr = _int(s.get("print_error"))
    if perr:
        errors.append(PrinterError(code=f"PRINT_ERROR_{perr:08X}", message=bambu_errors.print_error_text(perr, model)))
    for h in (s.get("hms") or []):
        if isinstance(h, dict):
            errors.append(PrinterError(code=hms_code(h), message=bambu_errors.hms_text(
                int(h.get("attr", 0) or 0), int(h.get("code", 0) or 0), model)))

    # Why it stopped: a "Paused: ..." stage says it directly; otherwise the first error with a known text.
    reason = None
    if st in (JobState.PAUSED, JobState.FAILED, JobState.ERROR):
        label = STAGES.get(stage) if stage is not None else None
        if label and (label.startswith("Paused") or st != JobState.PAUSED) and stage not in NO_STAGE:
            reason = label                                   # whole phrase: "Paused: filament runout", "Paused by user"
        if not reason:
            reason = next((bambu_errors.short(e.message) for e in errors if e.message), None)

    return PrinterStatus(
        printer_id=printer_id, state=st, job=job, temps=temps, fans=fans,
        filament=filament_units(s, version), errors=errors, firmware=firmware(version),
        updated_at=now, reason=reason, busy_with=busy_with(s), update_available=update_available(s))


# upgrade_state.status values seen idle on real printers [verified]: IDLE, UPGRADE_SUCCESS. Anything else
# (DOWNLOADING / UPGRADING / FLASHING ... [recalled]) is treated as an update in progress - the safe side.
UPGRADE_IDLE = {"", "IDLE", "UPGRADE_SUCCESS", "UPGRADE_FAIL", "UPGRADE_FAILED", "FAILED", "SUCCESS"}


def busy_with(s: dict) -> Optional[str]:
    """Maintenance that must not lose power even though no print is running."""
    up = s.get("upgrade_state")
    if isinstance(up, dict) and str(up.get("status") or "").upper() not in UPGRADE_IDLE:
        return "firmware update"
    for u in ((s.get("ams") or {}).get("ams") or []):
        if isinstance(u, dict) and (_int(u.get("dry_time")) or 0) > 0:      # minutes of drying left [guess]
            return "AMS drying"
    return None


def update_available(s: dict) -> Optional[str]:
    """New printer firmware on offer: X1C reports new_version_state 1 + ota_new_version_number [verified on
    a real X1C]; P1/A1 list modules in new_ver_list [recalled]."""
    up = s.get("upgrade_state")
    if not isinstance(up, dict) or _int(up.get("new_version_state")) != 1:
        return None
    if up.get("ota_new_version_number"):
        return str(up["ota_new_version_number"])
    for m in up.get("new_ver_list") or []:
        if isinstance(m, dict) and m.get("name") == "ota" and m.get("new_ver"):
            return str(m["new_ver"])
    return "new version"


# --------------------------------------------------------------------------- report merging
TASK_KEYS = ("task_id", "subtask_name", "gcode_file")


def merge_report(state: dict, delta: dict) -> bool:
    """Merge one `print` payload into `state` (P1/A1 send deltas). Lists are replaced.

    Returns True when a different job started (caller resets thumbnail etc.).
    A new job briefly inherits the old job's mc_percent / layer_num until fresh values
    arrive, so those are reset when the job identity changes without them.
    """
    before = tuple(state.get(k) for k in TASK_KEYS)
    _deep_merge(state, delta)
    after = tuple(state.get(k) for k in TASK_KEYS)
    new_job = any(b is not None for b in before) and before != after
    if new_job:
        for key in ("mc_percent", "layer_num"):
            if key not in delta:
                state[key] = 0
    return new_job


def _deep_merge(dst: dict, src: dict) -> None:
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _deep_merge(dst[k], v)
        else:
            dst[k] = v
