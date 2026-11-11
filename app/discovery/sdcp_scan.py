"""Find SDCP printers (Elegoo Centauri Carbon) by UDP broadcast "M99999" to port 3000.

The printer answers with JSON Data {Name, MachineName, BrandName, MainboardIP, MainboardID,
ProtocolVersion, FirmwareVersion} - verified on a real CC (fw V1.4.49, unicast reply).
Blocking - call from a thread.
"""
from __future__ import annotations

import json
import socket
import time
from typing import Callable, Optional

MODELS = {"Centauri Carbon": "elegoo.cc1"}


def to_row(d: dict, sender: str) -> Optional[dict]:
    d = d.get("Data", d)
    if not d.get("MainboardID"):
        return None
    machine = d.get("MachineName") or d.get("Name") or "SDCP printer"
    return {"name": d.get("Name") or machine, "host": d.get("MainboardIP") or sender, "serial": d.get("MainboardID"),
            "firmware": d.get("FirmwareVersion"), "model": f"{d.get('BrandName', '').title()} {machine}".strip(),
            "driver": MODELS.get(machine, "elegoo.unsupported"), "how": "udp"}


def broadcast_scan(timeout: float = 2.5, on_found: Optional[Callable[[dict], None]] = None,
                   subnet: Optional[str] = None, port: int = 3000) -> list[dict]:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.settimeout(0.3)
    targets = ["255.255.255.255"] + ([f"{subnet}.255"] if subnet else [])
    found: dict[str, dict] = {}
    end, next_send = time.time() + timeout, 0.0
    try:
        while time.time() < end:
            if time.time() >= next_send:
                for t in targets:
                    try:
                        s.sendto(b"M99999", (t, port))
                    except OSError:
                        pass
                next_send = time.time() + 1.0
            try:
                data, addr = s.recvfrom(4096)
            except (socket.timeout, OSError):
                continue
            try:
                row = to_row(json.loads(data.decode("utf-8", "replace")), addr[0])
            except ValueError:
                continue
            if row and row["serial"] not in found:
                found[row["serial"]] = row
                if on_found:
                    on_found(row)
    finally:
        s.close()
    return list(found.values())
