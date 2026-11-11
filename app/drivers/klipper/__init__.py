"""Klipper / Moonraker family.

Polls Moonraker's HTTP API (GET only; nothing is ever sent to Klipper). KlipperPrinter holds the
Moonraker plumbing; children add printer-specific extras (SnapmakerU1: filament slots, cavity
chamber sensor, device name/serial from /machine/system_info).
Real-hardware findings: docs/SPEC.md section 5.2.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, ClassVar, Optional

from ... import config
from ...errors import AuthFailed, DriverError, PrinterUnreachable
from ...transports.http import HttpJson, quote
from ..base import FieldSpec, PrinterBase
from ..registry import register
from . import parse

log = logging.getLogger(__name__)

_KLIPPER_FIELDS = [
    FieldSpec(key="host", label="IP address"),
    FieldSpec(key="port", label="Port", kind="int", required=False,
              help="Leave blank to try 80, then 7125"),
    FieldSpec(key="api_key", label="Moonraker API key", kind="secret", required=False,
              help="Only if the printer refuses connections without one"),
]
AUTH_HELP = ("the printer refused this computer ({}). Enter its Moonraker API key, or add your network "
             "to trusted_clients in moonraker.conf")



UPTIME_EVERY_S = 300


def boot_time(uptime_s) -> Optional[datetime]:
    """system_uptime (seconds) -> boot time, rounded to the minute so it doesn't wobble between reads."""
    if not isinstance(uptime_s, (int, float)) or uptime_s <= 0:
        return None
    t = datetime.now(timezone.utc) - timedelta(seconds=float(uptime_s))
    return t.replace(second=0, microsecond=0)

class KlipperPrinter(PrinterBase):
    family = "klipper"
    mode = "poll"
    default_poll_interval = 3.0
    abstract = True
    fields = _KLIPPER_FIELDS
    default_ports: ClassVar[tuple[int, ...]] = (80, 7125)
    chamber_sensor: ClassVar[Optional[str]] = None
    u1_filament: ClassVar[bool] = False

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._http: Optional[HttpJson] = None
        self._static: Optional[dict] = None          # objects, sensors, fans, firmware, name
        self._meta_for: Optional[str] = None         # filename the cached metadata belongs to
        self._meta: Optional[dict] = None
        self._thumb_key: Optional[str] = None
        self._thumb_url: Optional[str] = None
        self._thumb_task: Optional[asyncio.Task] = None
        self._totals: Optional[dict] = None
        self._totals_at: Optional[float] = None       # None = read on the first poll
        self._booted_at = None                          # from /machine/proc_stats system_uptime
        self._uptime_at: Optional[float] = None

    # ------------------------------------------------------------------ connection
    def _connect(self) -> HttpJson:
        """Find the Moonraker port (setting, else 80 then 7125)."""
        key = self.secret("api_key")
        port = self.setting("port")
        ports = (int(port),) if port else self.default_ports
        last: Exception = PrinterUnreachable(f"{self.config.host} not reachable")
        for p in ports:
            h = HttpJson(self.config.host, p, key)
            try:
                info = h.get("/server/info")
            except AuthFailed as e:
                raise AuthFailed(AUTH_HELP.format(e)) from e
            except (PrinterUnreachable, DriverError) as e:
                last = e
                continue
            if isinstance(info, dict) and "result" in info:
                return h
        raise last

    def _load_static(self, h: HttpJson) -> dict:
        objects = h.get("/printer/objects/list")["result"]["objects"]
        heaters = h.get("/printer/objects/query?heaters")["result"]["status"].get("heaters", {})
        sensors = heaters.get("available_sensors") or []
        info = h.get("/printer/info").get("result", {})
        static = {"objects": objects, "sensors": sensors,
                  "firmware": (info.get("software_version") or "").split("_")[0] or None,
                  "hostname": info.get("hostname"), "name": None, "serial": None}
        self._add_static(h, static)
        return static

    def _add_static(self, h: HttpJson, static: dict) -> None:
        """Children add model-specific facts (name, serial, firmware)."""

    def _query_path(self, static: dict) -> str:
        wanted = ["print_stats", "virtual_sdcard", "toolhead", "webhooks"]
        wanted += [o for o in ("print_task_config",) if o in static["objects"]]
        wanted += static["sensors"] + parse.fan_objects(static["objects"])
        return "/printer/objects/query?" + "&".join(quote(o).replace("/", "%2F")
                                                    for o in dict.fromkeys(wanted))

    def _read(self) -> tuple[dict, dict, Optional[dict]]:
        """Blocking: one full read. Returns (static, status, file metadata)."""
        if self._http is None or self._static is None:
            self._http = self._connect()
            self._static = self._load_static(self._http)
        h, static = self._http, self._static
        st = h.get(self._query_path(static))["result"]["status"]
        if self._totals_at is None or time.monotonic() - self._totals_at > 600:            # lifetime totals every 10 min
            self._totals_at = time.monotonic()
            try:
                self._totals = (h.get("/server/history/totals").get("result") or {}).get("job_totals")
            except DriverError:
                self._totals = None                             # history component disabled
        if self._uptime_at is None or time.monotonic() - self._uptime_at > UPTIME_EVERY_S:   # boot time, every 5 min
            self._uptime_at = time.monotonic()
            try:
                up = (h.get("/machine/proc_stats").get("result") or {}).get("system_uptime")
                self._booted_at = boot_time(up)
            except DriverError:
                self._booted_at = None                  # machine component missing / not allowed
        fname = (st.get("print_stats") or {}).get("filename") or None
        if fname != self._meta_for:
            self._meta_for, self._meta = fname, None
            if fname:
                try:
                    self._meta = h.get("/server/files/metadata?filename=" + quote(fname)).get("result")
                except DriverError:
                    self._meta = None                       # file deleted / not in gcodes root
        return static, st, self._meta

    # ------------------------------------------------------------------ poll
    async def _poll_once(self) -> None:
        try:
            static, st, meta = await asyncio.to_thread(self._read)
        except AuthFailed:
            self._http = self._static = None
            raise
        except DriverError:
            self._http = self._static = None                # reconnect + reload objects next time
            raise
        except (KeyError, TypeError) as e:
            self._http = self._static = None
            raise DriverError(f"unexpected Moonraker reply ({type(e).__name__}: {e})") from e
        status = parse.to_status(self.config.id, st, objects=static["objects"], sensors=static["sensors"],
                                 meta=meta, chamber_sensor=self.chamber_sensor, u1=self.u1_filament,
                                 firmware=static["firmware"])
        status.device_name = static.get("name") or self._fallback_name(static)
        status.booted_at = self._booted_at
        if isinstance(self._totals, dict) and isinstance(self._totals.get("total_print_time"), (int, float)):
            status.lifetime_print_s = float(self._totals["total_print_time"])
            jobs = self._totals.get("total_jobs")
            status.lifetime_jobs = int(jobs) if isinstance(jobs, (int, float)) else None
        if static.get("serial") and not self.config.serial:
            self.learned_serial = static["serial"]
        self._maybe_thumbnail(meta, status)
        if self._thumb_url and self._thumb_key == self._job_key(meta):
            status.job.thumbnail_url = self._thumb_url
            status.job.thumbnail_source = "printer"
        await self._publish(status)

    def _fallback_name(self, static: dict) -> Optional[str]:
        return static.get("hostname")

    # ------------------------------------------------------------------ thumbnails
    def _job_key(self, meta: Optional[dict]) -> Optional[str]:
        if not meta or not self._meta_for:
            return None
        return f"{self._meta_for}|{meta.get('modified')}|{meta.get('size')}"   # re-sliced file = new key

    def _maybe_thumbnail(self, meta: Optional[dict], status) -> None:
        key = self._job_key(meta)
        if not key or key == self._thumb_key or (self._thumb_task and not self._thumb_task.done()):
            return
        thumbs = [t for t in (meta.get("thumbnails") or []) if t.get("relative_path")]
        if not thumbs:
            self._thumb_key, self._thumb_url = key, None
            return
        best = max(thumbs, key=lambda t: (t.get("width") or 0) * (t.get("height") or 0))
        folder = self._meta_for.rsplit("/", 1)[0] + "/" if "/" in self._meta_for else ""
        self._thumb_task = asyncio.create_task(self._fetch_thumbnail(key, folder + best["relative_path"]))

    async def _fetch_thumbnail(self, key: str, rel: str) -> None:
        try:
            data = await asyncio.to_thread(self._http.get_bytes, "/server/files/gcodes/" + quote(rel))
        except Exception as e:  # noqa: BLE001 - try again on the next job change / reconnect
            log.info("printer %s: thumbnail download failed: %s", self.config.id, e)
            return
        if data[:8] != b"\x89PNG\r\n\x1a\n":
            log.info("printer %s: thumbnail %s is not a PNG", self.config.id, rel)
            return
        config.THUMB_DIR.mkdir(parents=True, exist_ok=True)
        (config.THUMB_DIR / f"{self.config.id}.png").write_bytes(data)
        self._thumb_key = key
        self._thumb_url = f"/api/printers/{self.config.id}/thumbnail?k={abs(hash(key)) % 10 ** 10}"
        log.info("printer %s: thumbnail %s", self.config.id, rel)


@register
class KlipperGeneric(KlipperPrinter):
    """Any stock Klipper/Moonraker printer (Voron, Mercury, ...). Filament shown is what the
    sliced file says (labeled 'From slicer')."""
    driver_key = "klipper.generic"
    maker = "Klipper (generic)"
    model_name = "Klipper / Moonraker"
    fields = _KLIPPER_FIELDS + [
        FieldSpec(key="model_label", label="Model (shown on the card)", required=False,
                  help='e.g. "Voron 2.4", "Mercury One"'),
    ]


@register
class SnapmakerU1(KlipperPrinter):
    """Snapmaker U1: 4 toolheads, print_task_config filament slots, cavity chamber sensor,
    name/serial/firmware from /machine/system_info product_info (verified on U1-A)."""
    driver_key = "snapmaker.u1"
    maker = "Snapmaker"
    model_name = "U1"
    fields = [_KLIPPER_FIELDS[0], _KLIPPER_FIELDS[1]]    # verified: port 80, no API key needed
    default_ports = (80, 7125)
    chamber_sensor = "temperature_sensor cavity"
    u1_filament = True

    def _add_static(self, h: HttpJson, static: dict) -> None:
        try:
            si = h.get("/machine/system_info")["result"]["system_info"]
        except (DriverError, KeyError, TypeError):
            return
        pi: dict[str, Any] = si.get("product_info") or {}
        static["name"] = pi.get("device_name") or None
        static["serial"] = pi.get("serial_number") or None
        static["firmware"] = pi.get("firmware_version") or static["firmware"]

    def _fallback_name(self, static: dict) -> Optional[str]:
        return None                     # every U1 reports hostname "lava": not a useful name
