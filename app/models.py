"""Normalized status model shared by every printer driver.

Every driver translates its own protocol into these types, so the UI renders one
layout for all printers. Missing data stays ``None`` and the UI shows the label
with an empty value (decision O1).
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field


class ConnState(str, Enum):
    CONNECTING = "connecting"
    ONLINE = "online"
    OFFLINE = "offline"          # unreachable / dropped off the network
    AUTH_ERROR = "auth_error"    # wrong access code / API key
    BUSY = "busy"                # printer refused: connection slots full (Elegoo CC)
    ERROR = "error"              # anything else; detail in PrinterStatus.connection_detail


class JobState(str, Enum):
    IDLE = "idle"
    PREPARING = "preparing"      # heating, leveling, homing, file checking
    PRINTING = "printing"
    PAUSED = "paused"
    FINISHED = "finished"
    CANCELLED = "cancelled"
    FAILED = "failed"
    ERROR = "error"
    UNKNOWN = "unknown"


class Temperature(BaseModel):
    key: str                      # stable id, e.g. "nozzle", "extruder2", "bed", "chamber"
    label: str                    # UI label, e.g. "Nozzle", "T3", "Bed", "Chamber"
    current: Optional[float] = None
    target: Optional[float] = None


class Fan(BaseModel):
    key: str
    label: str
    percent: Optional[int] = None


SlotSource = Literal["ams", "external", "toolhead", "canvas", "slicer"]


class FilamentSlot(BaseModel):
    index: int                    # 0-based position within its unit
    source: SlotSource
    present: Optional[bool] = None
    active: bool = False
    material: Optional[str] = None      # PLA, PETG ...
    sub_type: Optional[str] = None      # Matte, HF, Basic ...
    brand: Optional[str] = None
    color_hex: Optional[str] = None     # "#RRGGBB"
    extra_colors: list[str] = Field(default_factory=list)  # multi-color spools
    remaining_pct: Optional[int] = None  # only when the printer really knows it
    rfid: Optional[bool] = None


class FilamentUnit(BaseModel):
    id: str                        # "ams0", "external", "toolheads", "canvas0"
    kind: str                      # "AMS", "AMS 2 Pro", "AMS Lite", "External spool", "Canvas", "U1 toolheads"
    humidity_pct: Optional[int] = None
    humidity_level: Optional[int] = None   # Bambu 1-5 scale
    temperature: Optional[float] = None
    slots: list[FilamentSlot] = Field(default_factory=list)


class Job(BaseModel):
    name: Optional[str] = None          # human-friendly job name
    file_name: Optional[str] = None
    progress_pct: Optional[float] = None
    elapsed_s: Optional[int] = None
    remaining_s: Optional[int] = None
    eta: Optional[datetime] = None
    layer: Optional[int] = None
    total_layers: Optional[int] = None
    plate: Optional[int] = None
    plate_count: Optional[int] = None
    thumbnail_url: Optional[str] = None  # served by our backend, never the printer directly
    thumbnail_source: Optional[str] = None  # "cloud" (Bambu account) | "printer" (file on the printer)
    sub_state: Optional[str] = None      # e.g. "preheat done", "auto feeding"
    task_id: Optional[str] = None        # printer's own job id when it has one (CC TaskId) - links history imports
    transferring: bool = False           # file still arriving on the printer (Bambu cloud download, CC file transfer)
    transfer_pct: Optional[float] = None # Bambu only: gcode_file_prepare_percent


class PrinterError(BaseModel):
    code: str
    message: Optional[str] = None


class PrinterStatus(BaseModel):
    printer_id: str
    connection: ConnState = ConnState.CONNECTING
    connection_detail: Optional[str] = None
    state: JobState = JobState.UNKNOWN
    job: Job = Field(default_factory=Job)
    temps: list[Temperature] = Field(default_factory=list)
    fans: list[Fan] = Field(default_factory=list)
    filament: list[FilamentUnit] = Field(default_factory=list)
    errors: list[PrinterError] = Field(default_factory=list)
    reason: Optional[str] = None        # why it's paused / failed, in plain words, when the printer says
    firmware: Optional[str] = None
    device_name: Optional[str] = None   # the printer's own friendly name, if its protocol reports it
    lifetime_print_s: Optional[float] = None   # printer's own lifetime print-time counter, if reported
    lifetime_jobs: Optional[int] = None
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    link: Optional[str] = None               # Bambu: "local" or "cloud" (how the dashboard reaches it right now)
    last_seen_at: Optional[datetime] = None   # last time the printer itself reported (shown on offline cards)
    booted_at: Optional[datetime] = None      # when the printer's own computer last started (Klipper: uptime;
                                              # Bambu: report counter starting over, detected by the manager)
    report_seq: Optional[int] = None          # Bambu report sequence_id (starts over at boot - verified A1 Mini)
    unhomed: Optional[bool] = None            # CC: position is exactly 0,0,0 = not moved since power-on (verified)
    busy_with: Optional[str] = None   # idle but mid-maintenance ("firmware update", "AMS drying"): never cut power
    update_available: Optional[str] = None    # new firmware version the printer offers (notification only)
