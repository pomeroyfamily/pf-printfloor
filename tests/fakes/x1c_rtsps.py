"""Fake X1C camera for tests: RTSPS (TLS) with Digest auth (bblp + access code), H.264 over TCP-interleaved RTP,
like the real printer (probe on a real X1C: OPTIONS/DESCRIBE 200 with Digest, H264/90000)."""
from __future__ import annotations

import base64
import hashlib
import re
import socket
import ssl
import struct
import threading
import time
from pathlib import Path

from .bambu_server import free_port, make_cert

REALM, NONCE = "LIVE555 Streaming Media", "8f1c2d3e4b5a"


def make_h264(frames: int = 60, w: int = 320, h: int = 240) -> bytes:
    """Annex-B H.264 test clip (a white box moving over blue), made with PyAV's libx264."""
    import av
    import io
    buf = io.BytesIO()
    out = av.open(buf, "w", format="h264")
    s = out.add_stream("libx264", rate=30)
    s.width, s.height, s.pix_fmt = w, h, "yuv420p"
    s.options = {"tune": "zerolatency", "x264-params": "keyint=15:repeat-headers=1"}
    for i in range(frames):
        x = (i * 4) % (w - 40)
        row = lambda y: b"".join(b"\xff\xff\xff" if 40 <= y < 80 and x <= c < x + 40 else b"\x00\x00\x78" for c in range(w))
        fr = av.VideoFrame(w, h, "rgb24")
        plane = fr.planes[0]
        data = bytearray(plane.line_size * h)
        for y in range(h):
            r = row(y)
            data[y * plane.line_size:y * plane.line_size + len(r)] = r
        plane.update(bytes(data))
        for p in s.encode(fr):
            out.mux(p)
    for p in s.encode():
        out.mux(p)
    out.close()
    return buf.getvalue()


def _md5(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()


class FakeX1cCamera:
    def __init__(self, tmp: Path, code: str):
        self.code, self.port = code, free_port()
        nals = [n for n in re.split(rb"\x00\x00\x00\x01|\x00\x00\x01", make_h264()) if n]
        self.sps = next(n for n in nals if n[0] & 0x1F == 7)
        self.pps = next(n for n in nals if n[0] & 0x1F == 8)
        self.aus, cur = [], []
        for n in nals:
            cur.append(n)
            if n[0] & 0x1F in (1, 5):               # one slice per frame in this clip
                self.aus.append(cur)
                cur = []
        cert, key = make_cert(tmp, "01P00A000000000")
        self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.ctx.load_cert_chain(cert, key)
        self.srv = socket.create_server(("127.0.0.1", self.port))
        self.auth_failures = 0
        self._stop = threading.Event()
        threading.Thread(target=self._accept, daemon=True).start()

    def close(self):
        self._stop.set()
        self.srv.close()

    def _accept(self):
        while not self._stop.is_set():
            try:
                c, _ = self.srv.accept()
                conn = self.ctx.wrap_socket(c, server_side=True)
            except OSError:
                continue
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _sdp(self) -> str:
        ps = f"{base64.b64encode(self.sps).decode()},{base64.b64encode(self.pps).decode()}"
        return ("v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\ns=x1c\r\nt=0 0\r\nm=video 0 RTP/AVP 96\r\nc=IN IP4 0.0.0.0\r\n"
                f"a=rtpmap:96 H264/90000\r\na=fmtp:96 packetization-mode=1;sprop-parameter-sets={ps}\r\n"
                "a=control:track1\r\n")

    def _handle(self, conn):
        f = conn.makefile("rb")
        try:
            while True:
                lines = []
                while True:
                    line = f.readline()
                    if not line:
                        return
                    line = line.decode().rstrip("\r\n")
                    if not line:
                        break
                    lines.append(line)
                method, url, _ = lines[0].split(" ", 2)
                h = {k.lower(): v.strip() for k, v in (x.split(":", 1) for x in lines[1:])}

                def reply(code, extra="", body=""):
                    msg = f"RTSP/1.0 {code}\r\nCSeq: {h.get('cseq', '0')}\r\n{extra}"
                    if body:
                        msg += f"Content-Type: application/sdp\r\nContent-Length: {len(body)}\r\n"
                    conn.sendall((msg + "\r\n" + body).encode())
                if method == "OPTIONS":
                    reply("200 OK", "Public: OPTIONS, DESCRIBE, SETUP, TEARDOWN, PLAY\r\n")
                    continue
                auth = h.get("authorization", "")
                m = dict(re.findall(r'(\w+)="([^"]*)"', auth))
                want = _md5(f"{_md5('bblp:' + REALM + ':' + self.code)}:{NONCE}:{_md5(method + ':' + m.get('uri', ''))}")
                if not (auth.startswith("Digest") and m.get("username") == "bblp" and m.get("response") == want):
                    if auth:
                        self.auth_failures += 1
                    reply("401 Unauthorized", f'WWW-Authenticate: Digest realm="{REALM}", nonce="{NONCE}"\r\n')
                    continue
                if method == "DESCRIBE":
                    reply("200 OK", f"Content-Base: {url}/\r\n", self._sdp())
                elif method == "SETUP":
                    reply("200 OK", "Transport: RTP/AVP/TCP;unicast;interleaved=0-1\r\nSession: 12345\r\n")
                elif method == "PLAY":
                    reply("200 OK", "Session: 12345\r\n")
                    break
                else:
                    reply("200 OK")
            self._play(conn)
        except OSError:
            pass
        finally:
            conn.close()

    def _play(self, conn):
        seq = ts = i = 0
        while not self._stop.is_set():
            au = self.aus[i % len(self.aus)]
            i += 1
            for k, n in enumerate(au):
                last = k == len(au) - 1
                parts = [n] if len(n) <= 1400 else None
                if parts is None:              # FU-A fragments
                    hdr, body = n[0], n[1:]
                    chunks = [body[j:j + 1400] for j in range(0, len(body), 1400)]
                    parts = [bytes([(hdr & 0xE0) | 28, (0x80 if c == 0 else 0) | (0x40 if c == len(chunks) - 1 else 0)
                                    | (hdr & 0x1F)]) + ch for c, ch in enumerate(chunks)]
                for c, payload in enumerate(parts):
                    marker = last and c == len(parts) - 1
                    rtp = struct.pack("!BBHII", 0x80, (0x80 if marker else 0) | 96, seq & 0xFFFF, ts & 0xFFFFFFFF,
                                      0x1234) + payload
                    conn.sendall(b"$\x00" + struct.pack("!H", len(rtp)) + rtp)
                    seq += 1
            ts += 3000
            time.sleep(1 / 30)
