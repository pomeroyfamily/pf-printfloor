import asyncio
import json
from pathlib import Path

import pytest

from app import config
from app.drivers import get_driver
from app.drivers.klipper import parse
from app.models import ConnState, JobState, PrinterStatus
from app.store import PrinterConfig
from tests.fakes.moonraker import FakeMoonraker
from tests.test_bambu_session import png

FX = json.loads((Path(__file__).parent / "fixtures/klipper/u1_fw200_printing.json").read_text())
THUMB300 = ".thumbs/LTR ~ LEG THIGH RIGHT_PETG_1d6h32m-300x300.png"


@pytest.fixture(autouse=True)
def thumb_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "THUMB_DIR", tmp_path / "thumbs")


def u1_status(fx=FX, **kw) -> PrinterStatus:
    return parse.to_status("u", fx["status"], objects=fx["objects"], sensors=fx["available_sensors"],
                           meta=fx["file_metadata"], chamber_sensor="temperature_sensor cavity", u1=True,
                           firmware="2.0.0", **kw)


def test_u1_job_temps_fans():
    st = u1_status()
    assert st.state == JobState.PRINTING
    j = st.job
    assert j.name == "LTR ~ LEG THIGH RIGHT_PETG_1d6h32m" and j.progress_pct == 82.0
    assert (j.layer, j.total_layers, j.elapsed_s) == (585, 911, 88980)
    # probe said ~5h25m by elapsed/progress; slicer says 30h31m - 24h43m = 5h48m; blend sits between
    assert 5.4 * 3600 < j.remaining_s < 5.8 * 3600 and j.eta
    t = {x.key: x for x in st.temps}
    assert [x.label for x in st.temps[:3]] == ["Nozzle", "Bed", "Chamber"]
    assert (t["nozzle"].current, t["nozzle"].target, t["chamber"].current) == (230, 230, 44)
    assert [x.label for x in st.temps[3:]] == ["T1 nozzle", "T2 nozzle", "T3 nozzle", "T4 nozzle"]
    assert t["tool1"].current == 191 and t["tool3"].target is None
    f = {x.label: x.percent for x in st.fans}
    assert f["Part cooling"] == 90 and f["Power supply"] == 60 and f["T1 hotend"] == 100 and f["Chamber"] == 0


def test_u1_filament_slots():
    (unit,) = u1_status().filament
    assert unit.kind == "Toolheads" and len(unit.slots) == 4
    s = unit.slots
    assert [x.material for x in s] == ["PETG", "PETG", "PETG", "PLA"] and s[1].sub_type == "HF"
    assert [x.color_hex for x in s] == ["#1E88E5", "#E72F1D", "#000000", "#FFFFFF"]
    assert s[0].active and not any(x.active for x in s[1:]) and s[0].brand == "Generic" and s[0].rfid is False


def test_active_tool_follows_toolhead():
    fx = json.loads(json.dumps(FX))
    fx["status"]["toolhead"]["extruder"] = "extruder2"
    st = u1_status(fx)
    assert st.temps[0].current == 80 and st.filament[0].slots[2].active


def test_idle_and_klippy_error():
    fx = json.loads(json.dumps(FX))
    fx["status"]["print_stats"]["state"] = "standby"
    st = u1_status(fx)
    assert st.state == JobState.IDLE and st.job.name is None and not any(s.active for s in st.filament[0].slots)
    fx["status"]["webhooks"] = {"state": "shutdown", "state_message": "MCU 'mcu' shutdown: Timer too close"}
    st = u1_status(fx)
    assert st.state == JobState.ERROR and st.errors[0].code == "klippy_shutdown" and "Timer" in st.errors[0].message


def test_generic_klipper_uses_slicer_filament():
    st = parse.to_status("g", FX["status"], objects=FX["objects"], sensors=FX["available_sensors"],
                         meta={**FX["file_metadata"], "filament_colors": "#FF0000;#00FF00"}, chamber_sensor=None,
                         u1=False, firmware=None)
    (unit,) = st.filament
    assert unit.kind == "From slicer" and unit.slots[0].source == "slicer" and unit.slots[1].color_hex == "#00FF00"
    assert st.temps[2].label == "Chamber" and st.temps[2].current == 44      # found by name ("cavity")


# ---------------------------------------------------------------------- driver against fake Moonraker
async def run(driver, port, seconds=1.5, secrets=None, serial=None):
    seen: list[PrinterStatus] = []
    cfg = PrinterConfig(id="k1", name="k", driver=driver, host="127.0.0.1", serial=serial,
                        settings={"port": port, "poll_interval": 0.3}, secrets=secrets or {})
    d = get_driver(driver)(cfg, lambda s: seen.append(s.model_copy(deep=True)))
    d.start()
    await asyncio.sleep(seconds)
    await d.stop()
    return d, seen


def test_u1_driver_end_to_end():
    img = png(300, 300, (30, 136, 229))
    fm = FakeMoonraker(FX, thumbnails={THUMB300: img})
    try:
        d, seen = asyncio.run(run("snapmaker.u1", fm.port))
    finally:
        fm.close()
    last = [s for s in seen if s.connection == ConnState.ONLINE][-1]
    assert last.device_name == "U1-A" and last.firmware == "2.0.0" and last.job.progress_pct == 82.0
    assert d.learned_serial == "8110000000000000TEST"
    assert last.job.thumbnail_url and last.job.thumbnail_source == "printer"
    assert (config.THUMB_DIR / "k1.png").read_bytes() == img          # picked the 300x300 one
    assert all(p.startswith(("/server", "/printer", "/machine")) for p in fm.paths)   # read-only GETs
    assert sum("/server/files/metadata" in p for p in fm.paths) == 1   # metadata fetched once per file
    from datetime import datetime, timedelta, timezone
    up = datetime.now(timezone.utc) - last.booted_at                 # fake reports 5 h 0 min 7 s of uptime
    assert timedelta(hours=5) <= up <= timedelta(hours=5, minutes=2)
    assert sum("/machine/proc_stats" in p for p in fm.paths) == 1     # every 5 min, not every poll


def test_auth_error_explains_fix():
    fm = FakeMoonraker(FX, require_key="secret")
    try:
        _, seen = asyncio.run(run("klipper.generic", fm.port, 1.0))
        assert seen[-1].connection == ConnState.AUTH_ERROR and "trusted_clients" in seen[-1].connection_detail
        _, seen = asyncio.run(run("klipper.generic", fm.port, 1.0, secrets={"api_key": "secret"}))
        assert seen[-1].connection == ConnState.ONLINE
    finally:
        fm.close()


def test_generic_name_is_hostname_and_unreachable_is_offline():
    fm = FakeMoonraker(FX, u1=False)
    try:
        _, seen = asyncio.run(run("klipper.generic", fm.port, 1.0))
        assert seen[-1].device_name == "lava"
    finally:
        fm.close()
    _, seen = asyncio.run(run("klipper.generic", fm.port, 1.0))           # server gone
    assert seen[-1].connection == ConnState.OFFLINE


# ---------------------------------------------------------------------- discovery
import struct  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from app.discovery import klipper_scan  # noqa: E402
from app.manager import Manager  # noqa: E402
from app.web import create_app  # noqa: E402


def mdns_answer(name="U1-B", sn="8110000000000001TEST", ip="10.0.0.154") -> bytes:
    """mDNS response shaped like the U1's (TXT keys seen by u1_discover.py: device_name, sn, version, ip)."""
    def qn(s):
        return b"".join(bytes([len(p)]) + p.encode() for p in s.rstrip(".").split(".")) + b"\x00"
    inst = f"{name}._snapmaker._tcp.local."
    txt = b"".join(bytes([len(x)]) + x for x in
                   [f"device_name={name}".encode(), f"sn={sn}".encode(), b"version=2.0.0", f"ip={ip}".encode(),
                    b"link_mode=wan"])
    rr = qn(inst) + struct.pack("!HHIH", 16, 0x8001, 120, len(txt)) + txt
    return struct.pack("!6H", 0, 0x8400, 0, 1, 0, 0) + rr


def test_parse_mdns_txt():
    (r,) = klipper_scan.parse_mdns(mdns_answer(), "10.0.0.154")
    assert (r["name"], r["host"], r["serial"], r["firmware"]) == ("U1-B", "10.0.0.154", "8110000000000001TEST", "2.0.0")


def test_probe_host_u1_and_generic():
    fm = FakeMoonraker(FX)
    gm = FakeMoonraker(FX, u1=False)
    try:
        u = klipper_scan.probe_host("127.0.0.1", fm.port)
        g = klipper_scan.probe_host("127.0.0.1", gm.port)
    finally:
        fm.close()
        gm.close()
    assert (u["name"], u["serial"], u["model"], u["port"]) == ("U1-A", "8110000000000000TEST", "Snapmaker U1", fm.port)
    assert g["model"] == "Klipper / Moonraker" and g["name"] == "lava"
    assert klipper_scan.probe_host("127.0.0.1", 9, connect_timeout=0.2) is None      # nothing listening


FOUND = [
    {"name": "U1-A", "host": "10.0.0.107", "serial": "8110000000000000TEST", "model": "Snapmaker U1",
     "firmware": "2.0.0", "how": "http", "port": 80},
    {"name": "U1-A", "host": "10.0.0.107", "serial": "8110000000000000TEST", "firmware": "2.0.0",
     "model": "Snapmaker U1", "how": "mdns"},
    {"name": "U1-B", "host": "10.0.0.154", "serial": "8110000000000001TEST", "model": "Snapmaker U1", "how": "mdns"},
    {"name": "voron", "host": "10.0.0.30", "serial": None, "model": "Klipper / Moonraker", "how": "http"},
]


def fake_methods(monkeypatch, delay=0.0):
    import time as _t

    def sweep(subnet=None, hosts=None, on_found=None, on_progress=None, cancel=None):
        for i, r in enumerate([FOUND[0], None, FOUND[3], None]):
            _t.sleep(delay)
            if cancel and cancel.is_set():
                return []
            if r and on_found:
                on_found(dict(r))
            if on_progress:
                on_progress(i + 1, 4)
        return []

    def mdns(seconds=3.0, on_found=None):
        for r in FOUND[1:3]:
            on_found(dict(r))
        return []
    def elegoo(timeout=2.5, on_found=None, subnet=None, port=3000):
        on_found({"name": "Centauri Carbon", "host": "10.0.0.202", "serial": "0123456789abcdef0000000000000000",
                  "model": "Elegoo Centauri Carbon", "firmware": "V1.4.49", "driver": "elegoo.cc1", "how": "udp"})
        return []
    from app.discovery import sdcp_scan
    monkeypatch.setattr(klipper_scan, "http_sweep", sweep)
    monkeypatch.setattr(klipper_scan, "mdns_scan", mdns)
    monkeypatch.setattr(sdcp_scan, "broadcast_scan", elegoo)


def test_scan_merges_mdns_and_sweep(monkeypatch):
    fake_methods(monkeypatch)
    res = klipper_scan.scan()
    assert [(r["name"], r["driver"]) for r in res] == [("voron", "klipper.generic"), ("U1-A", "snapmaker.u1"),
                                                       ("U1-B", "snapmaker.u1"), ("Centauri Carbon", "elegoo.cc1")]
    assert res[1]["port"] == 80 and res[1]["firmware"] == "2.0.0"


def test_http_sweep_reports_progress_and_finds_printers():
    fm = FakeMoonraker(FX)
    seen, progress = [], []
    try:
        # probe_host uses port 80; point it at the fake for 127.0.0.1 only
        orig = klipper_scan.probe_host
        klipper_scan.probe_host = lambda h, port=80, connect_timeout=0.6: orig(h, fm.port if h == "127.0.0.1" else 9, 0.2)
        try:
            klipper_scan.http_sweep(hosts=["127.0.0.1", "127.0.0.2", "127.0.0.3"], on_found=seen.append,
                                    on_progress=lambda d, t: progress.append((d, t)))
        finally:
            klipper_scan.probe_host = orig
    finally:
        fm.close()
    assert [r["name"] for r in seen] == ["U1-A"] and progress[-1] == (3, 3)


def test_api_search_is_started_by_user_shows_progress_and_marks_added(tmp_path, monkeypatch):
    import time as _t
    fake_methods(monkeypatch, delay=0.15)
    monkeypatch.setattr(Manager, "_start", lambda self, cfg: None)
    with TestClient(create_app(tmp_path / "db.sqlite", start_drivers=False, ssdp_ports=())) as c:
        assert c.get("/api/discover/network").json()["started"] is False        # nothing runs on its own
        c.post("/api/printers", json={"driver": "snapmaker.u1", "name": None, "host": "10.0.0.107",
                                       "serial": "8110000000000000TEST"})
        assert c.post("/api/discover/network").status_code == 202
        snaps = []
        for _ in range(60):
            snaps.append(c.get("/api/discover/network").json())
            if snaps[-1]["done"]:
                break
            _t.sleep(0.05)
        assert any(not s["done"] and s["found"] for s in snaps)                  # results before the end
        assert any(0 < s["checked"] < s["total"] for s in snaps)                 # real progress
        rows = {r["name"]: r for r in snaps[-1]["found"]}
        assert rows["U1-A"]["added_id"] and not rows["U1-B"]["added_id"] and rows["voron"]["driver"] == "klipper.generic"
        assert rows["Centauri Carbon"]["driver"] == "elegoo.cc1" and not rows["Centauri Carbon"]["added_id"]


def test_api_search_can_be_stopped(tmp_path, monkeypatch):
    import time as _t
    fake_methods(monkeypatch, delay=0.3)
    monkeypatch.setattr(Manager, "_start", lambda self, cfg: None)
    with TestClient(create_app(tmp_path / "db.sqlite", start_drivers=False, ssdp_ports=())) as c:
        c.post("/api/discover/network")
        _t.sleep(0.4)
        assert c.delete("/api/discover/network").status_code == 204
        for _ in range(40):
            s = c.get("/api/discover/network").json()
            if s["done"]:
                break
            _t.sleep(0.05)
        assert s["done"] and s["cancelled"] and s["checked"] < 4


def test_scan_never_merges_different_serials_on_one_ip():
    job = klipper_scan.ScanJob()
    job._add({"name": "A", "host": "10.0.0.5", "serial": "S1", "model": "Snapmaker U1"})
    job._add({"name": "B", "host": "10.0.0.5", "serial": "S2", "driver": "elegoo.cc1"})
    job._add({"name": None, "host": "10.0.0.5", "serial": None, "model": "Snapmaker U1"})   # no serial: joins one
    assert sorted(r["name"] for r in job.snapshot()["found"]) == ["A", "B"]


def test_u1_pause_reason_from_exception():
    """U1-C dump 2026-11-02: paused, print_stats.exception carries the reason; message is empty."""
    fx = json.loads(json.dumps(FX))
    fx["status"]["print_stats"].update(state="paused", message="", exception={
        "id": 523, "index": 1, "code": 38, "message": "detect filament tangled!", "level": 2})
    st = u1_status(fx)
    assert st.state == JobState.PAUSED and st.reason == "Paused: filament tangled (Toolhead 2)"
    assert any(e.code == "u1_38" and e.message == "filament tangled (Toolhead 2)" for e in st.errors)
    fx["status"]["print_stats"]["state"] = "printing"           # resumed, exception not cleared: no label
    st = u1_status(fx)
    assert st.reason is None and not st.errors
    assert parse.exception_reason({"message": "", "code": 1}) is None
    assert parse.exception_reason({"message": "Nozzle clog", "index": None}) == "Nozzle clog"
