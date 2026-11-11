"""Bambu Lab family: Bambu's MQTT protocol (TLS 8883) + FTPS (990) for thumbnails.

Protocol details and real-hardware findings: docs/SPEC.md section 5.1.
Read-only: the only messages ever published are `pushing.pushall` and `info.get_version`.
Away from the LAN the same reports come through Bambu's cloud MQTT (connection setting auto / cloud).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import socket
import ssl
import time
from typing import ClassVar, Optional

from ... import config
from ...errors import AuthFailed, DriverError, PrinterUnreachable
from ...cloud import bambu as bambu_cloud
from ...models import ConnState, JobState
from ...transports.mqtt import MqttConnection
from ...transports.tls import peer_common_name
from ..base import FieldSpec, PrinterBase
from ..registry import register
from . import parse, thumbnails

log = logging.getLogger(__name__)

PUSHALL = json.dumps({"pushing": {"sequence_id": "1", "command": "pushall", "version": 1, "push_target": 1}})
GET_VERSION = json.dumps({"info": {"sequence_id": "2", "command": "get_version"}})
_THUMB_LIMIT = 2                             # at most 2 printers downloading over FTPS at once
_thumb_slots: dict[int, asyncio.Semaphore] = {}


def _slots() -> asyncio.Semaphore:
    loop_id = id(asyncio.get_running_loop())
    if loop_id not in _thumb_slots:
        _thumb_slots.clear()
        _thumb_slots[loop_id] = asyncio.Semaphore(_THUMB_LIMIT)
    return _thumb_slots[loop_id]


class BambuPrinter(PrinterBase):
    error_model: ClassVar[Optional[str]] = None   # model name in the error-text table (printer-specific texts)
    account_hours_unit_s: ClassVar[Optional[int]] = None   # unit of the account's total_print_time; None = unknown, not used
    family = "bambu"
    maker = "Bambu Lab"
    mode = "live"
    abstract = True
    fields = [
        FieldSpec(key="host", label="IP address"),
        FieldSpec(key="access_code", label="LAN access code", kind="secret",
                  help="Printer screen: Settings -> Network/WLAN -> Access Code"),
        FieldSpec(key="serial", label="Serial number", required=False,
                  help="Optional - read from the printer's certificate if left blank"),
    ]

    # ---- per-model differences (children override) ----
    full_reports: ClassVar[bool] = False         # X1 pushes full state; P1/A1 push deltas
    pushall_min_interval: ClassVar[float] = 300  # P1P lags if asked more often than every 5 min
    has_chamber_sensor: ClassVar[bool] = False
    has_aux_fan: ClassVar[bool] = True
    has_chamber_fan: ClassVar[bool] = True
    ssdp_model_codes: ClassVar[tuple[str, ...]] = ()   # DevModel.bambu.com values

    mqtt_port: ClassVar[int] = 8883
    ftps_port: ClassVar[int] = 990

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._state: dict = {}
        self._version: Optional[dict] = None
        self._serial: Optional[str] = self.config.serial
        self._thumb_key: Optional[str] = None        # job key the current thumbnail belongs to
        self._thumb_url: Optional[str] = None
        self._thumb_source: Optional[str] = None
        self._thumb_task: Optional[asyncio.Task] = None
        self._thumb_retry_at: float = 0.0
        self._thumb_done: set[str] = set()
        self._via_cloud = False
        self._local_down_since: Optional[float] = None
        self._link: Optional[str] = None

    # ---------------------------------------------------------------- session
    # Connection (Details): "auto" (default) = the printer's own MQTT on the LAN; if that fails for
    # CLOUD_AFTER_S and the printer is on the linked Bambu account (and the account says it's online), read
    # the same reports through Bambu's cloud MQTT, and look for it on the LAN again every LOCAL_RETRY_S.
    # "local" / "cloud" pin one path. Read-only either way: only pushall + get_version are ever published.
    CLOUD_AFTER_S: ClassVar[float] = 120
    LOCAL_RETRY_S: ClassVar[float] = 600

    def _conn_mode(self) -> str:
        m = str(self.setting("connection", "auto") or "auto")
        return m if m in ("auto", "local", "cloud") else "auto"

    async def _session(self) -> None:
        mode = self._conn_mode()
        if mode == "local":
            self._via_cloud = False
            await self._local_session()
            return
        if mode == "cloud":
            self._via_cloud = True
            await self._cloud_session(check_local=False)
            return
        while True:                                          # auto
            if not self._via_cloud:
                try:
                    await self._local_session()
                    return
                except PrinterUnreachable:
                    now = time.monotonic()
                    self._local_down_since = self._local_down_since or now
                    if now - self._local_down_since < self.CLOUD_AFTER_S:
                        raise
                    why = await asyncio.to_thread(self._cloud_unavailable)
                    if why:
                        log.info("printer %s: not on the LAN and no cloud fallback (%s)", self.config.id, why)
                        raise
                    log.info("printer %s: not on the LAN for %.0f s - switching to the Bambu cloud",
                             self.config.id, now - self._local_down_since)
                    self._via_cloud = True
            try:
                back = await self._cloud_session(check_local=True)
            except DriverError:
                self._via_cloud = False                      # next round: LAN first again
                raise
            if not back:
                self._via_cloud = False
                return
            log.info("printer %s: back on the LAN - switching to local", self.config.id)
            self._via_cloud, self._local_down_since = False, None

    def _account_access_code(self, rejected: str) -> Optional[str]:
        """The printer's current LAN access code from the linked Bambu account, if it differs from the rejected one."""
        acct = bambu_cloud.get_account()
        if not acct or acct.state != "linked" or not self._serial:
            return None
        now = time.monotonic()
        if now - getattr(self, "_code_checked_at", -1e9) < 300:     # ask the account at most every 5 minutes
            return None
        self._code_checked_at = now
        dev = acct.device(self._serial, fresh=True)
        code = str((dev or {}).get("dev_access_code") or "").strip()
        return code if code and code != rejected else None

    def _cloud_unavailable(self) -> Optional[str]:
        """None if the cloud path can be used now, else why not (blocking)."""
        acct = bambu_cloud.get_account()
        if not acct or acct.state != "linked":
            return "no linked Bambu account"
        if not self._serial:
            return "serial unknown"
        dev = acct.device(self._serial)
        if dev is None:
            return "not on the linked Bambu account"
        if dev.get("online") is False:
            return "the Bambu account says it is offline"
        return None

    async def _local_session(self) -> None:
        code = self.secret("access_code")
        if not code:
            raise AuthFailed("no LAN access code set")
        host = self.config.host
        port = int(self.setting("mqtt_port", self.mqtt_port))
        if not self._serial:
            self._serial = await asyncio.to_thread(peer_common_name, host, port)
            if not self._serial:
                raise DriverError("could not read the serial from the printer certificate - enter it manually")
            self.learned_serial = self._serial
        conn = MqttConnection(host, port, "bblp", code)
        try:
            await conn.connect()
        except AuthFailed:
            # The LAN access code changes when the printer is set up again (new Wi-Fi, re-added in Bambu Handy).
            # The linked Bambu account knows the current one: take it, and keep it if the printer accepts it.
            fresh = await asyncio.to_thread(self._account_access_code, code)
            if not fresh:
                raise
            log.info("printer %s: access code rejected - trying the current one from the Bambu account", self.config.id)
            conn = MqttConnection(host, port, "bblp", fresh)
            await conn.connect()                             # AuthFailed again: the account's code is wrong too
            self.learned_access_code = fresh                 # the manager saves it
            log.info("printer %s: access code updated from the Bambu account", self.config.id)
        self._local_down_since = None
        self._link = "local"
        try:
            await self._pump(conn)
        finally:
            await conn.close()

    async def _cloud_session(self, check_local: bool) -> bool:
        """Read through Bambu's cloud. Returns True when the printer answers on the LAN again (auto only)."""
        acct = bambu_cloud.get_account()
        if not acct or acct.state != "linked":
            raise AuthFailed("cloud connection needs a linked Bambu account (Settings -> Bambu account)")
        if not self._serial:
            raise DriverError("serial number unknown - enter it in Details to use the cloud connection")
        try:
            host, port, user, token = await asyncio.to_thread(acct.mqtt_login)
        except bambu_cloud.TokenExpired:
            raise AuthFailed("Bambu account needs to be linked again (Settings -> Bambu account)") from None
        except bambu_cloud.CloudError as e:
            raise PrinterUnreachable(f"Bambu cloud: {e}") from None
        ctx = None if acct.mqtt_host_override else ssl.create_default_context()   # cloud: real certificate
        conn = MqttConnection(host, port, user, token, ssl_context=ctx)
        try:
            await conn.connect()
        except AuthFailed:
            raise AuthFailed("Bambu cloud refused the account login - link the account again") from None
        self._link = "cloud"
        try:
            return await self._pump(conn, check_local=check_local)
        finally:
            await conn.close()

    def _lan_answers(self) -> bool:
        try:
            with socket.create_connection((self.config.host, int(self.setting("mqtt_port", self.mqtt_port))), 3):
                return True
        except OSError:
            return False

    async def _pump(self, conn: MqttConnection, check_local: bool = False) -> bool:
        """Subscribe, ask for a full report, then process reports until the connection drops.
        With check_local (cloud path in auto mode): returns True once the printer answers on the LAN."""
        conn.subscribe(f"device/{self._serial}/report")
        conn.publish(f"device/{self._serial}/request", PUSHALL)
        conn.publish(f"device/{self._serial}/request", GET_VERSION)
        last_pushall = last_lan_check = time.monotonic()
        idle_timeout = 90.0 if self.full_reports else self.pushall_min_interval + 90.0
        async for _topic, payload in conn.messages(idle_timeout):
            try:
                data = json.loads(payload)
            except (ValueError, UnicodeDecodeError):
                continue
            if isinstance(data.get("print"), dict):
                if parse.merge_report(self._state, data["print"]):
                    self._thumb_url = None
            if isinstance(data.get("info"), dict) and data["info"].get("command") == "get_version":
                self._version = data["info"]
            await self._publish_state()
            self._maybe_fetch_thumbnail()
            if not self.full_reports and time.monotonic() - last_pushall > self.pushall_min_interval:
                conn.publish(f"device/{self._serial}/request", PUSHALL)
                last_pushall = time.monotonic()
            if check_local and time.monotonic() - last_lan_check > self.LOCAL_RETRY_S:
                last_lan_check = time.monotonic()
                if await asyncio.to_thread(self._lan_answers):
                    return True
        return False

    async def _publish_state(self) -> None:
        if not self._state.get("gcode_state") and not self._state.get("nozzle_temper"):
            return                                   # wait for the first full report
        st = parse.to_status(self.config.id, self._state, self._version,
                             has_chamber_sensor=self.has_chamber_sensor, has_aux_fan=self.has_aux_fan,
                             has_chamber_fan=self.has_chamber_fan, model=self.error_model)
        st.connection, st.connection_detail = ConnState.ONLINE, None
        st.link = self._link
        seq = str(self._state.get("sequence_id") or "")
        st.report_seq = int(seq) if seq.isdigit() else None
        if self._thumb_url and self._thumb_key == self._job_key():
            st.job.thumbnail_url = self._thumb_url
            st.job.thumbnail_source = self._thumb_source
        await self._publish(st)

    # ---------------------------------------------------------------- thumbnails
    def _job_key(self) -> Optional[str]:
        s = self._state
        parts = [str(s.get(k) or "") for k in ("task_id", "subtask_name", "gcode_file", "plate_idx")]
        return hashlib.sha1("|".join(parts).encode()).hexdigest()[:16] if any(parts[:3]) else None

    def _maybe_fetch_thumbnail(self) -> None:
        if self.setting("thumbnails", True) is False:
            return
        state = self.status.state
        key = self._job_key()
        if key is None or state in (JobState.IDLE, JobState.UNKNOWN):
            return
        if state == JobState.PREPARING and str(self._state.get("gcode_file_prepare_percent", "100")) != "100":
            return                                   # file still downloading to the printer
        if key in self._thumb_done or time.monotonic() < self._thumb_retry_at:
            return
        if self._thumb_task and not self._thumb_task.done():
            return
        self._thumb_task = asyncio.create_task(self._fetch_thumbnail(key))

    cloud_retry_s: ClassVar[float] = 30        # cloud task can lag the job start a little
    cloud_tries: ClassVar[int] = 4

    async def _fetch_thumbnail(self, key: str) -> None:
        s = self._state
        acct = bambu_cloud.get_account()
        if acct and acct.state == "linked" and s.get("print_type") == "cloud":
            for n in range(1, self.cloud_tries + 1):
                data, how = await asyncio.to_thread(acct.cover_for, self._serial, s.get("task_id"),
                                                    s.get("subtask_name"))
                if data:
                    await self._store_thumbnail(key, data, how, "cloud")
                    return
                log.info("printer %s: cloud cover not available (%s), try %d/%d", self.config.id, how, n,
                         self.cloud_tries)
                if acct.state != "linked" or n == self.cloud_tries:
                    break
                await asyncio.sleep(self.cloud_retry_s)
                if key != self._job_key():
                    return                                 # job changed while waiting
            log.info("printer %s: falling back to the file on the printer (best effort)", self.config.id)
        if self._link == "cloud":                          # not on the LAN: the printer's files can't be read
            self._thumb_retry_at = time.monotonic() + 600
            return
        async with _slots():
            try:
                data, how = await asyncio.to_thread(
                    thumbnails.fetch_thumbnail, self.config.host, self.secret("access_code") or "",
                    s.get("gcode_file"), s.get("subtask_name"), self.status.job.plate,
                    s.get("print_type") == "cloud", self.setting("ranged_ftp", True),
                    int(self.setting("ftps_port", self.ftps_port)))
            except Exception as e:  # noqa: BLE001 - network hiccup: try again in 2 minutes
                log.info("printer %s: thumbnail fetch failed: %s", self.config.id, e)
                self._thumb_retry_at = time.monotonic() + 120
                return
        await self._store_thumbnail(key, data, how, "printer")

    async def _store_thumbnail(self, key: str, data: Optional[bytes], how: str, source: str) -> None:
        self._thumb_done.add(key)
        log.info("printer %s: thumbnail %s", self.config.id, how)
        if data and key == self._job_key():
            config.THUMB_DIR.mkdir(parents=True, exist_ok=True)
            (config.THUMB_DIR / f"{self.config.id}.png").write_bytes(data)   # may be PNG/JPEG/WebP
            self._thumb_key, self._thumb_source = key, source
            self._thumb_url = f"/api/printers/{self.config.id}/thumbnail?k={key}"
            await self._publish_state()


@register
class BambuX1C(BambuPrinter):
    driver_key = "bambu.x1c"
    error_model = "X1C"
    account_hours_unit_s = 1     # account total_print_time in seconds (verified 2026-10-30: 8,345 h on screen)
    model_name = "X1 Carbon"
    full_reports = True
    pushall_min_interval = 60
    has_chamber_sensor = True        # fw 01.12: device.ctc.info.temp (chamber_temper removed)
    ssdp_model_codes = ("BL-P001",)  # verified on a real X1C


@register
class BambuP1P(BambuPrinter):
    driver_key = "bambu.p1p"
    error_model = "P1P"
    account_hours_unit_s = 60    # minutes (verified 2026-10-30: P1 718,664 = 11,978 h on screen)
    model_name = "P1P"
    ssdp_model_codes = ("C11",)      # verified on P1 / P2


@register
class BambuA1(BambuPrinter):
    driver_key = "bambu.a1"
    error_model = "A1"
    account_hours_unit_s = 60    # minutes (verified 2026-10-30: A1 368,263 = 6,137 h on screen)
    model_name = "A1"
    has_aux_fan = False
    has_chamber_fan = False
    ssdp_model_codes = ("N2S",)      # verified on A1-A


@register
class BambuA1Mini(BambuPrinter):
    driver_key = "bambu.a1mini"
    error_model = "A1MINI"
    account_hours_unit_s = 60    # minutes (A1 family; 250,001 = 4,167 h - screen not checked)
    model_name = "A1 Mini"
    has_aux_fan = False
    has_chamber_fan = False
    ssdp_model_codes = ("N1",)       # verified on Mini-Demo
