"""PrinterBase: lifecycle, reconnect/backoff, live vs poll mode, status publishing.

Class rule (spec D16): printers that share a protocol get a family base class
(BambuPrinter, KlipperPrinter, SDCPPrinter) with one child class per specific
printer. Shared transports (MQTT, WebSocket, HTTP) are helpers, not parents.

A family class implements ONE of:
  * live mode : ``_session()`` - connect, stream updates via ``_publish()``, return/raise on disconnect
  * poll mode : ``_poll_once()`` - connect, read, disconnect; called every ``poll_interval`` seconds
"""
from __future__ import annotations

import asyncio
import logging
from abc import ABC
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, ClassVar, Literal, Optional

from pydantic import BaseModel, Field

from ..errors import AuthFailed, DriverError, PrinterBusy, PrinterUnreachable  # noqa: F401 (re-exported)
from ..models import ConnState, PrinterStatus
from ..store import PrinterConfig

log = logging.getLogger(__name__)

StatusCallback = Callable[[PrinterStatus], Awaitable[None] | None]


# --------------------------------------------------------------------------- UI metadata
class FieldSpec(BaseModel):
    key: str
    label: str
    kind: Literal["text", "secret", "int"] = "text"
    required: bool = True
    default: Any = None
    help: Optional[str] = None


class DiscoveredPrinter(BaseModel):
    driver: str                     # registry key of the matching child class
    name: Optional[str] = None      # friendly name if the printer announces it
    host: str
    serial: Optional[str] = None
    model: Optional[str] = None
    firmware: Optional[str] = None
    extra: dict[str, Any] = Field(default_factory=dict)
    added_id: Optional[str] = None  # filled in by discovery service when already configured


# --------------------------------------------------------------------------- base class
class PrinterBase(ABC):
    # ---- set by family / child classes ----
    driver_key: ClassVar[str] = ""          # registry key, e.g. "bambu.x1c" (children only)
    family: ClassVar[str] = ""              # "bambu", "klipper", "sdcp"
    maker: ClassVar[str] = ""               # UI: "Bambu Lab", "Snapmaker", "Elegoo", "Klipper (generic)"
    model_name: ClassVar[str] = ""          # UI: "X1 Carbon", "U1", "Centauri Carbon"
    fields: ClassVar[list[FieldSpec]] = []  # connection fields shown in the add form
    mode: ClassVar[Literal["live", "poll"]] = "live"
    default_poll_interval: ClassVar[float] = 20.0
    abstract: ClassVar[bool] = True         # families stay True; concrete children set False
    available: ClassVar[bool] = True        # False while a family's driver is not built yet

    # reconnect backoff (seconds) for live mode / after failures in poll mode
    backoff_steps: ClassVar[tuple[float, ...]] = (2, 5, 10, 30, 60)
    busy_retry: ClassVar[float] = 45.0      # slot-limit refusals retry slower

    def __init__(self, config: PrinterConfig, on_update: Optional[StatusCallback] = None):
        self.config = config
        self.on_update = on_update
        self.status = PrinterStatus(printer_id=config.id)
        self._task: Optional[asyncio.Task] = None
        self._stopping = asyncio.Event()
        self._failures = 0
        self.learned_serial: Optional[str] = None   # set when the driver reads it from the printer
        self.learned_access_code: Optional[str] = None   # Bambu: new LAN code taken from the account (manager saves it)

    # ---------------------------------------------------------------- helpers for subclasses
    @property
    def poll_interval(self) -> float:
        return float(self.config.settings.get("poll_interval", self.default_poll_interval))

    def setting(self, key: str, default: Any = None) -> Any:
        return self.config.settings.get(key, default)

    def secret(self, key: str) -> Optional[str]:
        return self.config.secrets.get(key)

    async def _publish(self, status: Optional[PrinterStatus] = None) -> None:
        """Push a new status to the UI. Drivers call this whenever data changes."""
        if status is not None:
            seen = self.status.last_seen_at
            self.status = status
            if self.status.last_seen_at is None:
                self.status.last_seen_at = seen
        self.status.updated_at = datetime.now(timezone.utc)
        if self.status.connection == ConnState.ONLINE:
            self.status.last_seen_at = self.status.updated_at
        if self.on_update:
            res = self.on_update(self.status)
            if asyncio.iscoroutine(res):
                await res

    async def _set_conn(self, state: ConnState, detail: Optional[str] = None) -> None:
        if self.status.connection != state or self.status.connection_detail != detail:
            self.status.connection = state
            self.status.connection_detail = detail
            await self._publish()

    # ---------------------------------------------------------------- to implement
    async def _session(self) -> None:
        """Live mode: run one connected session until disconnect. Raise DriverError subclasses."""
        raise NotImplementedError

    async def _poll_once(self) -> None:
        """Poll mode: connect, read status, publish, disconnect."""
        raise NotImplementedError

    @classmethod
    async def discover(cls, timeout: float = 5.0) -> list[DiscoveredPrinter]:
        """Family classes override; returns printers found on the LAN."""
        return []

    # ---------------------------------------------------------------- lifecycle
    def start(self) -> asyncio.Task:
        if self._task is None or self._task.done():
            self._stopping.clear()
            self._task = asyncio.create_task(self._run(), name=f"printer-{self.config.id}")
        return self._task

    async def stop(self) -> None:
        self._stopping.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._task = None

    async def _sleep(self, seconds: float) -> bool:
        """Sleep unless stopping; returns False if stop() was requested."""
        try:
            await asyncio.wait_for(self._stopping.wait(), timeout=seconds)
            return False
        except asyncio.TimeoutError:
            return True

    def _backoff(self) -> float:
        i = min(self._failures, len(self.backoff_steps)) - 1
        return self.backoff_steps[max(i, 0)]

    async def _run(self) -> None:
        self.status.connection, self.status.connection_detail = ConnState.CONNECTING, None
        await self._publish()  # always announce the card, even before the first connect
        while not self._stopping.is_set():
            try:
                if self.mode == "poll":
                    await self._poll_once()
                    self._failures = 0
                    if self.status.connection != ConnState.ONLINE:
                        await self._set_conn(ConnState.ONLINE)
                    wait = self.poll_interval
                else:
                    await self._session()          # returns when the session ends cleanly
                    self._failures = 0
                    wait = self.backoff_steps[0]
                    await self._set_conn(ConnState.CONNECTING, "reconnecting")
            except asyncio.CancelledError:
                raise
            except PrinterBusy as e:
                self._failures += 1
                await self._set_conn(ConnState.BUSY, str(e) or "connection slots full - retrying")
                wait = self.busy_retry
            except DriverError as e:
                self._failures += 1
                await self._set_conn(e.conn_state, str(e) or None)
                wait = self._backoff() * (3 if isinstance(e, AuthFailed) else 1)
            except Exception as e:  # noqa: BLE001 - never let one printer kill the loop
                self._failures += 1
                log.exception("printer %s: unexpected driver error", self.config.id)
                await self._set_conn(ConnState.ERROR, f"{type(e).__name__}: {e}")
                wait = self._backoff()
            if not await self._sleep(wait):
                break
