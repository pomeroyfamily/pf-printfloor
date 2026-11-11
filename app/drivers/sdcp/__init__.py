"""SDCP v3 family (Elegoo / ChiTu): UDP discovery (M99999 -> 3000), WebSocket :3030.

Poll mode (decision O6): the printer allows only 5 WebSocket clients and open slicers hold
slots, so every poll_interval we connect -> read -> disconnect, never holding a slot. A refused
connection ("500 too many client") shows the card as Busy and retries.
Read-only: only SDCP read commands are ever sent (0 status, 1 attributes, 320/321 print history, 324 Canvas).
Real-hardware findings: docs/SPEC.md section 5.4.
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
import socket
import time
import urllib.error
import urllib.request
import uuid
from typing import ClassVar, Optional

from ... import config
from ...errors import DriverError, PrinterUnreachable
from ...models import JobState
from ...transports.websocket import WebSocket
from ..base import FieldSpec, PrinterBase
from ..registry import register
from . import parse

log = logging.getLogger(__name__)
READ_CMDS = {0, 1, 320, 321, 324}   # 320/321 = print history ids / details


def udp_discover(host: str, port: int = 3000, timeout: float = 2.0) -> Optional[dict]:
    """Unicast M99999 to one printer (verified on a real CC: unicast works, Docker-friendly)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.sendto(b"M99999", (host, port))
        data, _ = s.recvfrom(4096)
        d = json.loads(data.decode("utf-8", "replace"))
        return d.get("Data", d)
    except (OSError, ValueError):
        return None
    finally:
        s.close()


def request(cmd: int, mainboard: str, data: Optional[dict] = None) -> str:
    assert cmd in READ_CMDS, "this dashboard only sends SDCP read commands"
    return json.dumps({"Id": uuid.uuid4().hex,
                       "Data": {"Cmd": cmd, "Data": data or {}, "RequestID": secrets.token_hex(8),
                                "MainboardID": mainboard, "TimeStamp": int(time.time() * 1000), "From": 1},
                       "Topic": f"sdcp/request/{mainboard}"})


class SDCPPrinter(PrinterBase):
    family = "sdcp"
    maker = "Elegoo"
    mode = "poll"
    default_poll_interval = 15.0
    abstract = True
    fields = [
        FieldSpec(key="host", label="IP address"),
        FieldSpec(key="poll_interval", label="Refresh every (seconds)", kind="int", required=False,
                  default=15, help="Connects briefly each time, so slicers keep their connection slots"),
    ]
    ws_port: ClassVar[int] = 3030
    udp_port: ClassVar[int] = 3000
    http_port: ClassVar[int] = 80
    supports_canvas: ClassVar[bool] = False
    canvas_refresh_s: ClassVar[float] = 300       # re-read the Canvas at most every 5 min while idle
    # While printing: every 2 min. Tested mid-print on a real CC 2026-10-30 (cc_probe --canvas-while-printing):
    # answered with active_tray_id 0 (black, the tray in use, tray status 2), print unaffected.
    canvas_refresh_printing_s: ClassVar[float] = 120
    thumb_retry_s: ClassVar[float] = 60
    history_sync_s: ClassVar[float] = 600         # read the printer's own job history at most every 10 min (idle only)

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._mainboard: Optional[str] = self.config.serial or self.setting("mainboard_id")
        self._canvas: Optional[dict] = None
        self._canvas_at: Optional[float] = None       # monotonic time of the last Canvas read
        self._canvas_mode: Optional[str] = None       # "idle" | "printing": printer state at that read
        self._canvas_off: Optional[str] = None       # reason, if the printer misbehaved after Cmd 324
        self._thumb_task_id: Optional[str] = None
        self._thumb_url: Optional[str] = None
        self._thumb_next = 0.0
        self._last_status: Optional[dict] = None
        self._canvas_off_logged = False
        self._hist_at: Optional[float] = None
        self._hist_seen: set[str] = set()           # history task ids already handed to the dashboard
        self.pending_jobs: list[dict] = []           # finished jobs from the printer's history, for the manager

    # ------------------------------------------------------------------ one read (blocking)
    def _want_canvas(self, last: Optional[dict]) -> bool:
        if not self.supports_canvas or self._canvas_off or self.setting("canvas", True) is False:
            return False
        if not last or last.get("AmsConnectStatus") != 1:
            return False
        mode = self._mode(last)
        if mode == "printing" and self.setting("canvas_while_printing", True) is False:
            return False
        if mode not in ("idle", "printing"):
            return False                                # file transfer / calibrating: leave it alone
        if self._canvas_at is None or self._canvas_mode != mode:
            return True                                 # never read, or state changed (idle <-> printing)
        every = self.canvas_refresh_s if mode == "idle" else self.canvas_refresh_printing_s
        return time.monotonic() - self._canvas_at > every

    @staticmethod
    def _mode(status: dict) -> Optional[str]:
        cs = status.get("CurrentStatus") or status.get("CurrenStatus") or [0]
        return {0: "idle", 1: "printing"}.get(cs[0] if cs else 0)

    def _want_history(self, last: Optional[dict]) -> bool:
        if not last or self.setting("history", True) is False:
            return False
        idle = (last.get("CurrentStatus") or last.get("CurrenStatus") or [0]) == [0]
        return idle and (self._hist_at is None or time.monotonic() - self._hist_at > self.history_sync_s)

    def _read(self, ask_canvas: bool, ask_history: bool = False) -> tuple[dict, dict, Optional[dict], Optional[list]]:
        host = self.config.host
        if not self._mainboard:
            d = udp_discover(host, int(self.setting("udp_port", self.udp_port)))
            if d and d.get("MainboardID"):
                self._mainboard = d["MainboardID"]
        ws = WebSocket(host, int(self.setting("ws_port", self.ws_port)))
        status: dict = {}
        attrs: dict = {}
        canvas: Optional[dict] = None
        canvas_sent = False
        need_canvas: Optional[bool] = None             # decided once the first status arrives
        hist: Optional[list] = None                    # Cmd 321 details (None = not read)
        hist_state = "off"                             # off | ids | details | done
        try:
            ws.settimeout(1.0)
            sent = False
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                if self._mainboard and not sent:
                    ws.send_text(request(1, self._mainboard))
                    ws.send_text(request(0, self._mainboard))
                    sent = True
                if status and attrs:
                    if need_canvas is None:            # only while idle with the Canvas attached
                        need_canvas = ask_canvas and status.get("AmsConnectStatus") == 1 \
                            and self._mode(status) in ("idle", "printing")
                    if need_canvas and not canvas_sent:
                        ws.send_text(request(324, self._mainboard))
                        canvas_sent, deadline = True, time.monotonic() + 4
                    canvas_done = not need_canvas or canvas is not None
                    idle_now = (status.get("CurrentStatus") or status.get("CurrenStatus") or [0]) == [0]
                    if canvas_done and ask_history and idle_now and hist_state == "off":
                        ws.send_text(request(320, self._mainboard))
                        hist_state, deadline = "ids", time.monotonic() + 4
                    if canvas_done and hist_state in ("off", "done"):
                        break
                try:
                    txt = ws.recv_text()
                except socket.timeout:
                    continue
                except (ConnectionError, OSError) as e:
                    if canvas_sent:
                        self._canvas_off = "printer dropped the connection after the Canvas request"
                        break                              # status already in hand: keep this poll
                    raise PrinterUnreachable(f"connection lost ({e})") from e
                if txt is None:
                    if canvas_sent:
                        self._canvas_off = "printer closed the connection after the Canvas request"
                    break
                if txt == "pong":
                    continue
                try:
                    msg = json.loads(txt)
                except ValueError:
                    continue
                topic = msg.get("Topic", "")
                mb = msg.get("MainboardID") or (msg.get("Attributes") or {}).get("MainboardID") \
                    or (msg.get("Data") or {}).get("MainboardID")
                if mb and not self._mainboard:
                    self._mainboard = mb
                if "Status" in msg or "sdcp/status" in topic:
                    status.update(msg.get("Status") or (msg.get("Data") or {}).get("Status") or {})
                if "Attributes" in msg or "sdcp/attributes" in topic:
                    attrs.update(msg.get("Attributes") or (msg.get("Data") or {}).get("Attributes") or {})
                if "sdcp/response" in topic:
                    d = msg.get("Data") or {}
                    if d.get("Cmd") == 324:
                        canvas = d.get("Data") or {}
                    elif d.get("Cmd") == 320 and hist_state == "ids":
                        ids = [i for i in ((d.get("Data") or {}).get("HistoryData") or []) if isinstance(i, str)]
                        new = [i for i in ids if i not in self._hist_seen][:20]
                        if new:
                            ws.send_text(request(321, self._mainboard, {"Id": new}))
                            hist_state, deadline = "details", time.monotonic() + 6
                        else:
                            hist, hist_state = [], "done"
                    elif d.get("Cmd") == 321 and hist_state == "details":
                        hist = (d.get("Data") or {}).get("HistoryDetailList") or []
                        hist_state = "done"
        finally:
            ws.close()
        if canvas_sent and canvas is None and not self._canvas_off:
            self._canvas_off = "no reply to the Canvas request"
        if not status:
            raise DriverError("printer sent no status" + ("" if self._mainboard else " (Mainboard ID unknown)"))
        return status, attrs, canvas, hist

    # ------------------------------------------------------------------ poll
    async def _poll_once(self) -> None:
        ask_hist = self._want_history(self._last_status)
        status, attrs, canvas, hist = await asyncio.to_thread(
            self._read, self._want_canvas(self._last_status), ask_hist)
        self._last_status = status
        if ask_hist:
            self._hist_at = time.monotonic()               # asked (answered or not): wait for the next window
        if hist:
            items = parse.history_items(hist)
            self._hist_seen.update(d.get("TaskId") for d in hist if d.get("TaskId"))
            self.pending_jobs.extend(items)
            log.info("printer %s: read %d job(s) from the printer's history", self.config.id, len(items))
        if canvas is not None:
            self._canvas, self._canvas_at, self._canvas_mode = canvas, time.monotonic(), self._mode(status)
            log.info("printer %s: Canvas read (%d unit(s))", self.config.id, len(canvas.get("canvas_list") or []))
        if self._canvas_off and not self._canvas_off_logged:
            log.warning("printer %s: stopped asking for Canvas data: %s", self.config.id, self._canvas_off)
            self._canvas_off_logged = True
        if self._mainboard and not self.config.serial:
            self.learned_serial = self._mainboard          # MainboardID = this printer's serial
        if status.get("AmsConnectStatus") != 1:
            self._canvas = None                             # Canvas unplugged
        st = parse.to_status(self.config.id, status, attrs, self._canvas)
        stale = self._canvas_mode != "printing" or self._canvas_at is None or \
            time.monotonic() - self._canvas_at > 2 * self.canvas_refresh_printing_s + 60
        if self._canvas is not None and st.state != JobState.IDLE and stale:
            for u in st.filament:                           # read while idle (or too old): active tray unknown
                for s in u.slots:
                    s.active = False
        await self._maybe_thumbnail(status)
        if self._thumb_url and self._thumb_task_id == (status.get("PrintInfo") or {}).get("TaskId"):
            st.job.thumbnail_url, st.job.thumbnail_source = self._thumb_url, "printer"
        await self._publish(st)

    # ------------------------------------------------------------------ thumbnails
    async def _maybe_thumbnail(self, status: dict) -> None:
        task = (status.get("PrintInfo") or {}).get("TaskId")
        if not task or not (status.get("PrintInfo") or {}).get("Filename"):
            return
        if task == self._thumb_task_id and (self._thumb_url or time.monotonic() < self._thumb_next):
            return
        if task != self._thumb_task_id:
            self._thumb_task_id, self._thumb_url = task, None
        port = int(self.setting("http_port", self.http_port))
        url = f"http://{self.config.host}" + ("" if port == 80 else f":{port}") + \
            f"/board-resource/history_image/{task}.png"
        try:
            data = await asyncio.to_thread(_download, url)
        except Exception as e:  # noqa: BLE001 - not there yet (appears partway through a job)
            log.info("printer %s: thumbnail not available yet (%s)", self.config.id, e)
            self._thumb_next = time.monotonic() + self.thumb_retry_s
            return
        config.THUMB_DIR.mkdir(parents=True, exist_ok=True)
        (config.THUMB_DIR / f"{self.config.id}.png").write_bytes(data)
        self._thumb_url = f"/api/printers/{self.config.id}/thumbnail?k={task[:12]}"
        log.info("printer %s: thumbnail for task %s", self.config.id, task)


def _download(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=8) as r:
        data = r.read(5 * 1024 * 1024)
    if data[:8] != b"\x89PNG\r\n\x1a\n" and data[:3] != b"\xff\xd8\xff":
        raise ValueError("not an image")
    return data


@register
class ElegooCentauriCarbon(SDCPPrinter):
    driver_key = "elegoo.cc1"
    model_name = "Centauri Carbon"
    supports_canvas = True        # Cmd 324 (Elegoo SDK); asked only while idle - see docs/SPEC.md 5.4
    machine_names: ClassVar[tuple[str, ...]] = ("Centauri Carbon",)
