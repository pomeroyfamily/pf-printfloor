"""Minimal blocking WebSocket client (RFC 6455, text frames) - standard library only.
Ported from cc_probe.py, which ran against a real Centauri Carbon. Call from asyncio.to_thread."""
from __future__ import annotations

import base64
import os
import socket
import struct
from typing import Optional

from ..errors import PrinterBusy, PrinterUnreachable


class WebSocket:
    def __init__(self, host: str, port: int, path: str = "/websocket", timeout: float = 6):
        try:
            self.sock = socket.create_connection((host, port), timeout=timeout)
        except OSError as e:
            raise PrinterUnreachable(f"{host}:{port} not reachable ({e})") from e
        key = base64.b64encode(os.urandom(16)).decode()
        req = (f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
        try:
            self.sock.sendall(req.encode())
            resp = b""
            while b"\r\n\r\n" not in resp:
                chunk = self.sock.recv(4096)
                if not chunk:
                    raise PrinterUnreachable("connection closed during WebSocket handshake")
                resp += chunk
                if len(resp) > 65536:
                    raise PrinterUnreachable("bad WebSocket handshake reply")
        except OSError as e:
            self.sock.close()
            raise PrinterUnreachable(f"WebSocket handshake failed ({e})") from e
        head, self.buf = resp.split(b"\r\n\r\n", 1)
        status = head.split(b"\r\n", 1)[0].decode(errors="replace")
        if " 101 " not in status + " ":
            try:                                   # body may follow (e.g. "too many client")
                self.sock.settimeout(1)
                self.buf += self.sock.recv(4096)
            except OSError:
                pass
            self.sock.close()
            text = (status + " " + self.buf.decode(errors="replace")).strip()
            if "too many" in text.lower() or " 500 " in status + " " or " 503 " in status + " ":
                raise PrinterBusy("all connection slots are in use (open slicers and apps hold them) - retrying")
            raise PrinterUnreachable(f"WebSocket refused: {text[:120]}")

    def settimeout(self, t: float) -> None:
        self.sock.settimeout(t)

    def _recv_exact(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("connection closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def send_text(self, text: str, opcode: int = 1) -> None:
        payload = text.encode()
        hdr = bytes([0x80 | opcode])
        n = len(payload)
        if n < 126:
            hdr += bytes([0x80 | n])
        elif n < 65536:
            hdr += bytes([0x80 | 126]) + struct.pack("!H", n)
        else:
            hdr += bytes([0x80 | 127]) + struct.pack("!Q", n)
        mask = os.urandom(4)
        self.sock.sendall(hdr + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def recv_text(self) -> Optional[str]:
        """Next text message, or None when the peer closes. Raises socket.timeout / ConnectionError."""
        parts = []
        while True:
            b1, b2 = self._recv_exact(2)
            fin, op = b1 & 0x80, b1 & 0x0F
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack("!H", self._recv_exact(2))[0]
            elif n == 127:
                n = struct.unpack("!Q", self._recv_exact(8))[0]
            mask = self._recv_exact(4) if b2 & 0x80 else None
            data = self._recv_exact(n)
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if op == 8:
                return None
            if op == 9:
                self.send_text(data.decode(errors="replace"), opcode=10)
                continue
            if op == 10:
                continue
            parts.append(data)
            if fin:
                return b"".join(parts).decode("utf-8", "replace")

    def close(self) -> None:
        try:
            self.sock.sendall(bytes([0x88, 0x80]) + os.urandom(4))
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass
