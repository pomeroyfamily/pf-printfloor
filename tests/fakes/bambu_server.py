"""In-process fake Bambu printer for tests: MQTT 3.1.1 over TLS + implicit FTPS.

Replays real fixture states. Records every request so tests can assert the driver
stays read-only (only pushall / get_version).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import socket
import ssl
import struct
import threading
from pathlib import Path
from typing import Optional

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


def make_cert(tmp: Path, cn: str) -> tuple[Path, Path]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "BBL Device CA Test")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(issuer).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(days=1))
            .not_valid_after(now + dt.timedelta(days=2)).sign(key, hashes.SHA256()))
    c, k = tmp / "cert.pem", tmp / "key.pem"
    c.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    k.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                                    serialization.NoEncryption()))
    return c, k


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _enc_len(n: int) -> bytes:
    out = bytearray()
    while True:
        b, n = n % 128, n // 128
        out.append(b | 0x80 if n else b)
        if not n:
            return bytes(out)


def _publish_pkt(topic: str, payload: bytes) -> bytes:
    t = topic.encode()
    body = struct.pack("!H", len(t)) + t + payload
    return bytes([0x30]) + _enc_len(len(body)) + body


async def _read_pkt(r: asyncio.StreamReader):
    h = (await r.readexactly(1))[0]
    mult, n = 1, 0
    while True:
        b = (await r.readexactly(1))[0]
        n += (b & 0x7F) * mult
        mult *= 128
        if not b & 0x80:
            break
    return h, await r.readexactly(n)


class FakeBambu:
    def __init__(self, tmp: Path, fixture: dict, serial: str = "01S099000000014", code: str = "12345678",
                 deltas: Optional[list[dict]] = None, delta_interval: float = 0.05, silent_after: bool = False,
                 no_rest: bool = False, user: str = "bblp", ftps: bool = True):
        self.tmp, self.fixture, self.serial, self.code = tmp, fixture, serial, code
        self.user = user            # "u_<uid>" + token for a fake Bambu cloud broker
        self.ftps = ftps            # False: no fake FTPS server (tests that never read files)
        self._ftp_thread: Optional[threading.Thread] = None
        self.deltas = deltas or []
        self.delta_interval = delta_interval
        self.silent_after = silent_after
        self.no_rest = no_rest      # like the P1P's BBL-P003 FTP server: REST -> 502
        self.requests: list[dict] = []
        self.mqtt_port = free_port()
        self.ftps_port = free_port()
        self.cert, self.key = make_cert(tmp, serial)
        self.ftp_root = tmp / "sd"
        (self.ftp_root / "cache").mkdir(parents=True)
        self._server: Optional[asyncio.base_events.Server] = None
        self._ftp = None

    # ---------------------------------------------------------------- MQTT
    async def _handle(self, r: asyncio.StreamReader, w: asyncio.StreamWriter):
        try:
            h, body = await _read_pkt(r)
            plen = struct.unpack("!H", body[:2])[0]
            flags = body[2 + plen + 1]
            i = 2 + plen + 4

            def s(i):
                ln = struct.unpack("!H", body[i:i + 2])[0]
                return body[i + 2:i + 2 + ln].decode(), i + 2 + ln
            _cid, i = s(i)
            user = pw = None
            if flags & 0x80:
                user, i = s(i)
            if flags & 0x40:
                pw, i = s(i)
            ok = user == self.user and pw == self.code
            w.write(bytes([0x20, 2, 0, 0 if ok else 5]))
            await w.drain()
            if not ok:
                w.close()
                return
            report = f"device/{self.serial}/report"
            while True:
                h, body = await _read_pkt(r)
                typ = h >> 4
                if typ == 8:
                    w.write(bytes([0x90, 3]) + body[:2] + b"\x00")
                elif typ == 3:
                    tl = struct.unpack("!H", body[:2])[0]
                    topic = body[2:2 + tl].decode()
                    payload = json.loads(body[2 + tl:])
                    self.requests.append({"topic": topic, "payload": payload})
                    if topic != f"device/{self.serial}/request":
                        continue
                    if payload.get("pushing", {}).get("command") == "pushall":
                        full = dict(self.fixture["merged_state"], command="push_status", msg=0)
                        w.write(_publish_pkt(report, json.dumps({"print": full}).encode()))
                        asyncio.create_task(self._stream(w, report))
                    elif payload.get("info", {}).get("command") == "get_version":
                        w.write(_publish_pkt(report, json.dumps({"info": self.fixture["version"]}).encode()))
                elif typ == 12:
                    w.write(b"\xd0\x00")
                elif typ == 14:
                    break
                await w.drain()
        except (asyncio.IncompleteReadError, ConnectionResetError, ssl.SSLError):
            pass
        finally:
            w.close()

    async def _stream(self, w, report):
        for d in self.deltas:
            await asyncio.sleep(self.delta_interval)
            w.write(_publish_pkt(report, json.dumps({"print": dict(d, command="push_status", msg=1)}).encode()))
            await w.drain()

    # ---------------------------------------------------------------- FTPS
    def _start_ftps(self):
        from pyftpdlib.authorizers import DummyAuthorizer
        from pyftpdlib.handlers import TLS_FTPHandler
        from pyftpdlib.servers import ThreadedFTPServer

        class Implicit(TLS_FTPHandler):
            def handle(self):
                self.secure_connection(self.ssl_context)

            def handle_ssl_established(self):
                TLS_FTPHandler.handle(self)

            def ftp_AUTH(self, line):
                self.respond("503 Already using TLS.")

            def ftp_REST(self, line):
                if no_rest:
                    self.respond("502 ")
                else:
                    TLS_FTPHandler.ftp_REST(self, line)

        no_rest = self.no_rest
        auth = DummyAuthorizer()
        auth.add_user("bblp", self.code, str(self.ftp_root), perm="elr")
        Implicit.authorizer = auth
        Implicit.certfile, Implicit.keyfile = str(self.cert), str(self.key)
        Implicit.tls_control_required = Implicit.tls_data_required = True
        self._ftp = ThreadedFTPServer(("127.0.0.1", self.ftps_port), Implicit)
        self._ftp_thread = threading.Thread(target=self._ftp.serve_forever, kwargs={"handle_exit": False}, daemon=True)
        self._ftp_thread.start()

    # ---------------------------------------------------------------- lifecycle
    async def __aenter__(self):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(self.cert, self.key)
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", self.mqtt_port, ssl=ctx)
        if self.ftps:
            self._start_ftps()
        return self

    async def __aexit__(self, *exc):
        if self._server:
            self._server.close()
        if self._ftp:
            # Close and WAIT for the server thread. Without the wait, its loop could still be running when the next
            # test opens a socket that reuses the same file number, and close it ("Bad file descriptor" on GitHub).
            self._ftp.close_all()
            if self._ftp_thread:
                await asyncio.to_thread(self._ftp_thread.join, 5)
