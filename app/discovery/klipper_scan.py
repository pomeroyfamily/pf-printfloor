"""Find Snapmaker U1s, other Klipper/Moonraker printers and Elegoo (SDCP) printers on the LAN
(on demand, read-only; Elegoo via sdcp_scan's UDP broadcast).

Two methods, run together (both proven by u1_discover.py on a real network, 2026-10-28):
  * mDNS query for "_snapmaker._tcp.local." - TXT carries device_name, sn, version, ip
  * HTTP sweep of the local /24 on port 80: GET /machine/system_info (U1: product_info with
    device_name, machine_type, serial_number, firmware_version), else GET /server/info (any Moonraker)
Blocking functions - call from asyncio.to_thread.
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import socket
import struct
import threading
import time
import urllib.request
from typing import Callable, Optional

SERVICE = "_snapmaker._tcp.local."
MDNS = ("224.0.0.251", 5353)


# ---------------------------------------------------------------------- mDNS (minimal, stdlib)
def _qname(name: str) -> bytes:
    return b"".join(bytes([len(p)]) + p.encode() for p in name.rstrip(".").split(".")) + b"\x00"


def _read_name(msg: bytes, i: int) -> tuple[str, int]:
    labels, jumped, end = [], False, i
    for _ in range(64):
        ln = msg[i]
        if ln == 0:
            i += 1
            break
        if ln & 0xC0 == 0xC0:
            ptr = struct.unpack("!H", msg[i:i + 2])[0] & 0x3FFF
            if not jumped:
                end = i + 2
            i, jumped = ptr, True
            continue
        labels.append(msg[i + 1:i + 1 + ln].decode("utf-8", "replace"))
        i += 1 + ln
    return ".".join(labels) + ".", (end if jumped else i)


def _records(msg: bytes) -> list[tuple[str, int, bytes, int]]:
    qd, an, ns, ar = struct.unpack("!4H", msg[4:12])
    i = 12
    for _ in range(qd):
        _, i = _read_name(msg, i)
        i += 4
    out = []
    for _ in range(an + ns + ar):
        name, i = _read_name(msg, i)
        rtype, _cls, _ttl, rdlen = struct.unpack("!HHIH", msg[i:i + 10])
        i += 10
        out.append((name, rtype, msg[i:i + rdlen], i))
        i += rdlen
    return out


def parse_mdns(msg: bytes, sender: str) -> list[dict]:
    """Snapmaker TXT records in one mDNS packet -> [{name, host, serial, firmware}]."""
    recs = _records(msg)
    a_records = {n: socket.inet_ntoa(d) for n, t, d, _ in recs if t == 1 and len(d) == 4}
    found: dict[str, dict] = {}
    for name, rtype, data, off in recs:
        if SERVICE.lower() not in name.lower():
            continue
        e = found.setdefault(name, {})
        if rtype == 16:                                   # TXT
            j = 0
            while j < len(data):
                ln = data[j]
                kv = data[j + 1:j + 1 + ln].decode("utf-8", "replace")
                j += 1 + ln
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    e[k] = v
        elif rtype == 33:                                 # SRV -> target host name
            e["_target"], _ = _read_name(msg, off + 6)
    out = []
    for e in found.values():
        host = e.get("ip") or a_records.get(e.get("_target", "")) or sender
        if e.get("sn") or e.get("device_name"):
            out.append({"name": e.get("device_name"), "host": host, "serial": e.get("sn"),
                        "firmware": e.get("version"), "model": "Snapmaker U1", "how": "mdns"})
    return out


def mdns_scan(timeout: float = 3.0, on_found: Optional[Callable[[dict], None]] = None) -> list[dict]:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
    s.settimeout(0.4)
    query = struct.pack("!6H", 0, 0, 1, 0, 0, 0) + _qname(SERVICE) + struct.pack("!HH", 12, 1)   # PTR IN
    out: dict[str, dict] = {}
    end, next_send = time.time() + timeout, 0.0
    try:
        while time.time() < end:
            if time.time() >= next_send:
                try:
                    s.sendto(query, MDNS)
                except OSError:
                    return []
                next_send = time.time() + 1.0
            try:
                msg, addr = s.recvfrom(9000)
            except (socket.timeout, OSError):
                continue
            try:
                for p in parse_mdns(msg, addr[0]):
                    key = p["serial"] or p["host"]
                    if key not in out and on_found:
                        on_found(p)
                    out[key] = p
            except (IndexError, struct.error, UnicodeDecodeError):
                continue
    finally:
        s.close()
    return list(out.values())


# ---------------------------------------------------------------------- HTTP sweep
def local_subnet() -> Optional[str]:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))                  # picks the outgoing interface; sends nothing
        return s.getsockname()[0].rsplit(".", 1)[0]
    except OSError:
        return None
    finally:
        s.close()


def _get_json(url: str, timeout: float):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def probe_host(ip: str, port: int = 80, connect_timeout: float = 0.6) -> Optional[dict]:
    try:
        with socket.create_connection((ip, port), timeout=connect_timeout):
            pass
    except OSError:
        return None
    base = f"http://{ip}" + ("" if port == 80 else f":{port}")
    try:
        si = _get_json(base + "/machine/system_info", 2)["result"]["system_info"]
        pi = si.get("product_info") or {}
        if pi:
            return {"name": pi.get("device_name"), "host": ip, "port": port, "serial": pi.get("serial_number"),
                    "firmware": pi.get("firmware_version"), "model": pi.get("machine_type") or "Snapmaker",
                    "how": "http"}
    except Exception:  # noqa: BLE001 - not a U1; try plain Moonraker
        pass
    try:
        info = _get_json(base + "/server/info", 2)["result"]
        if "moonraker_version" in info or "klippy_state" in info:
            name = None
            try:
                name = _get_json(base + "/printer/info", 2)["result"].get("hostname")
            except Exception:  # noqa: BLE001
                pass
            return {"name": name, "host": ip, "port": port, "serial": None, "firmware": info.get("moonraker_version"),
                    "model": "Klipper / Moonraker", "how": "http"}
    except Exception:  # noqa: BLE001 - not Moonraker
        pass
    return None


def http_sweep(subnet: Optional[str] = None, hosts: Optional[list[str]] = None,
               on_found: Optional[Callable[[dict], None]] = None,
               on_progress: Optional[Callable[[int, int], None]] = None,
               cancel: Optional[threading.Event] = None) -> list[dict]:
    if hosts is None:
        subnet = subnet or local_subnet()
        if not subnet:
            return []
        hosts = [f"{subnet}.{i}" for i in range(1, 255)]
    out, done = [], 0
    ex = cf.ThreadPoolExecutor(max_workers=64)
    try:
        futs = [ex.submit(probe_host, h) for h in hosts]
        for f in cf.as_completed(futs):
            done += 1
            r = f.result()
            if r:
                out.append(r)
                if on_found:
                    on_found(r)
            if on_progress:
                on_progress(done, len(hosts))
            if cancel and cancel.is_set():
                for x in futs:
                    x.cancel()
                break
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    return out


def _sort_key(r: dict):
    return tuple(int(x) for x in r["host"].split(".") if x.isdigit())


class ScanJob:
    """One on-demand network search. Thread-safe snapshot() for the UI to poll:
    printers appear as they're found; checked/total is the sweep's real progress."""

    def __init__(self, subnet: Optional[str] = None, mdns_seconds: float = 3.0):
        self.subnet, self.mdns_seconds = subnet, mdns_seconds
        self.cancel = threading.Event()
        self._lock = threading.Lock()
        self._merged: dict[str, dict] = {}
        self.checked, self.total, self.done = 0, 254, False
        self.started = time.time()
        self.error: Optional[str] = None

    def _add(self, r: dict) -> None:
        with self._lock:
            key = r.get("serial") or r["host"]
            # an mDNS hit and a sweep hit for the same printer: merge (serial first, then IP)
            if key not in self._merged:                 # same IP: merge only if serials don't conflict
                key = next((k for k, v in self._merged.items() if v.get("host") == r["host"]
                            and (not v.get("serial") or not r.get("serial") or v["serial"] == r["serial"])), key)
            cur = self._merged.setdefault(key, {})
            for k, v in r.items():
                if v and not cur.get(k):
                    cur[k] = v
            if not cur.get("driver"):
                cur["driver"] = "snapmaker.u1" if "snapmaker" in (cur.get("model") or "").lower() \
                    else "klipper.generic"

    def _progress(self, done: int, total: int) -> None:
        self.checked, self.total = done, total

    def run(self) -> None:
        try:
            from . import sdcp_scan
            with cf.ThreadPoolExecutor(max_workers=3) as ex:
                m = ex.submit(mdns_scan, self.mdns_seconds, self._add)
                e = ex.submit(sdcp_scan.broadcast_scan, 2.5, self._add, self.subnet or local_subnet())
                h = ex.submit(http_sweep, self.subnet, None, self._add, self._progress, self.cancel)
                h.result()
                m.result()
                e.result()
        except Exception as e:  # noqa: BLE001 - report instead of hanging the UI
            self.error = f"{type(e).__name__}: {e}"
        finally:
            self.checked = self.total if not self.cancel.is_set() else self.checked
            self.done = True

    def snapshot(self) -> dict:
        with self._lock:
            found = sorted((dict(v) for v in self._merged.values()), key=_sort_key)
        return {"done": self.done, "checked": self.checked, "total": self.total, "found": found,
                "seconds": round(time.time() - self.started, 1), "error": self.error,
                "cancelled": self.cancel.is_set()}


def scan(subnet: Optional[str] = None, mdns_seconds: float = 3.0) -> list[dict]:
    """Blocking: whole search, merged by serial (then IP). Each result: name, host, port, serial,
    firmware, model, driver ("snapmaker.u1" | "klipper.generic")."""
    job = ScanJob(subnet, mdns_seconds)
    job.run()
    return job.snapshot()["found"]
