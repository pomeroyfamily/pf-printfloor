"""Printer cameras: one upstream connection per printer, shared by every viewer, open only while watched.

Sources (measured on real printers 2026-10-30, docs/SPEC.md "Camera probe results"):
  * Bambu P1/A1   - TLS :6000, 80-byte auth (bblp + access code), 16-byte header + JPEG.        ~0.5 fps idle A1
  * Snapmaker U1  - extended firmware: /webcam/snapshot.jpg (no wake).
                    stock: Moonraker JSON-RPC camera.start_monitor every 5 s + /server/files/camera/monitor.jpg. ~3 fps 1080p
  * Elegoo CC     - SDCP Cmd 386 {"Enable":1} -> VideoUrl (MJPEG :3031); {"Enable":0} when the last viewer leaves. 9.5 fps
  * External URL  - any MJPEG stream or JPEG snapshot URL (per printer setting `camera_url`), overrides built-in.
  * Bambu X1C     - RTSPS H.264 needs a relay (later): no built-in camera here yet.
Camera "turn on" messages (U1 start_monitor, CC Cmd 386) were approved by the owner; nothing else is ever sent.

Viewers: an MJPEG stream (popup) holds a viewer while connected; snapshot requests (camera wall) take a short lease.
The upstream stops LINGER_S after the last viewer/lease ends.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets as _secrets
import socket
import struct
import threading
import time
import urllib.parse
import urllib.request
import uuid
from typing import Optional

from .store import PrinterConfig
from .transports.tls import insecure_context
from .transports.websocket import WebSocket

log = logging.getLogger(__name__)

LINGER_S = 10.0          # keep the upstream open this long after the last viewer
LEASE_S = 8.0            # a snapshot request keeps the camera "watched" this long
FRAME_TIMEOUT_S = 12.0   # first frame must arrive within this
MAX_JPEG = 8_000_000


class CameraUnavailable(Exception):
    pass


def safe_url(url: str) -> str:
    """URL with any password hidden (for the UI and logs)."""
    try:
        p = urllib.parse.urlsplit(url)
    except ValueError:
        return "?"
    if p.password:
        netloc = f"{p.username}:***@{p.hostname}" + (f":{p.port}" if p.port else "")
        return urllib.parse.urlunsplit(p._replace(netloc=netloc))
    return url


def _opener_for(url: str):
    """urllib opener that handles user:pass@ in the URL (basic/digest)."""
    p = urllib.parse.urlsplit(url)
    if not p.username:
        return urllib.request.build_opener(), url
    clean = urllib.parse.urlunsplit(p._replace(netloc=p.hostname + (f":{p.port}" if p.port else "")))
    pm = urllib.request.HTTPPasswordMgrWithDefaultRealm()
    pm.add_password(None, clean, urllib.parse.unquote(p.username), urllib.parse.unquote(p.password or ""))
    return urllib.request.build_opener(urllib.request.HTTPBasicAuthHandler(pm),
                                       urllib.request.HTTPDigestAuthHandler(pm)), clean


# ====================================================================== sources (blocking, run in a thread)
class Source:
    """Runs in its own thread; calls emit(jpeg) for each new frame until stop is set."""
    label = "camera"

    def __init__(self, cfg: PrinterConfig):
        self.cfg = cfg

    def run(self, emit, stop: threading.Event) -> None:
        raise NotImplementedError

    def close(self) -> None:
        """Called once after run() returns (e.g. CC: turn the stream off)."""


def _read_mjpeg(resp, emit, stop: threading.Event) -> None:
    buf = b""
    while not stop.is_set():
        chunk = resp.read(65536)
        if not chunk:
            raise ConnectionError("camera stream ended")
        buf += chunk
        while True:
            a = buf.find(b"\xff\xd8")
            if a < 0:
                buf = buf[-1:]
                break
            b = buf.find(b"\xff\xd9", a + 2)
            if b < 0:
                buf = buf[a:]
                if len(buf) > MAX_JPEG:
                    buf = b""
                break
            emit(buf[a:b + 2])
            buf = buf[b + 2:]


class MjpegUrl(Source):
    label = "MJPEG stream"

    def __init__(self, cfg, url: str):
        super().__init__(cfg)
        self.url = url

    def run(self, emit, stop):
        opener, url = _opener_for(self.url)
        with opener.open(url, timeout=10) as r:
            _read_mjpeg(r, emit, stop)


class SnapshotUrl(Source):
    """Polls a JPEG URL as fast as it changes (max ~10/s). wake() is called every wake_every seconds if set."""
    label = "snapshots"
    min_gap = 0.1
    wake_every: Optional[float] = None
    parallel = 1             # requests in flight; >1 hides per-request latency (Paxx: ~0.37 s each -> ~2.7 fps alone)

    def __init__(self, cfg, url: str):
        super().__init__(cfg)
        self.url = url

    def wake(self) -> None:
        pass

    def run(self, emit, stop):
        if self.parallel > 1:
            return self._run_parallel(emit, stop)
        opener, url = _opener_for(self.url)
        last_wake, fails = 0.0, 0
        while not stop.is_set():
            if self.wake_every and time.monotonic() - last_wake > self.wake_every:
                last_wake = time.monotonic()
                try:
                    self.wake()
                except Exception as e:  # noqa: BLE001
                    log.info("camera %s: wake failed: %s", self.cfg.id, e)
            t0 = time.monotonic()
            try:
                sep = "&" if "?" in url else "?"
                with opener.open(f"{url}{sep}_={int(time.time() * 1000)}", timeout=6) as r:
                    img = r.read(MAX_JPEG)
                if img[:3] != b"\xff\xd8\xff":
                    raise ValueError("not a JPEG")
                emit(img)
                fails = 0
            except Exception:
                fails += 1
                if fails >= 5:
                    raise
                stop.wait(0.5)
            stop.wait(max(0.0, self.min_gap - (time.monotonic() - t0)))


    def _run_parallel(self, emit, stop):
        """N fetch loops staggered in time; a reply is shown only if its request started after the last one shown,
        so frames never go backwards. Any loop failing 5x in a row ends the run (the feed reconnects)."""
        opener, url = _opener_for(self.url)
        guard = threading.Lock()
        shown = [0.0]
        errors: list[BaseException] = []
        sep = "&" if "?" in url else "?"

        def loop(offset):
            stop.wait(offset)
            fails = 0
            while not stop.is_set() and not errors:
                t0 = time.monotonic()
                try:
                    with opener.open(f"{url}{sep}_={int(time.time() * 1000)}", timeout=6) as r:
                        img = r.read(MAX_JPEG)
                    if img[:3] != b"\xff\xd8\xff":
                        raise ValueError("not a JPEG")
                    with guard:
                        if t0 > shown[0]:
                            shown[0] = t0
                            emit(img)
                    fails = 0
                except Exception as e:  # noqa: BLE001
                    fails += 1
                    if fails >= 5:
                        errors.append(e)
                        return
                    stop.wait(0.5)
                stop.wait(max(0.0, self.min_gap - (time.monotonic() - t0)))
        threads = [threading.Thread(target=loop, args=(i * 0.12,), daemon=True) for i in range(self.parallel)]
        for t in threads:
            t.start()
        while not stop.is_set() and not errors:
            stop.wait(0.5)
        for t in threads:
            t.join(7)
        if errors:
            raise errors[0]


class U1Stock(SnapshotUrl):
    label = "U1 camera (stock firmware)"
    wake_every = 5.0

    def wake(self):
        port = int(self.cfg.settings.get("port") or 80)
        ws = WebSocket(self.cfg.host, port, "/websocket", timeout=5)
        try:
            ws.send_text(json.dumps({"jsonrpc": "2.0", "method": "camera.start_monitor",
                                     "params": {"domain": "lan", "interval": 0}, "id": 1}))
            ws.settimeout(2)
            try:
                ws.recv_text()
            except Exception:  # noqa: BLE001 - reply not needed
                pass
        finally:
            ws.sock.close()


class BambuTls(Source):
    label = "Bambu camera"

    def run(self, emit, stop):
        code = self.cfg.secrets.get("access_code") or ""
        auth = struct.pack("<IIII", 0x40, 0x3000, 0, 0) + b"bblp".ljust(32, b"\0") + \
            code.encode()[:32].ljust(32, b"\0")
        raw = socket.create_connection((self.cfg.host, int(self.cfg.settings.get("camera_port") or 6000)), timeout=10)
        s = insecure_context().wrap_socket(raw, server_hostname=self.cfg.host)
        self._sock = s
        try:
            s.sendall(auth)
            s.settimeout(15)

            def exact(n):
                b = bytearray()
                while len(b) < n:
                    c = s.recv(n - len(b))
                    if not c:
                        raise ConnectionError("printer closed the camera connection")
                    b += c
                return bytes(b)
            while not stop.is_set():
                n = struct.unpack("<I", exact(16)[:4])[0]
                if not 1000 <= n <= MAX_JPEG:
                    raise ValueError(f"bad camera frame length {n} (wrong access code?)")
                emit(exact(n))
        finally:
            s.close()

    def close(self):
        try:
            self._sock.close()
        except Exception:  # noqa: BLE001
            pass


class CentauriCmd386(Source):
    label = "Centauri camera"

    def _call(self, enable: int) -> dict:
        mb = self.cfg.serial or ""
        ws = WebSocket(self.cfg.host, int(self.cfg.settings.get("ws_port") or 3030))
        try:
            ws.settimeout(1)
            ws.send_text(json.dumps({"Id": uuid.uuid4().hex, "Data": {
                "Cmd": 386, "Data": {"Enable": enable}, "RequestID": _secrets.token_hex(8), "MainboardID": mb,
                "TimeStamp": int(time.time() * 1000), "From": 1}, "Topic": f"sdcp/request/{mb}"}))
            deadline = time.monotonic() + 6
            while time.monotonic() < deadline:
                try:
                    txt = ws.recv_text()
                except socket.timeout:
                    continue
                if not txt or txt == "pong":
                    if txt is None:
                        break
                    continue
                d = json.loads(txt).get("Data") or {}
                if d.get("Cmd") == 386:
                    return d.get("Data") or {}
        finally:
            ws.sock.close()
        raise TimeoutError("no reply to the camera request")

    def run(self, emit, stop):
        r = self._call(1)
        url = r.get("VideoUrl")
        if not url:
            why = {1: "the printer's viewer limit is reached (close the camera in Elegoo apps)",
                   2: "the printer reports no camera"}.get(r.get("Ack"), f"printer answered {r}")
            raise CameraUnavailable(why)
        self._on = True
        if not url.startswith("http"):
            url = "http://" + url
        with urllib.request.urlopen(url, timeout=10) as resp:
            _read_mjpeg(resp, emit, stop)

    def close(self):
        if getattr(self, "_on", False):
            try:
                self._call(0)
            except Exception as e:  # noqa: BLE001
                log.info("camera %s: turning the stream off failed: %s", self.cfg.id, e)


class U1Auto(Source):
    """Extended firmware if /webcam/snapshot.jpg answers with a JPEG, else stock monitor.jpg."""
    label = "U1 camera"

    def run(self, emit, stop):
        port = int(self.cfg.settings.get("port") or 80)
        base = f"http://{self.cfg.host}" + ("" if port == 80 else f":{port}")
        try:
            with urllib.request.urlopen(base + "/webcam/snapshot.jpg", timeout=4) as r:
                ext = r.read(3) == b"\xff\xd8\xff"
        except Exception:  # noqa: BLE001
            ext = False
        if ext:
            self.inner = SnapshotUrl(self.cfg, base + "/webcam/snapshot.jpg")
            self.inner.min_gap, self.inner.parallel = 0.3, 3        # 3 in flight -> ~3x the single-request rate
            self.label = "U1 camera (extended firmware)"
            self.player_url = base + "/webcam/player"             # Paxx WebRTC player: smooth, home network only
        else:
            self.inner = U1Stock(self.cfg, base + "/server/files/camera/monitor.jpg")
            self.label = U1Stock.label
        self.inner.run(emit, stop)


def source_for(cfg: PrinterConfig) -> Optional[Source]:
    url = (cfg.settings.get("camera_url") or "").strip()
    if url:
        kind = cfg.settings.get("camera_kind") or "auto"
        if kind == "snapshot":
            return SnapshotUrl(cfg, url)
        if kind == "mjpeg":
            return MjpegUrl(cfg, url)
        return AutoUrl(cfg, url)
    if cfg.settings.get("camera_builtin") is False:
        return None
    d = cfg.driver
    if d in ("bambu.p1p", "bambu.a1", "bambu.a1mini"):
        return BambuTls(cfg)
    if d == "snapmaker.u1":
        return U1Auto(cfg)
    if d.startswith("elegoo.") and cfg.serial:
        return CentauriCmd386(cfg)
    return None


class AutoUrl(Source):
    """External URL: MJPEG if the server answers multipart, else a snapshot to poll."""
    label = "external camera"

    def __init__(self, cfg, url):
        super().__init__(cfg)
        self.url = url

    def run(self, emit, stop):
        opener, url = _opener_for(self.url)
        r = opener.open(url, timeout=10)
        ctype = (r.headers.get("Content-Type") or "").lower()
        if "multipart" in ctype:
            self.label = "external MJPEG stream"
            with r:
                _read_mjpeg(r, emit, stop)
        else:
            r.close()
            self.label = "external snapshots"
            SnapshotUrl(self.cfg, self.url).run(emit, stop)


def builtin_kind(cfg: PrinterConfig) -> Optional[str]:
    """What the UI shows: 'external' | 'builtin' | None (no camera available)."""
    if (cfg.settings.get("camera_url") or "").strip():
        return "external"
    if source_for(cfg) is not None:
        return "builtin"
    return None


# ====================================================================== hub (asyncio side)
class Feed:
    """One printer's camera: upstream thread + latest frame + waiters."""

    def __init__(self, cfg: PrinterConfig, loop: asyncio.AbstractEventLoop):
        self.cfg, self.loop = cfg, loop
        self.frame: Optional[bytes] = None
        self.seq = 0
        self.error: Optional[str] = None
        self.viewers = 0
        self.lease_until = 0.0
        self._src: Optional[Source] = None
        self._cond = asyncio.Condition()
        self._stop: Optional[threading.Event] = None
        self._thread: Optional[threading.Thread] = None
        self._idle_task: Optional[asyncio.Task] = None
        self._last_hash = b""
        self.started_at = 0.0
        self._last_viewer_left = 0.0

    @property
    def label(self) -> str:
        src = self._src
        if src is None:
            return ""
        inner = getattr(src, "inner", None)                 # U1Auto picks stock / extended at run time
        return getattr(inner, "label", None) and src.label or src.label

    @property
    def player_url(self) -> Optional[str]:
        return getattr(self._src, "player_url", None)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _emit(self, img: bytes) -> None:          # thread -> loop
        h = hashlib.md5(img).digest()
        if h == self._last_hash:
            return
        self._last_hash = h
        self.loop.call_soon_threadsafe(self._push, img)

    def _push(self, img: bytes) -> None:
        self.frame, self.seq, self.error = img, self.seq + 1, None

        async def notify():
            async with self._cond:
                self._cond.notify_all()
        asyncio.ensure_future(notify())

    def _fail(self, msg: str) -> None:
        self.error = msg

        async def notify():
            async with self._cond:
                self._cond.notify_all()
        asyncio.ensure_future(notify())

    def ensure_running(self) -> None:
        if self.running:
            return
        src = source_for(self.cfg)
        if src is None:
            raise CameraUnavailable("this printer has no camera the dashboard can show yet")
        stop = threading.Event()
        self._stop, self.error, self.started_at = stop, None, time.monotonic()
        self._src = src

        def worker():
            backoff = 2.0
            while not stop.is_set():
                try:
                    src.run(self._emit, stop)
                    backoff = 2.0
                except CameraUnavailable as e:
                    self.loop.call_soon_threadsafe(self._fail, str(e))
                    stop.wait(15)
                except Exception as e:  # noqa: BLE001 - reconnect while someone is watching
                    if not stop.is_set():
                        log.info("camera %s (%s): %s: %s", self.cfg.id, src.label, type(e).__name__, e)
                        self.loop.call_soon_threadsafe(self._fail, f"{type(e).__name__}: {e}")
                    stop.wait(backoff)
                    backoff = min(backoff * 2, 20)
            try:
                src.close()
            except Exception:  # noqa: BLE001
                pass
            log.info("camera %s: stopped", self.cfg.id)

        self._thread = threading.Thread(target=worker, name=f"camera-{self.cfg.id}", daemon=True)
        self._thread.start()
        log.info("camera %s: started (%s)", self.cfg.id, src.label)
        self._schedule_idle_check()

    def stop(self) -> None:
        if self._stop:
            self._stop.set()
        self._thread = None
        self.frame, self.seq, self._last_hash = None, 0, b""

    def _schedule_idle_check(self) -> None:
        if self._idle_task and not self._idle_task.done():
            return

        async def check():
            while self.running:
                await asyncio.sleep(1)
                now = time.monotonic()
                if self.viewers == 0 and now > self.lease_until and now > self._last_viewer_left + LINGER_S:
                    self.stop()
        self._idle_task = asyncio.ensure_future(check())

    async def next_frame(self, after: int, timeout: float) -> Optional[tuple[int, bytes]]:
        """Frame newer than `after`, waiting up to timeout. None if none arrived."""
        deadline = time.monotonic() + timeout
        async with self._cond:
            while self.seq <= after:
                left = deadline - time.monotonic()
                if left <= 0:
                    return None
                try:
                    await asyncio.wait_for(self._cond.wait(), left)
                except asyncio.TimeoutError:
                    return None
            return self.seq, self.frame  # type: ignore[return-value]

    def add_viewer(self) -> None:
        self.viewers += 1
        self.ensure_running()

    def remove_viewer(self) -> None:
        self.viewers = max(0, self.viewers - 1)
        if self.viewers == 0:
            self._last_viewer_left = time.monotonic()

    def lease(self) -> None:
        self.lease_until = time.monotonic() + LEASE_S
        self.ensure_running()


class Cameras:
    def __init__(self):
        self.feeds: dict[str, Feed] = {}

    def feed(self, cfg: PrinterConfig) -> Feed:
        f = self.feeds.get(cfg.id)
        if f is None or _cam_sig(f.cfg) != _cam_sig(cfg):
            if f:
                f.stop()
            f = Feed(cfg, asyncio.get_running_loop())
            self.feeds[cfg.id] = f
        else:
            f.cfg = cfg
        return f

    def forget(self, printer_id: str) -> None:
        f = self.feeds.pop(printer_id, None)
        if f:
            f.stop()

    def stop_all(self) -> None:
        for f in self.feeds.values():
            f.stop()


def _cam_sig(cfg: PrinterConfig) -> tuple:
    return (cfg.driver, cfg.host, cfg.serial, cfg.settings.get("camera_url"), cfg.settings.get("camera_kind"),
            cfg.settings.get("camera_builtin"), cfg.secrets.get("access_code"), cfg.settings.get("port"))
