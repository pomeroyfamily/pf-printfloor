"""Fake printer cameras for tests: Bambu TLS :6000-style, U1 (stock + extended), MJPEG/snapshot HTTP."""
from __future__ import annotations

import base64
import hashlib
import json
import socket
import ssl
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).parent


def jpeg(w: int, h: int, n: int) -> bytes:
    """Tiny valid-looking JPEG (SOI, SOF0 with size, comment unique per n, EOI)."""
    sof = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, h, w, 1) + b"\x01\x11\x00"
    body = str(n).encode().ljust(1200, b".")
    return b"\xff\xd8" + sof + b"\xff\xfe" + struct.pack(">H", 2 + len(body)) + body + b"\xff\xd9"


class FakeBambuCamera:
    def __init__(self, code: str, fps: float = 10):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(HERE / "cam_cert.pem", HERE / "cam_key.pem")
        self.code, self.fps, self.connections, self.open = code, fps, 0, 0
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self._closed = False

        def serve():
            while not self._closed:
                try:
                    c, _ = self.sock.accept()
                except OSError:
                    return
                threading.Thread(target=self._client, args=(ctx, c), daemon=True).start()
        threading.Thread(target=serve, daemon=True).start()

    def _client(self, ctx, c):
        self.connections += 1
        self.open += 1
        try:
            s = ctx.wrap_socket(c, server_side=True)
            a = b""
            while len(a) < 80:
                a += s.recv(80 - len(a))
            if a[48:80].rstrip(b"\0").decode() != self.code:
                s.close()
                return
            n = 0
            while not self._closed:
                img = jpeg(1536, 1080, n)
                n += 1
                s.sendall(struct.pack("<IIII", len(img), 0, 1, 0) + img)
                time.sleep(1 / self.fps)
        except OSError:
            pass
        finally:
            self.open -= 1

    def close(self):
        self._closed = True
        self.sock.close()


class FakeHttpCameras:
    """One HTTP server: /mjpeg (multipart), /snap.jpg, U1 /websocket + /server/files/camera/monitor.jpg,
    optional extended /webcam/snapshot.jpg."""

    def __init__(self, extended: bool = False, ext_delay: float = 0.0):
        self.wakes: list[dict] = []
        self.mjpeg_clients = 0
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _img(self, img):
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(img)))
                self.end_headers()
                self.wfile.write(img)

            def do_GET(self):
                path = self.path.split("?")[0]
                if path == "/mjpeg" or path == "/video":
                    outer.mjpeg_clients += 1
                    self.send_response(200)
                    self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=--foo")
                    self.end_headers()
                    try:
                        for n in range(100000):
                            img = jpeg(640, 360, n)
                            self.wfile.write(b"----foo\r\nContent-Type: image/jpeg\r\n\r\n" + img + b"\r\n")
                            time.sleep(0.05)
                    except OSError:
                        pass
                    finally:
                        outer.mjpeg_clients -= 1
                elif path == "/snap.jpg":
                    self._img(jpeg(800, 600, int(time.time() * 5)))
                elif path == "/webcam/snapshot.jpg" and extended:
                    time.sleep(ext_delay)                       # Paxx on a real U1-C: ~0.37 s per request
                    self._img(jpeg(1920, 1080, int(time.time() * 100)))
                elif path == "/server/files/camera/monitor.jpg":
                    self._img(jpeg(1920, 1080, int(time.time() * 3) if outer.wakes else 0))
                elif path == "/websocket":
                    key = self.headers["Sec-WebSocket-Key"]
                    acc = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest())
                    self.send_response(101)
                    self.send_header("Upgrade", "websocket")
                    self.send_header("Connection", "Upgrade")
                    self.send_header("Sec-WebSocket-Accept", acc.decode())
                    self.end_headers()
                    self.wfile.flush()
                    b1, b2 = self.rfile.read(2)
                    n = b2 & 0x7F
                    if n == 126:
                        n = struct.unpack(">H", self.rfile.read(2))[0]
                    mask = self.rfile.read(4)
                    data = bytes(x ^ mask[i % 4] for i, x in enumerate(self.rfile.read(n)))
                    outer.wakes.append(json.loads(data))
                    reply = json.dumps({"jsonrpc": "2.0", "result": "ok", "id": 1}).encode()
                    self.wfile.write(bytes([0x81, len(reply)]) + reply)
                    self.wfile.flush()
                    self.close_connection = True
                else:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()

            def log_message(self, *a):
                pass

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.srv.daemon_threads = True
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()
