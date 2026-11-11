"""Passive listener for Bambu printer announcements (SSDP NOTIFY on UDP 2021 and 1990).

Bambu printers broadcast about every 5 s; the packet carries the friendly name the printer
shows in Bambu Studio (DevName.bambu.com), its IP (Location), serial (USN) and model code
(DevModel.bambu.com) - verified on all five test Bambus 2026-10-28. Listen only: nothing
is ever sent. Used to name newly added printers now and for "add from discovered list" next.
"""
from __future__ import annotations

import asyncio
import logging
import socket
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

log = logging.getLogger(__name__)
PORTS = (2021, 1990)


@dataclass
class Announcement:
    serial: str
    host: str
    name: Optional[str]
    model_code: Optional[str]
    firmware: Optional[str]
    seen_at: float = field(default_factory=time.time)


def parse(data: bytes, sender_ip: str) -> Optional[Announcement]:
    text = data.decode("utf-8", "replace")
    if "bambu" not in text.lower():
        return None
    h: dict[str, str] = {}
    for line in text.split("\r\n")[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            h[k.strip().lower()] = v.strip()
    serial = h.get("usn")
    if not serial:
        return None
    return Announcement(serial=serial, host=h.get("location") or sender_ip,
                        name=h.get("devname.bambu.com") or None, model_code=h.get("devmodel.bambu.com"),
                        firmware=h.get("devversion.bambu.com"))


class _Proto(asyncio.DatagramProtocol):
    def __init__(self, owner: "BambuSSDP"):
        self.owner = owner

    def datagram_received(self, data: bytes, addr) -> None:
        a = parse(data, addr[0])
        if a:
            self.owner._seen(a)


class BambuSSDP:
    def __init__(self, ports: tuple[int, ...] = PORTS):
        self.ports = ports
        self.printers: dict[str, Announcement] = {}            # by serial
        self.listeners: list[Callable[[Announcement], None]] = []
        self._transports: list[asyncio.DatagramTransport] = []
        self.listening_on: list[int] = []

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        for port in self.ports:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)   # share with Bambu Studio/Orca/Bambuddy
            if hasattr(socket, "SO_REUSEPORT"):                       # Linux/Docker: share with other listeners
                try:
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                except OSError:
                    pass
            try:
                s.bind(("", port))
            except OSError as e:
                log.warning("Bambu announcements: cannot listen on UDP %d (%s)", port, e)
                s.close()
                continue
            tr, _ = await loop.create_datagram_endpoint(lambda: _Proto(self), sock=s)
            self._transports.append(tr)
            self.listening_on.append(port)
        if not self._transports:
            log.warning("Bambu announcements unavailable: printer names and discovery won't work")

    def close(self) -> None:
        for tr in self._transports:
            tr.close()
        self._transports.clear()
        self.listening_on.clear()

    def _seen(self, a: Announcement) -> None:
        self.printers[a.serial] = a
        for cb in list(self.listeners):
            try:
                cb(a)
            except Exception:  # noqa: BLE001
                log.exception("Bambu announcement listener failed")

    def find(self, serial: Optional[str] = None, host: Optional[str] = None) -> Optional[Announcement]:
        if serial and serial in self.printers:
            return self.printers[serial]
        if host:
            return next((a for a in self.printers.values() if a.host == host), None)
        return None
