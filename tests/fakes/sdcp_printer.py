"""Fake Elegoo Centauri Carbon: UDP discovery (M99999), SDCP WebSocket, thumbnail HTTP.

Replays message shapes captured from a real CC (fw V1.4.49, 2026-10-29): Cmd 1 -> ack +
'Attributes' push, Cmd 0 -> ack + 'Status' push. Canvas (Cmd 324) reply follows the field list
in Elegoo's elegoo-link SDK parser (not yet seen on real hardware).
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import socket
import socketserver
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

CANVAS = {"Ack": 0, "active_canvas_id": 0, "active_tray_id": 1, "auto_refill": False, "canvas_list": [
    {"canvas_id": 0, "connected": True, "tray_list": [
        {"tray_id": 0, "brand": "ELEGOO", "filament_type": "PETG", "filament_name": "PETG Pro",
         "filament_code": "0x0001", "filament_color": "#1E88E5", "min_nozzle_temp": 230, "max_nozzle_temp": 260, "status": 1},
        {"tray_id": 1, "brand": "ELEGOO", "filament_type": "PETG", "filament_name": "Rapid PETG",
         "filament_code": "0x0002", "filament_color": "#E72F1D", "min_nozzle_temp": 230, "max_nozzle_temp": 260, "status": 1},
        {"tray_id": 2, "brand": "", "filament_type": "", "filament_name": "", "filament_code": "",
         "filament_color": "", "min_nozzle_temp": 0, "max_nozzle_temp": 0, "status": 0},
        {"tray_id": 3, "brand": "Generic", "filament_type": "PLA", "filament_name": "PLA",
         "filament_code": "", "filament_color": "#FFFFFF", "min_nozzle_temp": 190, "max_nozzle_temp": 230, "status": 1},
    ]}]}


def _frame(text: str) -> bytes:
    p = text.encode()
    n = len(p)
    hdr = bytes([0x81]) + (bytes([n]) if n < 126 else bytes([126]) + struct.pack("!H", n) if n < 65536
                           else bytes([127]) + struct.pack("!Q", n))
    return hdr + p


def _read_frame(rf) -> Optional[tuple[int, bytes]]:
    h = rf.read(2)
    if len(h) < 2:
        return None
    op, n = h[0] & 0x0F, h[1] & 0x7F
    if n == 126:
        n = struct.unpack("!H", rf.read(2))[0]
    elif n == 127:
        n = struct.unpack("!Q", rf.read(8))[0]
    mask = rf.read(4) if h[1] & 0x80 else b"\0\0\0\0"
    data = bytes(b ^ mask[i % 4] for i, b in enumerate(rf.read(n)))
    return op, data


class FakeCC:
    def __init__(self, dump: dict, *, idle: bool = False, canvas: str = "answer", busy: bool = False,
                 thumbnails: Optional[dict[str, bytes]] = None, history: Optional[list[dict]] = None,
                 video_url: Optional[str] = None, canvas_reply: Optional[dict] = None):
        self.dump = copy.deepcopy(dump)
        self.mb = self.dump["mainboard_id"]
        self.status = self.dump["status"]
        self.attrs = self.dump["attributes"]
        if idle:
            self.status["CurrentStatus"] = [0]
            self.status["PrintInfo"].update(Status=9, Progress=100)
        self.canvas_mode, self.busy = canvas, busy             # canvas: answer | close | silent
        self.thumbnails = thumbnails or {}
        self.cmds: list[int] = []
        self.history_details: list[dict] = list(history or [])  # Cmd 321 rows; Cmd 320 lists their TaskIds
        self.requested_ids: list[list[str]] = []
        self.camera_calls: list = []
        self.video_url = video_url
        self.canvas_reply = canvas_reply or CANVAS
        self.connections = 0
        outer = self

        class WSHandler(socketserver.StreamRequestHandler):
            def handle(self):
                head = b""
                while b"\r\n\r\n" not in head:
                    c = self.request.recv(4096)
                    if not c:
                        return
                    head += c
                outer.connections += 1
                if outer.busy:
                    self.wfile.write(b"HTTP/1.1 500 Internal Server Error\r\nContent-Length: 15\r\n\r\ntoo many client")
                    return
                key = next(l.split(b":", 1)[1].strip() for l in head.split(b"\r\n") if l.lower().startswith(b"sec-websocket-key"))
                acc = base64.b64encode(hashlib.sha1(key + GUID).digest())
                self.wfile.write(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                                 b"Sec-WebSocket-Accept: " + acc + b"\r\n\r\n")
                while True:
                    f = _read_frame(self.rfile)
                    if f is None or f[0] == 8:
                        return
                    op, data = f
                    if op != 1:
                        continue
                    txt = data.decode()
                    if txt == "ping":
                        self.wfile.write(_frame("pong"))
                        continue
                    req = json.loads(txt)["Data"]
                    cmd = req["Cmd"]
                    outer.cmds.append(cmd)
                    if cmd == 324:
                        if outer.canvas_mode == "close":
                            return
                        if outer.canvas_mode == "silent":
                            continue
                    self.wfile.write(_frame(json.dumps(outer._ack(cmd, req))))
                    if cmd == 1:
                        self.wfile.write(_frame(json.dumps({"Attributes": outer.attrs, "MainboardID": outer.mb,
                                                            "TimeStamp": 1, "Topic": f"sdcp/attributes/{outer.mb}"})))
                    if cmd == 0:
                        self.wfile.write(_frame(json.dumps({"Status": outer.status, "MainboardID": outer.mb,
                                                            "TimeStamp": 1, "Topic": f"sdcp/status/{outer.mb}"})))

        class TCP(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        self._ws = TCP(("127.0.0.1", 0), WSHandler)
        self.ws_port = self._ws.server_address[1]
        threading.Thread(target=self._ws.serve_forever, daemon=True).start()

        self._udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._udp.bind(("127.0.0.1", 0))
        self.udp_port = self._udp.getsockname()[1]
        threading.Thread(target=self._udp_loop, daemon=True).start()

        class HTTP(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                data = outer.thumbnails.get(self.path)
                if data is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self._http = ThreadingHTTPServer(("127.0.0.1", 0), HTTP)
        self.http_port = self._http.server_address[1]
        threading.Thread(target=self._http.serve_forever, daemon=True).start()

    def _ack(self, cmd: int, req: dict) -> dict:
        data = self.canvas_reply if cmd == 324 else {"Ack": 0}
        if cmd == 386:
            self.camera_calls.append((req.get("Data") or {}).get("Enable"))
            data = {"Ack": 0}
            if (req.get("Data") or {}).get("Enable") and self.video_url:
                data["VideoUrl"] = self.video_url
        if cmd == 320:
            data = {"Ack": 0, "HistoryData": [d["TaskId"] for d in self.history_details]}
        if cmd == 321:
            want = (req.get("Data") or {}).get("Id") or []
            self.requested_ids.append(want)
            data = {"Ack": 0, "HistoryDetailList": [d for d in self.history_details if d["TaskId"] in want]}
        return {"Id": "979d4C788A4a78bC777A870F1A02867A",
                "Data": {"Cmd": cmd, "Data": data, "RequestID": req.get("RequestID"), "MainboardID": self.mb,
                         "TimeStamp": 1}, "Topic": f"sdcp/response/{self.mb}"}

    def _udp_loop(self):
        while True:
            try:
                data, addr = self._udp.recvfrom(1024)
            except OSError:
                return
            if data.strip() == b"M99999":
                self._udp.sendto(json.dumps(self.dump["discovery"]["reply"]).encode(), addr)

    def settings(self) -> dict:
        return {"ws_port": self.ws_port, "udp_port": self.udp_port, "http_port": self.http_port, "poll_interval": 0.3}

    def close(self):
        self._ws.shutdown()
        self._ws.server_close()
        self._http.shutdown()
        self._http.server_close()
        self._udp.close()
