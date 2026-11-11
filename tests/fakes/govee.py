"""Fake Govee Platform API: the device list a real account returned on 2026-10-30 (8 H5086 + lights)."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

KEY = "good-key-123"
PLUGS = ["P1-1", "X1", "U1-A", "A1", "P1-2", "U1-B", "U1-C", "U1-D"]


class FakeGovee:
    def __init__(self):
        self.devices = [{"sku": "H605C", "device": "LIGHT:01", "deviceName": "TV Backlight", "type": "devices.types.light",
                         "capabilities": [{"type": "devices.capabilities.on_off", "instance": "powerSwitch"},
                                          {"type": "devices.capabilities.range", "instance": "brightness"}]}]
        self.devices += [{"sku": "H5086", "device": f"PLUG:{i:02d}", "deviceName": n, "type": "devices.types.socket",
                          "capabilities": [{"type": "devices.capabilities.on_off", "instance": "powerSwitch"}]}
                         for i, n in enumerate(PLUGS)]
        self.power = {d["device"]: 1 for d in self.devices}
        self.controls: list[tuple[str, int]] = []
        self.fail_control = False
        self.remaining: int | None = None       # X-RateLimit-Remaining to report
        self.limit_reads = False                # answer 429 to device list / state reads
        self.requests: list[str] = []
        self.ignore_controls = 0                # 'success' but the plug doesn't switch (a real U1-C, 2026-10-30)
        outer = self

        class H(BaseHTTPRequestHandler):
            def _send(self, code, obj):
                b = json.dumps(obj).encode()
                self.send_response(code)
                if outer.remaining is not None:
                    self.send_header("X-RateLimit-Remaining", str(outer.remaining))
                if code == 429:
                    self.send_header("Retry-After", "600")
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def _auth(self):
                if self.headers.get("Govee-API-Key") != KEY:
                    self._send(401, {"code": 401, "message": "Invalid API Key"})
                    return False
                return True

            def do_GET(self):
                outer.requests.append(self.path)
                if not self._auth():
                    return
                if outer.limit_reads:
                    self._send(429, {"code": 429, "message": "Too Many Requests"})
                    return
                if self.path == "/router/api/v1/user/devices":
                    self._send(200, {"code": 200, "message": "success", "data": outer.devices})
                else:
                    self._send(404, {"code": 404})

            def do_POST(self):
                if not self._auth():
                    return
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                p = body["payload"]
                outer.requests.append(self.path)
                if outer.limit_reads and self.path.endswith("/state"):
                    self._send(429, {"code": 429, "message": "Too Many Requests"})
                    return
                if self.path == "/router/api/v1/device/state":
                    self._send(200, {"code": 200, "payload": {"sku": p["sku"], "device": p["device"], "capabilities": [
                        {"type": "devices.capabilities.online", "instance": "online", "state": {"value": True}},
                        {"type": "devices.capabilities.on_off", "instance": "powerSwitch",
                         "state": {"value": outer.power[p["device"]]}}]}})
                elif self.path == "/router/api/v1/device/control":
                    if outer.fail_control:
                        self._send(200, {"code": 400, "msg": "device offline"})
                        return
                    cap = p["capability"]
                    assert cap["type"] == "devices.capabilities.on_off" and cap["instance"] == "powerSwitch"
                    outer.controls.append((p["device"], cap["value"]))
                    if outer.ignore_controls > 0:
                        outer.ignore_controls -= 1
                    else:
                        outer.power[p["device"]] = cap["value"]
                    self._send(200, {"code": 200, "msg": "success", "capability": cap})
                else:
                    self._send(404, {"code": 404})

            def log_message(self, *a):
                pass

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.srv.daemon_threads = True
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()
