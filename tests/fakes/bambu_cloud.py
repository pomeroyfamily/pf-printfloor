"""Fake Bambu cloud (api + web + cover storage) on one local HTTP server."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional


class FakeCloud:
    def __init__(self, tasks: list[dict], covers: dict[str, bytes], mode: str = "code",
                 token: str = "tok123", email: str = "d@example.com", password: str = "pw", code: str = "123456"):
        self.tasks, self.covers, self.mode = tasks, covers, mode
        self.devices: list[dict] = []
        self.token, self.email, self.password, self.code = token, email, password, code
        self.calls: list[str] = []
        self.reject_token = False
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, code: int, obj, cookie: Optional[str] = None):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                if cookie:
                    self.send_header("Set-Cookie", cookie)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                outer.calls.append(f"POST {self.path}")
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                if self.path == "/v1/user-service/user/login":
                    if "code" in body:
                        ok = body["code"] == outer.code and body["account"] == outer.email
                        return self._json(200, {"accessToken": outer.token} if ok else {"message": "bad code"})
                    if body.get("password") != outer.password:
                        return self._json(400, {"message": "wrong password"})
                    if outer.mode == "code":
                        return self._json(200, {"loginType": "verifyCode"})
                    if outer.mode == "tfa":
                        return self._json(200, {"loginType": "tfa", "tfaKey": "k1"})
                    return self._json(200, {"accessToken": outer.token})
                if self.path == "/v1/user-service/user/sendemail/code":
                    return self._json(200, {"success": True})
                if self.path == "/api/sign-in/tfa":
                    if "bbl_csrf_token=c1" not in (self.headers.get("Cookie") or ""):
                        return self._json(403, {"error": "CSRF error: missing_cookie", "reason": "missing_cookie"})
                    if self.headers.get("x-bbl-csrf-token") != "c1":
                        return self._json(403, {"error": "CSRF error: missing_header", "reason": "missing_header"})
                    ok = body.get("tfaKey") == "k1" and body.get("tfaCode") == outer.code
                    return self._json(200 if ok else 400, {}, cookie=f"token={outer.token}; Path=/" if ok else None)
                self._json(404, {})

            def do_GET(self):
                outer.calls.append(f"GET {self.path}")
                if self.path.startswith("/api/csrf"):
                    return self._json(200, {}, cookie="bbl_csrf_token=c1; Path=/")
                if self.path.startswith("/v1/user-service/my/tasks"):
                    if outer.reject_token or self.headers.get("Authorization") != f"Bearer {outer.token}":
                        return self._json(401, {"message": "token expired"})
                    return self._json(200, {"total": len(outer.tasks), "hits": outer.tasks})
                if self.path.startswith("/v1/design-user-service/my/preference"):
                    if outer.reject_token or self.headers.get("Authorization") != f"Bearer {outer.token}":
                        return self._json(401, {"message": "token expired"})
                    return self._json(200, {"uid": 42, "name": "user"})
                if self.path.startswith("/v1/iot-service/api/user/bind"):
                    if outer.reject_token or self.headers.get("Authorization") != f"Bearer {outer.token}":
                        return self._json(401, {"message": "token expired"})
                    return self._json(200, {"message": "success", "devices": outer.devices})
                if self.path.startswith("/cover/"):
                    data = outer.covers.get(self.path.split("/cover/", 1)[1].split("?")[0])
                    if data is None:
                        return self._json(404, {})
                    self.send_response(200)
                    self.send_header("Content-Type", "image/png")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                self._json(404, {})

        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base = f"http://127.0.0.1:{self._srv.server_address[1]}"
        threading.Thread(target=self._srv.serve_forever, daemon=True).start()

    def cover_url(self, name: str) -> str:
        return f"{self.base}/cover/{name}?X-Amz-Signature=abc"

    def close(self):
        self._srv.shutdown()
        self._srv.server_close()
