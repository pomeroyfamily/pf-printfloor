"""Runs one driver per configured printer, keeps the latest status, and fans updates out
to every open browser (Server-Sent Events)."""
from __future__ import annotations

import asyncio
import ipaddress
import socket
import logging
import uuid
from types import SimpleNamespace
from datetime import datetime, timezone
import time
from typing import Any, Optional

from . import config
from .cloud import bambu as bambu_cloud
from .discovery.bambu_ssdp import Announcement, BambuSSDP
from .cameras import Cameras, builtin_kind, safe_url
from .history import History, usage_key
from .plugs import Plugs
from .autocycle import AutoCycle, settings_of as autocycle_settings
from .drivers import driver_for_model_code, get_driver
from .store import DuplicatePrinter
from .drivers.base import PrinterBase
from .models import ConnState, PrinterStatus
from .store import PrinterConfig, PrinterStore

from .transports.tls import peer_common_name

log = logging.getLogger(__name__)


def _answers(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), 3):
            return True
    except OSError:
        return False


def _is_ip(text: str) -> bool:
    try:
        ipaddress.ip_address(text)
        return True
    except ValueError:
        return False


MAX_TAGS, MAX_TAG_LEN = 20, 30


def normalize_tags(tags, limit: Optional[int] = MAX_TAGS) -> list[str]:
    """Trim, collapse spaces, drop empties, de-duplicate ignoring case (first spelling wins)."""
    out, seen = [], set()
    for t in tags or []:
        t = " ".join(str(t).split())[:MAX_TAG_LEN]
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out[:limit] if limit else out


class Manager:
    def __init__(self, store: PrinterStore, ssdp: Optional[BambuSSDP] = None):
        self.store = store
        self.history = History(store._db, store._lock)
        self.ssdp = ssdp or BambuSSDP()
        self.ssdp.listeners.append(self._on_bambu_announcement)
        self.drivers: dict[str, PrinterBase] = {}
        self.status: dict[str, PrinterStatus] = {}
        self._moving: set[str] = set()                # printers being moved to an announced new address
        self.reported_names: dict[str, str] = {}     # last name each printer reported (see _auto_rename)
        self._subs: set[asyncio.Queue] = set()
        self._bambu_hours_task: Optional[asyncio.Task] = None
        self.cameras = Cameras()
        self.plugs = Plugs(store)
        self.autocycle = AutoCycle(store, self.plugs, self.status.get, self.broadcast_printers)
        self._conn_was: dict[str, Any] = {}
        self._boots: dict[str, dict] = dict(store.get_app_setting("bambu_boot") or {})   # pid -> seq, booted_at
        self._boots_saved_at: Optional[float] = None     # None = not saved yet this run

    # ------------------------------------------------------------------ lifecycle
    async def start(self, drivers: bool = True) -> None:
        self._loop = asyncio.get_running_loop()
        self._use_account(bambu_cloud.account_from_store(self.store))
        self.plugs.start(self.broadcast_printers, self.status.get)
        await self.ssdp.start()
        if drivers:
            for cfg in self.store.list():
                self._start(cfg)
            self._bambu_hours_task = asyncio.create_task(self._bambu_hours_loop())
            self.autocycle.start(self.store.list)

    async def stop(self) -> None:
        if self._bambu_hours_task:
            self._bambu_hours_task.cancel()
        self.cameras.stop_all()
        self.plugs.stop()
        self.autocycle.stop()
        try:
            self._save_last_seen()
            self._save_boots()
        except Exception:  # noqa: BLE001
            pass
        self.ssdp.close()
        await asyncio.gather(*(d.stop() for d in self.drivers.values()), return_exceptions=True)
        self.drivers.clear()

    # ------------------------------------------------------------------ Bambu lifetime hours (account)
    BAMBU_HOURS_EVERY_S = 600

    async def _bambu_hours_loop(self) -> None:
        await asyncio.sleep(20)                                  # let drivers connect / serials arrive first
        while True:
            try:
                await self.refresh_bambu_hours()
            except Exception as e:  # noqa: BLE001 - never kill the loop
                log.info("Bambu lifetime hours: %s", e)
            await asyncio.sleep(self.BAMBU_HOURS_EVERY_S)

    async def refresh_bambu_hours(self) -> int:
        """Bambu printers' lifetime print time from the linked account's device list (`total_print_time`). The unit
        differs by model (driver class `account_hours_unit_s`): X1C seconds; P1P, A1 minutes (verified against the printer
        screens 2026-10-30); A1 Mini minutes (same family, screen not checked).
        Bambu only adds successfully finished jobs, so the audit compares against finished jobs. Returns # updated."""
        acct = bambu_cloud.get_account()
        if not acct or acct.state != "linked":
            return 0
        try:
            devices = await asyncio.to_thread(acct.cloud.devices)
        except bambu_cloud.TokenExpired as e:
            acct.state, acct.last_error = "needs_login", str(e)
            return 0
        raw = {d.get("dev_id"): d.get("total_print_time") for d in devices if d.get("dev_id")}
        n = 0
        for cfg in self.store.list():
            if not (cfg.driver.startswith("bambu.") and cfg.serial):
                continue
            try:
                unit = get_driver(cfg.driver).account_hours_unit_s
            except Exception:  # noqa: BLE001
                unit = None
            v = raw.get(cfg.serial)
            if unit and isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0:
                self.history.report_lifetime(usage_key(cfg), float(v) * unit, None, basis="finished")
                n += 1
        return n

    # ------------------------------------------------------------------ names that follow the printer
    # settings.auto_name = "Use the printer's name": the card shows whatever name the printer reports, and
    # follows it when it is renamed on the printer. A printer added with a blank name starts this way (with a
    # placeholder "P1P at 10.0.0.189" until it reports). Typing a name in the dashboard turns it off; ticking
    # the box turns it back on. self.reported_names keeps the last name each printer reported, so ticking the
    # box can apply it at once and the edit form can show it.
    def _auto_rename(self, printer_id: str, name: Optional[str]) -> None:
        name = " ".join((name or "").split())[:60]
        if not name:
            return
        self.reported_names[printer_id] = name
        cfg = self.store.get(printer_id)
        if not cfg or not cfg.settings.get("auto_name") or cfg.name == name:
            return
        new = self.store.update(printer_id, name=name)
        if printer_id in self.drivers:
            self.drivers[printer_id].config = new
        log.info("printer %s: renamed %r -> %r (the printer's own name)", printer_id, cfg.name, name)
        self.broadcast_printers()

    def _on_bambu_announcement(self, a: Announcement) -> None:
        for cfg in self.store.list():
            if cfg.driver.startswith("bambu.") and (cfg.serial == a.serial if cfg.serial else cfg.host == a.host):
                self._auto_rename(cfg.id, a.name)
                if (cfg.serial and a.host and a.host != cfg.host and _is_ip(a.host) and _is_ip(cfg.host)
                        and a.sender == a.host                # sent from the address it claims
                        and cfg.id not in self._moving):     # a typed host name (DNS) is left alone
                    self._moving.add(cfg.id)
                    asyncio.get_running_loop().create_task(self._follow_new_address(cfg.id, a.host))

    async def _follow_new_address(self, printer_id: str, new_host: str) -> None:
        """A Bambu printer we know (same serial number) announced itself at a new address - e.g. after a Wi-Fi change
        or a new DHCP lease. Use the new address and reconnect; Details shows the change."""
        try:
            cfg = self.store.get(printer_id)
            if not cfg or cfg.host == new_host:
                return
            # Safety: the access code is sent to whatever address we connect to, so only move when the printer is
            # really gone from the old address AND the new address presents this printer's own certificate
            # (Bambu printers' TLS certificate is issued to their serial number).
            port = int(cfg.settings.get("mqtt_port") or 8883)
            if await asyncio.to_thread(_answers, cfg.host, port):
                log.info("printer %s: announced at %s but still answers at %s - not moving", printer_id, new_host,
                         cfg.host)
                return
            cn = await asyncio.to_thread(peer_common_name, new_host, port)
            if cn != cfg.serial:
                log.warning("printer %s: %s claims to be it but its certificate says %r - not moving", printer_id,
                            new_host, cn)
                return
            moved = {"from": cfg.host, "to": new_host, "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            new = self.store.update(printer_id, host=new_host, settings={**cfg.settings, "address_changed": moved})
            log.info("printer %s: now at %s (was %s) - it announced the new address", printer_id, new_host, cfg.host)
            await self._restart(new)
            self.broadcast_printers()
        finally:
            self._moving.discard(printer_id)

    def _heard_name(self, cfg: PrinterConfig) -> Optional[str]:
        if not cfg.driver.startswith("bambu."):
            return None
        a = self.ssdp.find(cfg.serial, cfg.host)
        return (" ".join((a.name or "").split())[:60] or None) if a else None

    def _start(self, cfg: PrinterConfig) -> None:
        try:
            cls = get_driver(cfg.driver)
        except KeyError:
            log.error("printer %s: unknown driver %s", cfg.id, cfg.driver)
            return
        d = cls(cfg, self._on_update)
        seen = (self.store.get_app_setting("last_seen") or {}).get(cfg.id)
        if seen and d.status.last_seen_at is None:
            try:
                d.status.last_seen_at = datetime.fromisoformat(seen)
            except ValueError:
                pass
        self.drivers[cfg.id] = d
        self.status[cfg.id] = d.status
        d.start()

    async def _restart(self, cfg: PrinterConfig) -> None:
        old = self.drivers.pop(cfg.id, None)
        if old:
            await old.stop()
        self._start(cfg)

    # ------------------------------------------------------------------ events
    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    def _broadcast(self, event: dict) -> None:
        for q in list(self._subs):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:          # stalled browser tab: drop it, it will reconnect
                self._subs.discard(q)

    def _save_last_seen(self, ids=None) -> None:
        """Persist last-seen times (when a printer drops, and on shutdown) so offline cards survive a restart."""
        saved = dict(self.store.get_app_setting("last_seen") or {})
        for pid, st in self.status.items():
            if (ids is None or pid in ids) and st.last_seen_at:
                saved[pid] = st.last_seen_at.isoformat()
        known = {c.id for c in self.store.list()}
        self.store.set_app_setting("last_seen", {k: v for k, v in saved.items() if k in known})

    BOOT_DROP = 50              # counter must fall by more than this ...
    BOOT_SAVE_EVERY_S = 600

    def _track_boot(self, st: PrinterStatus) -> None:
        """Bambus don't report uptime, but their report counter (sequence_id) starts over when they boot
        (verified on the A1 Mini 2026-11-05: 27026 -> 28). A drop, seen on two reports in a row, = a reboot,
        dated when noticed. Also catches reboots while the dashboard was down (dated when it came back up -
        that gap is small and corrects itself on the next reboot). Saved so it survives restarts."""
        rec = self._boots.setdefault(st.printer_id, {})
        seq, last = st.report_seq, rec.get("seq")
        if seq is None:
            return
        if last is not None and seq + self.BOOT_DROP < last:
            if rec.get("cand") is not None and rec["cand"] <= seq:      # second low reading: confirmed
                rec.update(seq=seq, booted_at=datetime.now(timezone.utc).replace(second=0, microsecond=0)
                           .isoformat())
                rec.pop("cand", None)
                log.info("printer %s: report counter started over - rebooted", st.printer_id)
                self._save_boots()
            else:
                rec["cand"] = seq
            return
        rec["seq"] = seq
        rec.pop("cand", None)
        if self._boots_saved_at is None or time.monotonic() - self._boots_saved_at > self.BOOT_SAVE_EVERY_S:
            self._save_boots()

    def _save_boots(self) -> None:
        self._boots_saved_at = time.monotonic()
        known = {c.id for c in self.store.list()}
        # setting name kept from the Bambu version; it holds the CC's boot info too
        self.store.set_app_setting("bambu_boot", {k: {kk: vv for kk, vv in v.items() if kk != "cand"}
                                                  for k, v in self._boots.items() if k in known})

    def _track_cc_boot(self, st: PrinterStatus) -> None:
        """CC: no uptime or counter, but its position reads exactly 0,0,0 after power-on until it moves; homing
        gives a real position (verified 2026-11-05). Moved/homed -> 0,0,0 = rebooted, dated when noticed.
        Remembered across dashboard restarts like the Bambu counter."""
        rec = self._boots.setdefault(st.printer_id, {})
        if st.unhomed and rec.get("homed") is True:
            rec.update(homed=False, booted_at=datetime.now(timezone.utc).replace(second=0, microsecond=0).isoformat())
            log.info("printer %s: position back to 0,0,0 - rebooted", st.printer_id)
            self._save_boots()
        elif st.unhomed is False and rec.get("homed") is not True:
            rec["homed"] = True
            self._save_boots()

    async def _on_update(self, st: PrinterStatus) -> None:
        if st.unhomed is not None and st.connection == ConnState.ONLINE:
            try:
                self._track_cc_boot(st)
            except Exception:  # noqa: BLE001
                log.exception("boot tracking failed")
        if st.report_seq is not None:
            try:
                self._track_boot(st)
            except Exception:  # noqa: BLE001
                log.exception("boot tracking failed")
        if st.booted_at is None and (self._boots.get(st.printer_id) or {}).get("booted_at"):
            st.booted_at = datetime.fromisoformat(self._boots[st.printer_id]["booted_at"])
        prev = self._conn_was.get(st.printer_id)
        self._conn_was[st.printer_id] = st.connection
        if prev == ConnState.ONLINE and st.connection != ConnState.ONLINE:
            try:
                self._save_last_seen({st.printer_id})
            except Exception:  # noqa: BLE001
                log.warning("could not save last-seen for %s", st.printer_id)
        self.status[st.printer_id] = st
        try:
            self.autocycle.observe(st)
        except Exception:  # noqa: BLE001
            log.exception("auto power cycle: observe failed")
        d = self.drivers.get(st.printer_id)
        if d and d.learned_serial and not d.config.serial:
            try:                                  # keep the serial: lets discovery mark it "Added"
                old_key = usage_key(d.config)
                d.config = self.store.update(st.printer_id, serial=d.learned_serial)
                self.history.rekey(old_key, usage_key(d.config))   # history follows the hardware identity
            except Exception:  # noqa: BLE001 - e.g. same printer added twice
                log.warning("printer %s: could not save serial %s", st.printer_id, d.learned_serial)
            d.learned_serial = None
        if d and d.learned_access_code:
            code, d.learned_access_code = d.learned_access_code, None
            try:
                d.config = self.store.update(st.printer_id, secrets={**d.config.secrets, "access_code": code})
                log.info("printer %s: saved the new access code", st.printer_id)
            except Exception:  # noqa: BLE001
                log.warning("printer %s: could not save the new access code", st.printer_id)
        if st.device_name:
            self._auto_rename(st.printer_id, st.device_name)
        cfg = d.config if d else self.store.get(st.printer_id)
        if cfg:
            try:
                if st.lifetime_print_s is not None:
                    self.history.report_lifetime(usage_key(cfg), st.lifetime_print_s, st.lifetime_jobs)
                pending = getattr(d, "pending_jobs", None)
                if pending:
                    items, d.pending_jobs = list(pending), []
                    added = self.history.import_printer_jobs(cfg, items)
                    if added:
                        log.info("printer %s: added %d past job(s) from the printer's history", cfg.id, added)
                        self._broadcast({"type": "job", "printer_id": cfg.id, "imported": added})
                for ev in self.history.observe(cfg, st):
                    self.on_job_event(ev)
            except Exception:  # noqa: BLE001 - history must never break live status
                log.exception("printer %s: job history update failed", st.printer_id)
        self._broadcast({"type": "status", "id": st.printer_id, "status": st.model_dump(mode="json")})

    def on_job_event(self, ev: dict) -> None:
        """Job started/ended. Notifications hook in here."""
        log.info("printer %s: %s %s %s", ev.get("printer_id"), ev["type"], ev.get("job"), ev.get("result", ""))
        self._broadcast({"type": "job", **ev})

    def broadcast_printers(self) -> None:
        self._broadcast({"type": "printers", "printers": self.printers(), "layout": self.layout()})

    # ------------------------------------------------------------------ views
    def printer_view(self, cfg: PrinterConfig) -> dict[str, Any]:
        d = cfg.to_public()
        try:
            cls = get_driver(cfg.driver)
            d.update(maker=cls.maker, model=cfg.settings.get("model_label") or cls.model_name, family=cls.family,
                     fields=[f.model_dump() for f in cls.fields])
        except KeyError:
            d.update(maker="Unknown", model=cfg.driver, family="", fields=[])
        d["tags"] = cfg.settings.get("tags") or []
        d["follow_name"] = bool(cfg.settings.get("auto_name"))
        d["reported_name"] = self.reported_names.get(cfg.id) or self._heard_name(cfg)
        d["location"] = self.location_of(cfg)
        if cfg.driver.startswith("bambu."):
            d["connection"] = cfg.settings.get("connection") or "auto"
        url = (cfg.settings.get("camera_url") or "").strip()
        d["camera"] = {"kind": builtin_kind(cfg), "url": safe_url(url) if url else "",
                       "url_kind": cfg.settings.get("camera_kind") or "auto"}
        st = self.status.get(cfg.id)
        d["status"] = st.model_dump(mode="json") if st else None
        d["power"] = self.plugs.view(cfg.id, cfg.settings.get("plug"), st)
        d["autocycle"] = self.autocycle.view(cfg) if d["power"] else None
        return d

    def printers(self) -> list[dict[str, Any]]:
        return [self.printer_view(c) for c in self.store.list()]

    # ------------------------------------------------------------------ changes
    async def add(self, name: Optional[str], driver: str, host: str, serial: Optional[str],
                  settings: dict, secrets: dict, location: Optional[str] = None) -> PrinterConfig:
        cls = get_driver(driver)                  # KeyError for unknown drivers
        settings = dict(settings)
        locs = self.locations()
        loc = location or settings.get("location")
        settings["location"] = loc if any(x["id"] == loc for x in locs["items"]) else locs["default"]
        name = (name or "").strip()
        if not name:                              # use the printer's own name once it reports it
            settings["auto_name"] = True
            name = f"{cls.model_name} at {host}"
        cfg = self.store.add(name, driver, host, serial, settings, {k: v for k, v in secrets.items() if v})
        self._start(cfg)
        heard = self.ssdp.find(serial, host) if driver.startswith("bambu.") else None
        if heard:                                 # already heard its announcement: name it now
            self._auto_rename(cfg.id, heard.name)
            cfg = self.store.get(cfg.id) or cfg
        else:
            self.broadcast_printers()
        return cfg

    async def update(self, printer_id: str, name: Optional[str] = None, host: Optional[str] = None,
                     serial: Optional[str] = None, secrets: Optional[dict] = None,
                     tags: Optional[list[str]] = None, camera_url: Optional[str] = None,
                     camera_kind: Optional[str] = None, location: Optional[str] = None,
                     connection: Optional[str] = None, follow_name: Optional[bool] = None) -> PrinterConfig:
        cfg = self.store.get(printer_id)
        if not cfg:
            raise KeyError(printer_id)
        fields: dict[str, Any] = {}
        if location is not None:
            if not any(x["id"] == location for x in self.locations()["items"]):
                raise ValueError("unknown location")
            fields["settings"] = {**cfg.settings, "location": location}
        follow_now = None
        if follow_name is True:                   # "Use the printer's name": takes its name now (if known) and later
            fields["settings"] = {**fields.get("settings", cfg.settings), "auto_name": True}
            follow_now = self.reported_names.get(printer_id) or self._heard_name(cfg)
            if follow_now:
                fields["name"] = follow_now
        elif (follow_name is False or (name is not None and name != cfg.name)) and cfg.settings.get("auto_name"):
            fields["settings"] = {k: v for k, v in fields.get("settings", cfg.settings).items() if k != "auto_name"}
        if follow_name is not True and name is not None and name != cfg.name:
            fields["name"] = name                 # a typed name wins from now on
        restart = False
        if connection is not None and connection != (cfg.settings.get("connection") or "auto"):
            if not cfg.driver.startswith("bambu."):
                raise ValueError("connection mode is for Bambu printers")
            fields["settings"] = {**fields.get("settings", cfg.settings), "connection": connection}
            restart = True
        if tags is not None:
            settings = dict(fields.get("settings", cfg.settings))
            settings["tags"] = normalize_tags(tags)
            fields["settings"] = settings
        if camera_url is not None or camera_kind is not None:
            settings = dict(fields.get("settings", cfg.settings))
            if camera_url is not None:
                url = camera_url.strip()
                if "***@" in url:                       # masked value sent back unchanged: keep the saved one
                    url = cfg.settings.get("camera_url") or ""
                settings["camera_url"] = url
            if camera_kind is not None:
                settings["camera_kind"] = camera_kind
            fields["settings"] = settings
        if host is not None and host != cfg.host:
            fields["host"] = host
            if cfg.settings.get("address_changed"):    # typed by hand: the old "it moved" note no longer applies
                fields["settings"] = {k: v for k, v in fields.get("settings", cfg.settings).items()
                                      if k != "address_changed"}
        if serial is not None:
            fields["serial"] = serial or None
        if secrets:
            merged = dict(cfg.secrets)
            merged.update({k: v for k, v in secrets.items() if v})   # blank = keep the saved value
            fields["secrets"] = merged
        new = self.store.update(printer_id, **fields) if fields else cfg
        if restart or any(k in fields for k in ("host", "serial", "secrets")):
            await self._restart(new)
        elif printer_id in self.drivers:
            self.drivers[printer_id].config = new             # name/tags only: no reconnect needed
        self.broadcast_printers()
        return new

    async def remove(self, printer_id: str) -> bool:
        d = self.drivers.pop(printer_id, None)
        if d:
            await d.stop()
        self.status.pop(printer_id, None)
        self.cameras.forget(printer_id)
        ok = self.store.remove(printer_id)
        self.history.close_orphans({c.id for c in self.store.list()})   # its history is kept
        (config.THUMB_DIR / f"{printer_id}.png").unlink(missing_ok=True)
        self.broadcast_printers()
        return ok

    # ------------------------------------------------------------------ Bambu account printers
    def _bambu_added(self, serial: str, host: Optional[str]) -> Optional[PrinterConfig]:
        for cfg in self.store.list():
            if cfg.driver.startswith("bambu.") and (cfg.serial == serial or (host and not cfg.serial
                                                                          and cfg.host == host)):
                return cfg
        return None

    def account_printers(self, devices: list[dict]) -> list[dict[str, Any]]:
        """Printers bound to the Bambu account, with the IP heard on the LAN and Added / Not added.
        Access codes stay on the server."""
        out = []
        for d in devices:
            serial = str(d.get("dev_id") or "")
            cls = driver_for_model_code("bambu", d.get("dev_model_name"))
            heard = self.ssdp.find(serial)
            host = heard.host if heard else None
            added = self._bambu_added(serial, host)
            if added:
                host = added.host
            out.append({"serial": serial, "name": d.get("name") or serial,
                        "model": d.get("dev_product_name") or d.get("dev_model_name"),
                        "driver": cls.driver_key if cls else None, "supported": bool(cls),
                        "online": d.get("online"), "host": host,
                        "added_id": added.id if added else None})
        return out

    async def import_bambu(self, devices: list[dict], picks: list[dict],
                           location: Optional[str] = None) -> list[dict[str, Any]]:
        by_serial = {str(d.get("dev_id")): d for d in devices}
        results = []
        for pick in picks:
            serial = str(pick.get("serial") or "")
            d = by_serial.get(serial)
            res: dict[str, Any] = {"serial": serial}
            cls = driver_for_model_code("bambu", d.get("dev_model_name")) if d else None
            heard = self.ssdp.find(serial)
            host = (pick.get("host") or "").strip() or (heard.host if heard else "")
            if not d:
                res["error"] = "not on the Bambu account"
            elif not cls:
                res["error"] = f"{d.get('dev_product_name') or d.get('dev_model_name')} is not supported yet"
            elif not host:
                res["error"] = "IP address unknown: type it in"
            elif not d.get("dev_access_code"):
                res["error"] = "the account did not return an access code"
            elif self._bambu_added(serial, host):
                res["error"] = "already added"
            else:
                try:
                    cfg = await self.add(d.get("name") or None, cls.driver_key, host, serial, {},
                                         {"access_code": str(d["dev_access_code"])}, location)
                    res["id"] = cfg.id
                except DuplicatePrinter:
                    res["error"] = "already added"
            results.append(res)
        return results

    # ------------------------------------------------------------------ network scan (U1 / Klipper)
    def mark_network(self, found: list[dict]) -> list[dict[str, Any]]:
        """Scan results + which are already added (serial first, then IP)."""
        mine = [c for c in self.store.list() if c.driver in ("snapmaker.u1", "klipper.generic", "elegoo.cc1")]
        out = []
        for r in found:
            same = [c for c in mine if c.driver.split(".")[0] == (r.get("driver") or "").split(".")[0]]
            hit = next((c for c in same if r.get("serial") and c.serial == r["serial"]), None) or \
                next((c for c in same if c.host == r.get("host")), None)
            out.append({**r, "added_id": hit.id if hit else None})
        return out

    # ------------------------------------------------------------------ tags (across printers)
    def tags(self) -> list[dict[str, Any]]:
        """Every tag: the ones on printers plus the ones created in Manage tags that no printer has (yet)."""
        counts: dict[str, list] = {}
        for t in self.store.get_app_setting("tag_list") or []:
            counts.setdefault(t.lower(), [t, 0, []])
        for cfg in self.store.list():
            for t in cfg.settings.get("tags") or []:
                row = counts.setdefault(t.lower(), [t, 0, []])
                row[1] += 1
                row[2].append(cfg.id)
        return sorted(({"name": n, "count": c, "printers": ids} for n, c, ids in counts.values()),
                      key=lambda r: r["name"].lower())

    def _tag_list(self, change) -> None:
        """Update the saved list of defined tags (kept even when no printer has the tag)."""
        self.store.set_app_setting("tag_list", normalize_tags(change(list(self.store.get_app_setting("tag_list") or [])),
                                                              limit=None))

    def set_tag_printers(self, name: str, printer_ids: list[str]) -> dict[str, Any]:
        """Create tag `name` (if new) and put it on exactly these printers. Returns the tag's row."""
        name = (normalize_tags([name]) or [None])[0]
        if not name:
            raise ValueError("enter a tag name")
        known = {c.id for c in self.store.list()}
        unknown = set(printer_ids) - known
        if unknown:
            raise KeyError(sorted(unknown)[0])
        existing = next((t["name"] for t in self.tags() if t["name"].lower() == name.lower()), name)
        want = set(printer_ids)
        for cfg in self.store.list():
            tags = cfg.settings.get("tags") or []
            has = any(t.lower() == name.lower() for t in tags)
            if (cfg.id in want) == has:
                continue
            new_tags = normalize_tags(tags + [existing]) if cfg.id in want else [t for t in tags if t.lower() != name.lower()]
            if cfg.id in want and len(new_tags) == len(tags):
                raise ValueError(f"{cfg.name} already has the most tags allowed ({MAX_TAGS})")
            upd = self.store.update(cfg.id, settings={**cfg.settings, "tags": new_tags})
            if cfg.id in self.drivers:
                self.drivers[cfg.id].config = upd
        self._tag_list(lambda lst: lst + [existing])
        self.broadcast_printers()
        return next(t for t in self.tags() if t["name"].lower() == name.lower())

    def retag(self, old: str, new: Optional[str]) -> int:
        """Rename tag `old` to `new` on every printer (None = delete). Case-insensitive. Returns printers changed."""
        changed = 0
        for cfg in self.store.list():
            tags = cfg.settings.get("tags") or []
            if not any(t.lower() == old.lower() for t in tags):
                continue
            repl = [new if t.lower() == old.lower() else t for t in tags] if new else \
                [t for t in tags if t.lower() != old.lower()]
            settings = {**cfg.settings, "tags": normalize_tags(repl)}
            upd = self.store.update(cfg.id, settings=settings)
            if cfg.id in self.drivers:
                self.drivers[cfg.id].config = upd
            changed += 1
        self._tag_list(lambda lst: [new if t.lower() == old.lower() else t for t in lst] if new
                       else [t for t in lst if t.lower() != old.lower()])
        self.broadcast_printers()
        return changed

    # ------------------------------------------------------------------ automatic power cycle
    def set_autocycle(self, pid: str, enabled: bool, at: str, idle_min: int, tz: Optional[str]) -> dict:
        cfg = self.store.get(pid)
        if not cfg:
            raise KeyError(pid)
        if enabled and not (cfg.settings.get("plug") or {}).get("device"):
            raise ValueError("assign a smart plug first")
        ac = autocycle_settings(SimpleNamespace(settings={"autocycle": {"enabled": enabled, "time": at,
                                                                         "idle_min": idle_min}}))
        new = self.store.update(pid, settings={**cfg.settings, "autocycle": ac})
        if pid in self.drivers:
            self.drivers[pid].config = new
        if tz and not (self.store.get_app_setting("dashboard") or {}).get("tz"):
            self.set_dashboard(None, None, tz)
        self.autocycle.configure(new, enabled, at, idle_min)
        return self.printer_view(new)

    # ------------------------------------------------------------------ dashboard name + locations
    DEFAULT_LOCATION = "Home"

    def locations(self) -> dict[str, Any]:
        """{"items": [{"id", "name"}] in the user's order, "default": id}. There is always at least one."""
        v = self.store.get_app_setting("locations") or {}
        items = [x for x in v.get("items") or [] if x.get("id") and x.get("name")]
        if not items:
            items = [{"id": uuid.uuid4().hex[:8], "name": self.DEFAULT_LOCATION}]
            v = {"items": items, "default": items[0]["id"]}
            self.store.set_app_setting("locations", v)
        default = v.get("default") if any(x["id"] == v.get("default") for x in items) else items[0]["id"]
        return {"items": items, "default": default}

    def location_of(self, cfg: PrinterConfig) -> str:
        locs = self.locations()
        loc = cfg.settings.get("location")
        return loc if any(x["id"] == loc for x in locs["items"]) else locs["default"]

    def set_locations(self, items: list[dict], default: Optional[str]) -> dict[str, Any]:
        """Replace the list (order = list order). Items without an id are new. Printers in a removed location
        move to the default."""
        clean, seen = [], set()
        for x in items:
            name = " ".join(str(x.get("name") or "").split())[:40]
            if not name:
                raise ValueError("a location needs a name")
            if name.lower() in seen:
                raise ValueError(f"two locations are called {name}")
            seen.add(name.lower())
            clean.append({"id": x.get("id") or uuid.uuid4().hex[:8], "name": name, "_new_ref": x.get("ref")})
        if not clean:
            raise ValueError("keep at least one location")
        ids = {x["id"] for x in clean}
        by_ref = {x["_new_ref"]: x["id"] for x in clean if x["_new_ref"]}
        default = by_ref.get(default, default)
        if default not in ids:
            default = clean[0]["id"]
        for x in clean:
            x.pop("_new_ref")
        self.store.set_app_setting("locations", {"items": clean, "default": default})
        for cfg in self.store.list():                        # removed location -> default
            if cfg.settings.get("location") not in ids:
                upd = self.store.update(cfg.id, settings={**cfg.settings, "location": default})
                if cfg.id in self.drivers:
                    self.drivers[cfg.id].config = upd
        self.broadcast_printers()
        return self.locations()

    def dashboard(self) -> dict[str, Any]:
        v = self.store.get_app_setting("dashboard") or {}
        return {"name": v.get("name") or "PF PrintFloor", "banner": v.get("banner", True) is not False,
                "tz": v.get("tz") or None}

    def set_dashboard(self, name: Optional[str], banner: Optional[bool], tz: Optional[str] = None) -> dict[str, Any]:
        cur = self.dashboard()
        if tz is not None:
            from zoneinfo import ZoneInfo
            try:
                ZoneInfo(tz)
            except Exception as e:  # noqa: BLE001
                raise ValueError(f"unknown time zone {tz}") from e
            cur["tz"] = tz
        if name is not None:
            cur["name"] = " ".join(name.split())[:60] or "PF PrintFloor"
        if banner is not None:
            cur["banner"] = bool(banner)
        self.store.set_app_setting("dashboard", cur)
        self.broadcast_printers()
        return cur

    def layout(self) -> dict[str, Any]:
        return {"dashboard": self.dashboard(), "locations": self.locations()}

    def reorder(self, ids: list[str]) -> None:
        self.store.reorder(ids)
        self.broadcast_printers()

    def set_bambu_account(self, acct: Optional[bambu_cloud.Account]) -> None:
        self._use_account(acct)
        self._broadcast({"type": "account"})

    def _use_account(self, acct: Optional[bambu_cloud.Account]) -> None:
        if acct:
            acct.on_rejected = self._account_rejected
        bambu_cloud.set_account(acct)

    def _account_rejected(self) -> None:
        """Bambu refused the saved sign-in (may be called from a worker thread): save it, tell the browsers."""
        bambu_cloud.mark_rejected(self.store)
        loop = getattr(self, "_loop", None)
        if loop and not loop.is_closed():
            loop.call_soon_threadsafe(self._broadcast, {"type": "account"})
