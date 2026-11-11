"""Camera probe - measures what each printer's built-in camera really delivers (format, resolution, fps).

Run INSIDE the dashboard container so it can read IPs and access codes from the dashboard database
(nothing secret is printed). From a shell on the Docker host:

    docker exec -i pf-printfloor python - "P1" "U1-A" "Centauri Carbon" \
        < <source folder>/tools/camera_probe.py

Arguments = printer names as shown on the dashboard (any number). Each printer is watched for ~15 s.

What it sends (Owner-approved camera "turn on" commands, 2026-10-29):
  * Bambu P1/A1: TLS :6000, 80-byte auth (user bblp + access code), then reads JPEG frames. Read only.
  * Bambu X1C:   RTSPS :322 - OPTIONS + DESCRIBE only (no video decoded: needs ffmpeg). Read only.
  * Snapmaker U1 stock: Moonraker JSON-RPC camera.start_monitor {"domain":"lan","interval":0} every 5 s, then
                 polls /server/files/camera/monitor.jpg. Extended firmware (paxx12): /webcam/snapshot.jpg.
  * Elegoo CC:   SDCP Cmd 386 {"Enable":1} -> VideoUrl (MJPEG), reads it, then Cmd 386 {"Enable":0} to free the slot.
Nothing else: no motion, temperature, print or light commands.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import socket
import ssl
import struct
import sys
import time
import urllib.request
import uuid

from app import config
from app.store import PrinterStore
from app.transports.websocket import WebSocket

WATCH_S = 15


def jpeg_size(data: bytes):
    """(width, height) from a JPEG's SOF marker, or None."""
    i = 2
    while i + 9 < len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        m = data[i + 1]
        if m in (0xC0, 0xC1, 0xC2):
            h, w = struct.unpack(">HH", data[i + 5:i + 9])
            return w, h
        if m in (0xD8, 0x01) or 0xD0 <= m <= 0xD7:
            i += 2
            continue
        ln = struct.unpack(">H", data[i + 2:i + 4])[0]
        i += 2 + ln
    return None


def report(name, how, frames, distinct, secs, size, nbytes, note=""):
    fps = distinct / secs if secs else 0
    dims = f"{size[0]}x{size[1]}" if size else "size ?"
    print(f"  RESULT {name}: {how} | {distinct} distinct frames in {secs:.1f} s = {fps:.2f} fps | {dims}"
          f" | avg {nbytes // max(frames, 1) // 1024} KB/frame" + (f" | {note}" if note else ""))


def ctx():
    c = ssl.create_default_context()
    c.check_hostname, c.verify_mode = False, ssl.CERT_NONE
    return c


# ---------------------------------------------------------------- Bambu P1 / A1 (port 6000)
def bambu_6000(cfg):
    code = cfg.secrets.get("access_code") or ""
    auth = struct.pack("<IIII", 0x40, 0x3000, 0, 0) + b"bblp".ljust(32, b"\0") + code.encode()[:32].ljust(32, b"\0")
    raw = socket.create_connection((cfg.host, 6000), timeout=10)
    s = ctx().wrap_socket(raw, server_hostname=cfg.host)
    s.sendall(auth)

    def exact(n):
        b = b""
        while len(b) < n:
            c = s.recv(n - len(b))
            if not c:
                raise ConnectionError("printer closed the camera connection")
            b += c
        return b
    t0, frames, hashes, size, total = time.monotonic(), 0, set(), None, 0
    while time.monotonic() - t0 < WATCH_S:
        n = struct.unpack("<I", exact(16)[:4])[0]
        if not 1000 <= n <= 8_000_000:
            raise ValueError(f"bad frame length {n}")
        img = exact(n)
        frames += 1
        total += n
        hashes.add(hashlib.md5(img).digest())
        size = size or jpeg_size(img)
    s.close()
    report(cfg.name, "Bambu TLS:6000 JPEG", frames, len(hashes), time.monotonic() - t0, size, total)


# ---------------------------------------------------------------- Bambu X1C (RTSPS 322)
def bambu_rtsp(cfg):
    code = cfg.secrets.get("access_code") or ""
    url = f"rtsps://{cfg.host}:322/streaming/live/1"
    raw = socket.create_connection((cfg.host, 322), timeout=10)
    s = ctx().wrap_socket(raw, server_hostname=cfg.host)
    basic = base64.b64encode(f"bblp:{code}".encode()).decode()

    def ask(method, seq, extra=""):
        s.sendall(f"{method} {url} RTSP/1.0\r\nCSeq: {seq}\r\nAuthorization: Basic {basic}\r\n{extra}\r\n".encode())
        s.settimeout(8)
        buf = b""
        while b"\r\n\r\n" not in buf:
            c = s.recv(4096)
            if not c:
                break
            buf += c
        head, _, body = buf.partition(b"\r\n\r\n")
        ln = next((int(l.split(b":")[1]) for l in head.split(b"\r\n") if l.lower().startswith(b"content-length")), 0)
        while len(body) < ln:
            c = s.recv(4096)
            if not c:
                break
            body += c
        return head.decode(errors="replace"), body.decode(errors="replace")
    h1, _ = ask("OPTIONS", 1)
    print("  OPTIONS ->", h1.split("\r\n")[0])
    h2, sdp = ask("DESCRIBE", 2, "Accept: application/sdp\r\n")
    print("  DESCRIBE ->", h2.split("\r\n")[0])
    for line in sdp.splitlines():
        if line.startswith(("m=", "a=rtpmap", "a=framerate", "a=x-dimensions", "a=fmtp")):
            print("   ", line[:160])
    s.close()
    ok = " 200 " in h2.split("\r\n")[0] + " "
    print(f"  RESULT {cfg.name}: RTSPS:322 {'answers - stream available (H.264 needs a relay to show in a browser)' if ok else 'refused - is LAN Mode Liveview on?'}")


# ---------------------------------------------------------------- polling a snapshot URL
def poll_snapshot(name, url, how, wake=None):
    t0, frames, hashes, size, total, last_wake, errors = time.monotonic(), 0, set(), None, 0, 0.0, 0
    while time.monotonic() - t0 < WATCH_S:
        if wake and time.monotonic() - last_wake > 5:
            wake()
            last_wake = time.monotonic()
        try:
            with urllib.request.urlopen(url + ("&" if "?" in url else "?") + f"t={time.time()}", timeout=5) as r:
                img = r.read()
        except Exception as e:  # noqa: BLE001
            errors += 1
            if errors > 5:
                print(f"  {url} keeps failing: {e}")
                return False
            time.sleep(0.5)
            continue
        if img[:3] != b"\xff\xd8\xff":
            print(f"  {url} is not a JPEG ({img[:20]!r})")
            return False
        frames += 1
        total += len(img)
        hashes.add(hashlib.md5(img).digest())
        size = size or jpeg_size(img)
        time.sleep(0.1)
    report(name, how, frames, len(hashes), time.monotonic() - t0, size, total, f"{frames} requests")
    return True


# ---------------------------------------------------------------- Snapmaker U1
def u1(cfg):
    port = int(cfg.settings.get("port") or 80)
    base = f"http://{cfg.host}" + ("" if port == 80 else f":{port}")
    found = False
    try:                                           # extended firmware (paxx12)
        with urllib.request.urlopen(base + "/webcam/snapshot.jpg", timeout=5) as r:
            if r.read(3) == b"\xff\xd8\xff":
                found = True
                print("  extended firmware camera found (/webcam/snapshot.jpg)")
                poll_snapshot(cfg.name, base + "/webcam/snapshot.jpg", "U1 extended /webcam/snapshot.jpg")
                print(f"  also available: {base}/webcam/webrtc (WebRTC), rtsp://{cfg.host}:8554/stream if RTSP is on")
    except Exception:  # noqa: BLE001
        pass

    def wake():
        try:
            ws = WebSocket(cfg.host, port, "/websocket", timeout=5)
            ws.send_text(json.dumps({"jsonrpc": "2.0", "method": "camera.start_monitor",
                                     "params": {"domain": "lan", "interval": 0}, "id": 1}))
            ws.settimeout(3)
            try:
                for _ in range(5):
                    msg = ws.recv_text()
                    if msg and '"id": 1' in msg.replace('"id":1', '"id": 1'):
                        if "error" in msg:
                            print("  start_monitor error:", msg[:200])
                        break
            except Exception:  # noqa: BLE001
                pass
            ws.sock.close()
        except Exception as e:  # noqa: BLE001
            print("  wake failed:", e)
    if not found:
        print("  stock firmware: waking the camera with camera.start_monitor every 5 s")
    poll_snapshot(cfg.name, base + "/server/files/camera/monitor.jpg", "U1 stock monitor.jpg", wake)


# ---------------------------------------------------------------- Elegoo CC (SDCP Cmd 386)
def cc(cfg):
    mb = cfg.serial

    def req(cmd, data):
        return json.dumps({"Id": uuid.uuid4().hex, "Data": {"Cmd": cmd, "Data": data, "RequestID": secrets.token_hex(8),
                           "MainboardID": mb, "TimeStamp": int(time.time() * 1000), "From": 1},
                           "Topic": f"sdcp/request/{mb}"})

    def call(data):
        ws = WebSocket(cfg.host, 3030)
        ws.settimeout(1)
        ws.send_text(req(386, data))
        deadline, out = time.monotonic() + 6, None
        while time.monotonic() < deadline and out is None:
            try:
                txt = ws.recv_text()
            except socket.timeout:
                continue
            if not txt or txt == "pong":
                continue
            d = json.loads(txt).get("Data") or {}
            if d.get("Cmd") == 386:
                out = d.get("Data") or {}
        ws.sock.close()
        return out
    on = call({"Enable": 1})
    print("  Cmd 386 Enable:1 ->", on)
    url = (on or {}).get("VideoUrl")
    if not url:
        print(f"  RESULT {cfg.name}: no video URL (Ack {(on or {}).get('Ack')}: 1 = viewer limit reached, 2 = no camera)")
        return
    if not url.startswith("http"):
        url = "http://" + url
    try:
        r = urllib.request.urlopen(url, timeout=8)
        print("  stream content-type:", r.headers.get("Content-Type"))
        t0, buf, frames, hashes, size, total = time.monotonic(), b"", 0, set(), None, 0
        while time.monotonic() - t0 < WATCH_S:
            chunk = r.read(65536)
            if not chunk:
                break
            buf += chunk
            while True:
                a = buf.find(b"\xff\xd8")
                b = buf.find(b"\xff\xd9", a + 2) if a >= 0 else -1
                if a < 0 or b < 0:
                    break
                img, buf = buf[a:b + 2], buf[b + 2:]
                frames += 1
                total += len(img)
                hashes.add(hashlib.md5(img).digest())
                size = size or jpeg_size(img)
        r.close()
        report(cfg.name, f"CC MJPEG {url.split('//')[1].split('/')[0]}", frames, len(hashes),
               time.monotonic() - t0, size, total)
    finally:
        print("  Cmd 386 Enable:0 ->", call({"Enable": 0}))


def main(names):
    store = PrinterStore(config.DB_PATH)
    printers = {p.name.lower(): p for p in store.list()}
    if not names:
        print("printers:", ", ".join(p.name for p in store.list()))
        return
    for n in names:
        cfg = printers.get(n.lower())
        print(f"\n=== {n} ===")
        if not cfg:
            print("  not found. Names:", ", ".join(p.name for p in store.list()))
            continue
        print(f"  {cfg.driver} at {cfg.host}")
        try:
            if cfg.driver == "bambu.x1c":
                bambu_rtsp(cfg)
            elif cfg.driver.startswith("bambu."):
                bambu_6000(cfg)
            elif cfg.driver.startswith("snapmaker."):
                u1(cfg)
            elif cfg.driver.startswith("elegoo."):
                cc(cfg)
            else:
                print("  no camera test for", cfg.driver)
        except Exception as e:  # noqa: BLE001
            print(f"  RESULT {cfg.name}: FAILED - {type(e).__name__}: {e}")


if __name__ == "__main__":
    main(sys.argv[1:])
