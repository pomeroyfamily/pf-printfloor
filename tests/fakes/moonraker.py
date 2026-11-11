"""Fake Moonraker (HTTP GET only) serving a fixture. Records request paths."""
from __future__ import annotations

import copy
import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional


class FakeMoonraker:
    def __init__(self, fixture: dict, thumbnails: Optional[dict[str, bytes]] = None, require_key: Optional[str] = None,
                 u1: bool = True):
        self.fx = copy.deepcopy(fixture)
        self.thumbnails = thumbnails or {}
        self.require_key, self.u1 = require_key, u1
        self.paths: list[str] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code: int, body: bytes, ctype="application/json"):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _json(self, obj, code=200):
                self._send(code, json.dumps(obj).encode())

            def do_GET(self):
                outer.paths.append(self.path)
                if outer.require_key and self.headers.get("X-Api-Key") != outer.require_key:
                    return self._json({"error": {"code": 401, "message": "Unauthorized"}}, 401)
                u = urllib.parse.urlsplit(self.path)
                path, qs = urllib.parse.unquote(u.path), u.query
                fx = outer.fx
                if path == "/server/info":
                    return self._json({"result": fx["server_info"]})
                if path == "/printer/info":
                    return self._json({"result": fx["printer_info"]})
                if path == "/machine/system_info":
                    si = fx["system_info"] if outer.u1 else {"cpu_info": {}}
                    return self._json({"result": {"system_info": si}})
                if path == "/machine/proc_stats":                     # shape per Moonraker docs (U1 source tree)
                    return self._json({"result": {"system_uptime": fx.get("system_uptime", 5 * 3600 + 7),
                                                  "cpu_temp": 45.6, "websocket_connections": 2}})
                if path == "/printer/objects/list":
                    return self._json({"result": {"objects": fx["objects"]}})
                if path == "/printer/objects/query":
                    names = [urllib.parse.unquote(p.split("=")[0]) for p in qs.split("&") if p]
                    st = {}
                    for n in names:
                        if n == "heaters":
                            st[n] = {"available_sensors": fx["available_sensors"],
                                     "available_heaters": [s for s in fx["available_sensors"] if "sensor" not in s]}
                        elif n in fx["status"]:
                            st[n] = fx["status"][n]
                    return self._json({"result": {"eventtime": 1.0, "status": st}})
                if path == "/server/files/metadata":
                    fn = urllib.parse.parse_qs(qs).get("filename", [""])[0]
                    md = fx.get("file_metadata")
                    if md and md["filename"] == fn:
                        return self._json({"result": md})
                    return self._json({"error": {"code": 404, "message": "not found"}}, 404)
                if path.startswith("/server/files/gcodes/"):
                    rel = path[len("/server/files/gcodes/"):]
                    data = outer.thumbnails.get(rel)
                    if data is None:
                        return self._json({"error": {"code": 404}}, 404)
                    return self._send(200, data, "image/png")
                self._json({"error": {"code": 404, "message": path}}, 404)

        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self._srv.server_address[1]
        threading.Thread(target=self._srv.serve_forever, daemon=True).start()

    def close(self):
        self._srv.shutdown()
        self._srv.server_close()
