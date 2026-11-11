"""Web app: JSON API + live events (SSE) + the dashboard page.

Run:  python -m app            (http://localhost:8765)
"""
from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import config
from .cloud import bambu as bambu_cloud
from .discovery import klipper_scan
from .discovery.bambu_ssdp import BambuSSDP
from .drivers import catalog, get_driver
from .cameras import CameraUnavailable, FRAME_TIMEOUT_S
from .history import usage_key
from .plugs import GoveeAuthError, GoveeError, suggest_plugs
from .manager import Manager
from .store import DuplicatePrinter, PrinterStore

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"
KEEPALIVE_S = 15


class AddPrinter(BaseModel):
    driver: str
    name: Optional[str] = Field(default=None, max_length=60)   # blank: use the printer's own name
    host: str = Field(min_length=1)
    serial: Optional[str] = None
    settings: dict[str, Any] = Field(default_factory=dict)
    secrets: dict[str, str] = Field(default_factory=dict)
    location: Optional[str] = Field(default=None, max_length=40)


class EditPrinter(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=60)
    host: Optional[str] = None
    serial: Optional[str] = None
    secrets: dict[str, str] = Field(default_factory=dict)
    tags: Optional[list[str]] = None
    camera_url: Optional[str] = Field(default=None, max_length=500)
    camera_kind: Optional[str] = Field(default=None, pattern="^(auto|mjpeg|snapshot)$")
    location: Optional[str] = Field(default=None, max_length=40)
    connection: Optional[str] = Field(default=None, pattern="^(auto|local|cloud)$")   # Bambu only
    follow_name: Optional[bool] = None    # "Use the printer's name"


class LocationItem(BaseModel):
    id: Optional[str] = Field(default=None, max_length=40)     # None = new
    ref: Optional[str] = Field(default=None, max_length=40)    # client key for a new one (to pick it as default)
    name: str = Field(max_length=60)


class Locations(BaseModel):
    items: list[LocationItem] = Field(min_length=1, max_length=50)
    default: Optional[str] = Field(default=None, max_length=40)


class Dashboard(BaseModel):
    name: Optional[str] = Field(default=None, max_length=60)
    banner: Optional[bool] = None
    tz: Optional[str] = Field(default=None, max_length=64)


class AutoCycleIn(BaseModel):
    enabled: bool
    time: str = Field(default="06:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    idle_min: int = Field(default=30, ge=1, le=1440)
    tz: Optional[str] = Field(default=None, max_length=64)    # the browser's zone, used if none is set yet


class GoveeKey(BaseModel):
    key: str = Field(min_length=8, max_length=200)


class PlugPick(BaseModel):
    device: Optional[str] = None


class Power(BaseModel):
    on: bool
    confirm: Optional[str] = Field(default=None, max_length=100)


class Baseline(BaseModel):
    hours: Optional[float] = Field(default=None, ge=0, le=1_000_000)
    jobs: Optional[int] = Field(default=None, ge=0, le=10_000_000)


class LinkHistory(BaseModel):
    key: str


class TagPrinters(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    printers: list[str] = Field(default_factory=list, max_length=500)


class Retag(BaseModel):
    old: str = Field(min_length=1)
    new: Optional[str] = None           # missing/blank = delete the tag everywhere


class Order(BaseModel):
    ids: list[str]


class CloudLogin(BaseModel):
    email: str
    password: str
    region: str = "global"


class CloudCode(BaseModel):
    code: str


class ImportPick(BaseModel):
    serial: str
    host: Optional[str] = None


class ImportBambu(BaseModel):
    printers: list[ImportPick]
    location: Optional[str] = Field(default=None, max_length=40)


def create_app(db_path: Optional[Path] = None, start_drivers: bool = True,
               ssdp_ports: Optional[tuple[int, ...]] = None) -> FastAPI:
    state: dict[str, Any] = {"pending_login": None}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store = PrinterStore(db_path or config.DB_PATH)
        mgr = Manager(store, BambuSSDP(ssdp_ports) if ssdp_ports is not None else None)
        app.state.mgr = mgr
        await mgr.start(drivers=start_drivers)
        try:
            yield
        finally:
            await mgr.stop()
            store.close()

    app = FastAPI(title="PF PrintFloor", lifespan=lifespan)

    def mgr(request: Request) -> Manager:
        return request.app.state.mgr

    # ------------------------------------------------------------------ page
    # The page asks for app.css / app.js with ?v=<content hash>: after an update the browser fetches the new files
    # instead of using cached old ones (2026-10-30: new toggle showed up but the old CSS ignored it).
    import hashlib
    _ver = hashlib.sha1(b"".join((STATIC / f).read_bytes() for f in ("app.css", "app.js"))).hexdigest()[:10]
    _page = (STATIC / "index.html").read_text().replace('href="/static/app.css"', f'href="/static/app.css?v={_ver}"') \
        .replace('src="/static/app.js"', f'src="/static/app.js?v={_ver}"')

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def index():
        return HTMLResponse(_page, headers={"Cache-Control": "no-cache"})

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    # ------------------------------------------------------------------ data
    @app.get("/api/catalog")
    async def get_catalog():
        return catalog()

    @app.get("/api/printers")
    async def list_printers(request: Request):
        return mgr(request).printers()

    @app.post("/api/printers", status_code=201)
    async def add_printer(body: AddPrinter, request: Request):
        try:
            cls = get_driver(body.driver)
        except KeyError:
            raise HTTPException(400, f"unknown printer model {body.driver}")
        if not getattr(cls, "available", True):
            raise HTTPException(400, f"{cls.maker} {cls.model_name} support is not built yet")
        missing = [f.label for f in cls.fields if f.required and f.key not in ("host",)
                   and not (body.secrets.get(f.key) if f.kind == "secret" else
                            (body.serial if f.key == "serial" else body.settings.get(f.key)))]
        if missing:
            raise HTTPException(400, "Fill in: " + ", ".join(missing))
        try:
            cfg = await mgr(request).add(body.name, body.driver, body.host.strip(),
                                         (body.serial or "").strip() or None, body.settings, body.secrets,
                                         body.location)
        except DuplicatePrinter as e:
            raise HTTPException(409, str(e))
        return mgr(request).printer_view(cfg)

    @app.patch("/api/printers/{pid}")
    async def edit_printer(pid: str, body: EditPrinter, request: Request):
        try:
            url = body.camera_url
            if url and not url.strip().lower().startswith(("http://", "https://")):
                raise HTTPException(400, "camera URL must start with http:// or https:// (MJPEG stream or JPEG snapshot)")
            cfg = await mgr(request).update(pid, body.name, body.host, body.serial, body.secrets, body.tags,
                                            camera_url=url, camera_kind=body.camera_kind, location=body.location,
                                            connection=body.connection, follow_name=body.follow_name)
        except KeyError:
            raise HTTPException(404, "printer not found")
        except ValueError as e:
            raise HTTPException(400, str(e))
        return mgr(request).printer_view(cfg)

    # ------------------------------------------------------------------ job history + usage
    def _cfg(request: Request, pid: str):
        cfg = mgr(request).store.get(pid)
        if not cfg:
            raise HTTPException(404, "printer not found")
        return cfg

    # ------------------------------------------------------------------ Govee plugs
    def _govee_view(request: Request) -> dict:
        m = mgr(request)
        v = m.plugs.account_view()
        taken = set(m.plugs.assigned().keys())
        used = {p["device"] for p in m.plugs.assigned().values()}
        v["suggestions"] = suggest_plugs([(c.id, c.name) for c in m.store.list() if c.id not in taken],
                                         m.plugs.plug_list, used)
        return v

    @app.get("/api/govee")
    async def govee_get(request: Request):
        return _govee_view(request)

    @app.put("/api/govee")
    async def govee_link(body: GoveeKey, request: Request):
        try:
            await mgr(request).plugs.link(body.key)
        except GoveeAuthError:
            raise HTTPException(400, "Govee rejected that API key. Check it and paste it again.")
        except GoveeError as e:
            raise HTTPException(502, str(e))
        mgr(request).broadcast_printers()
        return _govee_view(request)

    @app.delete("/api/govee", status_code=204)
    async def govee_unlink(request: Request):
        mgr(request).plugs.unlink()
        mgr(request).broadcast_printers()

    @app.put("/api/printers/{pid}/plug")
    async def assign_plug(pid: str, body: PlugPick, request: Request):
        m = mgr(request)
        cfg = _cfg(request, pid)
        settings = dict(cfg.settings)
        if body.device:
            plug = next((p for p in m.plugs.plug_list if p["device"] == body.device), None)
            if not plug:
                raise HTTPException(400, "that plug isn't on the Govee account")
            other = next((c for c in m.store.list() if c.id != pid and (c.settings.get("plug") or {}).get("device") == body.device), None)
            if other:
                raise HTTPException(409, f"that plug is already set for {other.name}")
            settings["plug"] = dict(plug)
        else:
            settings.pop("plug", None)
        new = m.store.update(pid, settings=settings)
        if pid in m.drivers:
            m.drivers[pid].config = new
        m.plugs._wake.set()                      # read its state now
        m.broadcast_printers()
        return m.printer_view(new)

    @app.get("/api/printers/{pid}/power")
    async def power_info(pid: str, request: Request):
        m = mgr(request)
        cfg = _cfg(request, pid)
        dev = (cfg.settings.get("plug") or {}).get("device")
        if dev:
            await m.plugs.refresh_one(dev)
        return {"power": m.plugs.view(pid, cfg.settings.get("plug"), m.status.get(pid)), "log": m.plugs.log_rows(pid)}

    @app.post("/api/printers/{pid}/power")
    async def set_power(pid: str, body: Power, request: Request):
        m = mgr(request)
        cfg = _cfg(request, pid)
        try:
            was_off = (m.plugs.states.get((cfg.settings.get("plug") or {}).get("device"), {}) or {}).get("on") is False
            r = await m.plugs.set_power(cfg, m.status.get(pid), body.on, body.confirm)
        except GoveeError as e:
            raise HTTPException(502, str(e))
        if body.on and r.get("ok") and was_off:
            m.autocycle.manual_cycle(pid)              # turned back on from the dashboard = a power cycle
        m.broadcast_printers()
        return r

    @app.put("/api/printers/{pid}/autocycle")
    async def put_autocycle(pid: str, body: AutoCycleIn, request: Request):
        try:
            return mgr(request).set_autocycle(pid, body.enabled, body.time, body.idle_min, body.tz)
        except KeyError:
            raise HTTPException(404, "printer not found")
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.post("/api/printers/{pid}/autocycle/{action}", status_code=204)
    async def autocycle_action(pid: str, action: str, request: Request):
        m = mgr(request)
        cfg = _cfg(request, pid)
        if action == "pause":
            m.autocycle.pause(cfg)
        elif action == "resume":
            m.autocycle.resume(pid)
        elif action == "restart":
            m.autocycle.restart_countdown(pid)
        else:
            raise HTTPException(404, "unknown action")
        m.autocycle._views.pop(pid, None)
        m.broadcast_printers()
        return Response(status_code=204)

    # ------------------------------------------------------------------ cameras
    NO_STORE = {"Cache-Control": "no-store"}

    def _feed(request: Request, pid: str):
        return mgr(request).cameras.feed(_cfg(request, pid))

    @app.get("/api/printers/{pid}/camera")
    async def camera_info(pid: str, request: Request):
        f = _feed(request, pid)
        return {"running": f.running, "label": f.label, "error": f.error, "viewers": f.viewers, "seq": f.seq,
                "player_url": f.player_url}

    @app.get("/api/printers/{pid}/camera.jpg")
    async def camera_snapshot(pid: str, request: Request, after: int = 0, wait: float = 1.5):
        """Newest frame (camera wall). after=<seq>: wait up to `wait` s for a newer one, else 204."""
        f = _feed(request, pid)
        try:
            f.lease()
        except CameraUnavailable as e:
            raise HTTPException(404, str(e))
        got = await f.next_frame(after, min(max(wait, 0.0), 5.0) if after and f.seq else FRAME_TIMEOUT_S)
        hdr = {**NO_STORE, "X-Frame-Seq": str(f.seq), "X-Camera-Source": f.label}
        if got is None:
            if f.seq and after:
                return Response(status_code=204, headers=hdr)
            raise HTTPException(503, f.error or "camera did not send a picture yet")
        return Response(got[1], media_type="image/jpeg", headers={**hdr, "X-Frame-Seq": str(got[0])})

    @app.get("/api/printers/{pid}/camera.mjpg")
    async def camera_stream(pid: str, request: Request):
        """Live MJPEG for one viewer (popup). The upstream is shared and closes after the last viewer."""
        f = _feed(request, pid)
        try:
            f.add_viewer()
        except CameraUnavailable as e:
            raise HTTPException(404, str(e))

        async def gen():
            seq = 0
            try:
                while True:
                    got = await f.next_frame(seq, 15)
                    if await request.is_disconnected():
                        break
                    if got is None:
                        continue
                    seq, img = got
                    yield (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " + str(len(img)).encode()
                           + b"\r\n\r\n" + img + b"\r\n")
            finally:
                f.remove_viewer()
        return StreamingResponse(gen(), media_type="multipart/x-mixed-replace; boundary=frame", headers=NO_STORE)

    def _named(request: Request, rows: list[dict]) -> list[dict]:
        """Job rows show each printer's current name (history follows the printer, not the name it had then)."""
        now = {usage_key(c): c.name for c in mgr(request).store.list()}
        for r in rows:
            r["printer_name"] = now.get(r["usage_key"]) or r["printer_name"]
        return rows

    def _usage_view(request: Request, cfg) -> dict:
        h = mgr(request).history
        u = h.usage(usage_key(cfg))
        u["recent"] = _named(request, h.jobs([u["key"]], limit=5))
        live = {usage_key(c) for c in mgr(request).store.list()}
        u["linkable"] = h.orphan_keys(live)
        return u

    @app.get("/api/printers/{pid}/usage")
    async def printer_usage(pid: str, request: Request):
        return _usage_view(request, _cfg(request, pid))

    @app.put("/api/printers/{pid}/usage")
    async def set_usage(pid: str, body: Baseline, request: Request):
        cfg = _cfg(request, pid)
        mgr(request).history.set_baseline(usage_key(cfg), None if body.hours is None else body.hours * 3600, body.jobs)
        return _usage_view(request, cfg)

    @app.post("/api/printers/{pid}/usage/accept-reported")
    async def accept_reported(pid: str, request: Request):
        cfg = _cfg(request, pid)
        try:
            mgr(request).history.accept_reported(usage_key(cfg))
        except ValueError as e:
            raise HTTPException(400, str(e))
        return _usage_view(request, cfg)

    @app.post("/api/printers/{pid}/usage/link")
    async def link_history(pid: str, body: LinkHistory, request: Request):
        cfg = _cfg(request, pid)
        live = {usage_key(c) for c in mgr(request).store.list()}
        if body.key in live:
            raise HTTPException(400, "that history belongs to a printer on the dashboard")
        mgr(request).history.rekey(body.key, usage_key(cfg))
        return _usage_view(request, cfg)

    @app.get("/api/history")
    async def history(request: Request, printer_id: Optional[str] = None, result: Optional[str] = None,
                      limit: int = 50, before: Optional[int] = None):
        keys = None
        if printer_id:
            keys = [usage_key(_cfg(request, printer_id))]
        return _named(request, mgr(request).history.jobs(keys, result, min(max(limit, 1), 500), before))

    @app.get("/api/history/summary")
    async def history_summary(request: Request):
        h = mgr(request).history
        rows = []
        for cfg in mgr(request).store.list():
            u = h.usage(usage_key(cfg))
            rows.append({"printer_id": cfg.id, "name": cfg.name, **{k: u[k] for k in (
                "total_seconds", "total_jobs", "tracked_seconds", "tracked_jobs", "finished", "failed",
                "success_rate", "baseline_source")}, "audit_flagged": bool(u["audit"] and u["audit"]["flagged"])})
        fin, fail = sum(r["finished"] for r in rows), sum(r["failed"] for r in rows)
        return {"printers": rows, "fleet": {"total_seconds": sum(r["total_seconds"] for r in rows),
                                            "total_jobs": sum(r["total_jobs"] for r in rows),
                                            "finished": fin, "failed": fail,
                                            "success_rate": round(100 * fin / (fin + fail)) if fin + fail else None}}

    @app.get("/api/history.csv")
    async def history_csv(request: Request):
        import csv
        import io
        rows = _named(request, mgr(request).history.jobs(None, None, 100000, None))
        buf = io.StringIO()
        wr = csv.writer(buf)
        wr.writerow(["printer", "job", "file", "started_utc", "ended_utc", "result", "print_hours", "filament", "source"])
        for r in reversed(rows):
            wr.writerow([r["printer_name"], r["job"], r["file_name"], r["started_at"], r["ended_at"] or "",
                         r["result"], round(r["print_seconds"] / 3600, 2), "; ".join(r["filament"]), r["source"]])
        return Response(buf.getvalue(), media_type="text/csv",
                        headers={"Content-Disposition": "attachment; filename=print-history.csv"})

    @app.get("/api/tags")
    async def list_tags(request: Request):
        return mgr(request).tags()

    @app.put("/api/tags")
    async def set_tag(body: TagPrinters, request: Request):
        """Create a tag (it's kept even with no printers) and set exactly which printers have it."""
        try:
            return mgr(request).set_tag_printers(body.name, body.printers)
        except ValueError as e:
            raise HTTPException(400, str(e))
        except KeyError:
            raise HTTPException(404, "printer not found")

    @app.post("/api/tags/retag")
    async def retag(body: Retag, request: Request):
        new = " ".join((body.new or "").split()) or None
        return {"changed": mgr(request).retag(body.old, new)}

    @app.delete("/api/printers/{pid}", status_code=204)
    async def remove_printer(pid: str, request: Request):
        if not await mgr(request).remove(pid):
            raise HTTPException(404, "printer not found")
        return Response(status_code=204)

    @app.put("/api/printers/order", status_code=204)
    async def reorder(body: Order, request: Request):
        try:
            mgr(request).reorder(body.ids)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return Response(status_code=204)

    @app.get("/api/printers/{pid}/thumbnail")
    async def thumbnail(pid: str):
        path = config.THUMB_DIR / f"{pid}.png"
        if not path.is_file():
            raise HTTPException(404, "no thumbnail")
        head = path.open("rb").read(12)
        media = "image/jpeg" if head[:3] == b"\xff\xd8\xff" else \
            "image/webp" if head[:4] == b"RIFF" and head[8:12] == b"WEBP" else "image/png"
        return FileResponse(path, media_type=media, headers={"Cache-Control": "max-age=86400"})

    # ------------------------------------------------------------------ live updates
    @app.get("/api/layout")
    async def get_layout(request: Request):
        return mgr(request).layout()

    @app.put("/api/locations")
    async def put_locations(body: Locations, request: Request):
        try:
            return mgr(request).set_locations([i.model_dump() for i in body.items], body.default)
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.put("/api/dashboard")
    async def put_dashboard(body: Dashboard, request: Request):
        try:
            return mgr(request).set_dashboard(body.name, body.banner, body.tz)
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.get("/api/events")
    async def events(request: Request):
        m = mgr(request)
        q = m.subscribe()

        async def stream():
            try:
                yield _sse({"type": "printers", "printers": m.printers(), "layout": m.layout()})
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        ev = await asyncio.wait_for(q.get(), timeout=KEEPALIVE_S)
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
                        continue
                    yield _sse(ev)
            finally:
                m.unsubscribe(q)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # ------------------------------------------------------------------ Bambu account (optional)
    @app.get("/api/bambu-account")
    async def account_status(request: Request):
        acct = bambu_cloud.get_account()
        pending = state["pending_login"]
        return {"linked": bool(acct), "email": acct.email if acct else None,
                "state": acct.state if acct else None,
                "pending_step": pending["step"] if pending else None}

    @app.post("/api/bambu-account/login")
    async def account_login(body: CloudLogin, request: Request):
        cloud = bambu_cloud.BambuCloud(region=body.region)
        email = body.email.strip()
        try:
            await asyncio.to_thread(cloud.login, email, body.password)
        except bambu_cloud.NeedsEmailCode:
            state["pending_login"] = {"step": "email_code", "cloud": cloud, "email": email}
            return {"step": "email_code"}
        except bambu_cloud.NeedsTfa as e:
            state["pending_login"] = {"step": "tfa", "cloud": cloud, "email": email, "tfa_key": e.tfa_key}
            return {"step": "tfa"}
        except bambu_cloud.CloudError as e:
            raise HTTPException(400, str(e))
        return _finish_login(request, cloud, email)

    @app.post("/api/bambu-account/code")
    async def account_code(body: CloudCode, request: Request):
        p = state["pending_login"]
        if not p:
            raise HTTPException(400, "Start again: enter your email and password first")
        try:
            if p["step"] == "tfa":
                await asyncio.to_thread(p["cloud"].login_tfa, p["tfa_key"], body.code)
            else:
                await asyncio.to_thread(p["cloud"].login_with_code, p["email"], body.code)
        except bambu_cloud.CloudError as e:
            raise HTTPException(400, f"Code not accepted: {e}")
        return _finish_login(request, p["cloud"], p["email"])

    async def _account_devices() -> list[dict]:
        acct = bambu_cloud.get_account()
        if not acct or acct.state != "linked":
            raise HTTPException(409, "Sign in to your Bambu account first")
        try:
            return await asyncio.to_thread(acct.cloud.devices)
        except bambu_cloud.TokenExpired as e:
            acct._rejected(e)
            raise HTTPException(409, "Bambu stopped accepting the saved sign-in. Sign in again")
        except bambu_cloud.CloudError as e:
            raise HTTPException(502, f"Could not reach Bambu: {e}")

    @app.get("/api/bambu-account/printers")
    async def account_printers(request: Request):
        return mgr(request).account_printers(await _account_devices())

    @app.post("/api/bambu-account/printers")
    async def account_import(body: ImportBambu, request: Request):
        devices = await _account_devices()
        return await mgr(request).import_bambu(devices, [p.model_dump() for p in body.printers],
                                                 body.location)

    # Network search for U1 / Klipper printers: started by the user, polled for progress.
    @app.post("/api/discover/network", status_code=202)
    async def discover_start(request: Request):
        old = state.get("scan")
        if old and not old.done:
            old.cancel.set()
        job = klipper_scan.ScanJob()
        state["scan"] = job
        asyncio.get_running_loop().run_in_executor(None, job.run)
        return {"started": True}

    @app.get("/api/discover/network")
    async def discover_status(request: Request):
        job = state.get("scan")
        if not job:
            return {"done": True, "checked": 0, "total": 0, "found": [], "started": False}
        snap = job.snapshot()
        snap["found"] = mgr(request).mark_network(snap["found"])
        snap["started"] = True
        return snap

    @app.delete("/api/discover/network", status_code=204)
    async def discover_stop():
        job = state.get("scan")
        if job:
            job.cancel.set()
        return Response(status_code=204)

    @app.delete("/api/bambu-account", status_code=204)
    async def account_unlink(request: Request):
        state["pending_login"] = None
        mgr(request).store.delete_app_setting("bambu_cloud")
        mgr(request).set_bambu_account(None)
        return Response(status_code=204)

    def _finish_login(request: Request, cloud: bambu_cloud.BambuCloud, email: str) -> dict:
        state["pending_login"] = None
        m = mgr(request)
        bambu_cloud.save_link(m.store, email, cloud)
        m.set_bambu_account(bambu_cloud.account_from_store(m.store))
        return {"step": "done", "email": email}

    return app


def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj, separators=(',', ':'))}\n\n"
