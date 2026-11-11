"""Bambu connection: local, cloud, and auto (LAN first, cloud when away, back to LAN). 2026-11-03."""
from __future__ import annotations

import asyncio

import pytest

from app import config
from app.cloud import bambu as bambu_cloud
from app.drivers import get_driver
from app.drivers.bambu import BambuPrinter
from app.models import ConnState
from app.store import PrinterConfig
from tests.fakes.bambu_cloud import FakeCloud
from tests.fakes.bambu_server import FakeBambu, free_port
from tests.test_bambu_session import fixture

SERIAL = "01S099000000014"
FX = "p1p_fw011000_running_cloud.json"


@pytest.fixture(autouse=True)
def thumb_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "THUMB_DIR", tmp_path / "thumbs")


@pytest.fixture
def cloud():
    fc = FakeCloud([], {}, mode="direct")
    fc.devices = [{"dev_id": SERIAL, "name": "Remote Mini", "online": True}]
    yield fc
    fc.close()
    bambu_cloud.set_account(None)


def link(fc, broker):
    acct = bambu_cloud.Account(bambu_cloud.BambuCloud(fc.token, api_base=fc.base, web_base=fc.base), "d@x")
    acct.mqtt_host_override = ("127.0.0.1", broker.mqtt_port)
    bambu_cloud.set_account(acct)
    return acct


async def drive(seconds, port, **settings):
    seen = []
    cfg = PrinterConfig(id="t1", name="t", driver="bambu.p1p", host="127.0.0.1", serial=SERIAL,
                        settings={"mqtt_port": port, "thumbnails": False, **settings}, secrets={"access_code": "12345678"})
    d = get_driver("bambu.p1p")(cfg, lambda s: seen.append(s.model_copy(deep=True)))
    return d, seen


def test_cloud_mode_reads_through_the_account(tmp_path, cloud):
    async def go():
        async with FakeBambu(tmp_path, fixture(FX), user="u_42", code=cloud.token) as broker:
            link(cloud, broker)
            d, seen = await drive(0, free_port(), connection="cloud")
            d.start(); await asyncio.sleep(1.5); await d.stop()
            return broker, seen
    broker, seen = asyncio.run(go())
    online = [s for s in seen if s.connection == ConnState.ONLINE]
    assert online and online[-1].link == "cloud" and online[-1].job.progress_pct is not None
    cmds = {list(r["payload"].values())[0]["command"] for r in broker.requests}
    assert cmds == {"pushall", "get_version"}                       # still read-only
    assert "GET /v1/design-user-service/my/preference" in cloud.calls


def test_auto_falls_back_to_cloud_when_not_on_the_lan(tmp_path, cloud, monkeypatch):
    monkeypatch.setattr(BambuPrinter, "CLOUD_AFTER_S", 0.0)
    async def go():
        async with FakeBambu(tmp_path, fixture(FX), user="u_42", code=cloud.token) as broker:
            link(cloud, broker)
            d, seen = await drive(0, free_port())                      # LAN port: nothing listening
            d.start(); await asyncio.sleep(2.0); await d.stop()
            return seen
    seen = asyncio.run(go())
    assert [s.link for s in seen if s.connection == ConnState.ONLINE][-1] == "cloud"


def test_auto_stays_offline_when_not_on_the_account(tmp_path, cloud, monkeypatch):
    monkeypatch.setattr(BambuPrinter, "CLOUD_AFTER_S", 0.0)
    cloud.devices = [{"dev_id": "SOMEONE_ELSE", "online": True}]
    async def go():
        async with FakeBambu(tmp_path, fixture(FX), user="u_42", code=cloud.token) as broker:
            link(cloud, broker)
            d, seen = await drive(0, free_port())
            d.start(); await asyncio.sleep(1.0); await d.stop()
            return seen
    seen = asyncio.run(go())
    assert not any(s.connection == ConnState.ONLINE for s in seen) and seen[-1].connection == ConnState.OFFLINE


def test_auto_comes_home_to_the_lan(tmp_path, cloud, monkeypatch):
    monkeypatch.setattr(BambuPrinter, "LOCAL_RETRY_S", 0.2)
    async def go():
        (tmp_path / "c").mkdir(); (tmp_path / "l").mkdir()
        async with FakeBambu(tmp_path / "c", fixture(FX), user="u_42", code=cloud.token,
                             deltas=[{"mc_percent": 50 + i} for i in range(40)]) as broker, \
                FakeBambu(tmp_path / "l", fixture(FX)) as lan:
            link(cloud, broker)
            d, seen = await drive(0, lan.mqtt_port)
            d._via_cloud = True                                        # it was away; now it's back home
            d.start(); await asyncio.sleep(2.5); await d.stop()
            return seen
    links = [s.link for s in asyncio.run(go()) if s.connection == ConnState.ONLINE]
    assert links[0] == "cloud" and links[-1] == "local"


def test_cloud_mode_without_account_says_so(tmp_path):
    bambu_cloud.set_account(None)
    async def go():
        d, seen = await drive(0, free_port(), connection="cloud")
        d.start(); await asyncio.sleep(0.4); await d.stop()
        return seen
    st = asyncio.run(go())[-1]
    assert st.connection == ConnState.AUTH_ERROR and "Bambu account" in st.connection_detail


def test_connection_setting_api(tmp_path):
    from fastapi.testclient import TestClient
    from app.web import create_app
    with TestClient(create_app(db_path=tmp_path / "d.db", start_drivers=False, ssdp_ports=())) as c:
        b = c.post("/api/printers", json={"driver": "bambu.a1mini", "name": "Mini", "host": "10.0.0.9", "serial": SERIAL,
                                          "secrets": {"access_code": "12345678"}}).json()
        assert b["connection"] == "auto"                                  # default for every Bambu
        assert c.patch(f"/api/printers/{b['id']}", json={"connection": "cloud"}).json()["connection"] == "cloud"
        assert c.patch(f"/api/printers/{b['id']}", json={"connection": "sideways"}).status_code == 422
        u = c.post("/api/printers", json={"driver": "snapmaker.u1", "name": "U", "host": "10.0.0.8"}).json()
        assert "connection" not in u
        assert c.patch(f"/api/printers/{u['id']}", json={"connection": "cloud"}).status_code == 400


def test_bambu_reboot_detected_from_report_counter(tmp_path):
    """sequence_id starts over at boot (A1 Mini 2026-11-05: 27026 -> 28). Two low reports in a row = reboot."""
    from app.manager import Manager
    from app.models import PrinterStatus
    from app.store import PrinterStore
    store = PrinterStore(tmp_path / "d.db")
    pid = store.add("Mini-B", "bambu.a1mini", "10.0.0.119", SERIAL).id
    m = Manager(store)
    st = lambda seq: PrinterStatus(printer_id=pid, connection=ConnState.ONLINE, report_seq=seq)  # noqa: E731
    for seq in (27024, 27025, 27026):
        m._track_boot(st(seq))
    m._track_boot(st(27010))                               # small wobble: not a reboot
    m._track_boot(st(28))                                  # one low report: not yet
    assert "booted_at" not in m._boots[pid]
    m._track_boot(st(29))                                  # second low report: rebooted
    booted = m._boots[pid]["booted_at"]
    assert booted and m._boots[pid]["seq"] == 29
    m2 = Manager(store)                                    # dashboard restart: remembered
    s2 = st(35)
    asyncio.run(m2._on_update(s2))
    assert s2.booted_at is not None and s2.booted_at.isoformat() == booted
    m2._track_boot(st(3))                                  # rebooted while the dashboard was down ...
    m2._track_boot(st(4))
    assert m2._boots[pid]["booted_at"] >= booted           # ... dated when noticed (fine)


def test_cc_reboot_detected_from_position(tmp_path):
    """CC: parked 202,264.5,224.4 -> rebooted 0,0,0 -> homed 202,264.5,14.35 (a real CC 2026-11-05)."""
    from app.drivers.sdcp.parse import unhomed
    from app.manager import Manager
    from app.models import PrinterStatus
    from app.store import PrinterStore
    store = PrinterStore(tmp_path / "d.db")
    pid = store.add("Centauri Carbon", "elegoo.cc1", "10.0.0.202", "2c7e1954").id
    m = Manager(store)
    st = lambda c: PrinterStatus(printer_id=pid, connection=ConnState.ONLINE, unhomed=unhomed(c))  # noqa: E731
    asyncio.run(m._on_update(st("0.00,0.00,0.00")))          # first sight, unhomed: unknown history -> nothing
    assert "booted_at" not in m._boots[pid]
    asyncio.run(m._on_update(st("202.00,264.50,14.35")))     # homed
    asyncio.run(m._on_update(st("202.00,264.50,224.40")))    # parked after a print
    assert "booted_at" not in m._boots[pid]
    s = st("0.00,0.00,0.00")
    asyncio.run(m._on_update(s))                              # rebooted
    assert m._boots[pid]["booted_at"] and s.booted_at is not None
    m2 = Manager(store)                                       # remembered across a dashboard restart
    s2 = st("202.00,264.50,14.35")
    asyncio.run(m2._on_update(s2))
    assert s2.booted_at is not None


def test_new_access_code_taken_from_the_account(tmp_path, cloud):
    """Printer set up again (new Wi-Fi, re-added in Handy): its LAN code changed. The account knows the new one."""
    cloud.devices = [{"dev_id": SERIAL, "name": "P1", "online": True, "dev_access_code": "87654321"}]
    async def go():
        (tmp_path / "c").mkdir(); (tmp_path / "l").mkdir()
        async with FakeBambu(tmp_path / "c", fixture(FX), user="u_42", code=cloud.token) as broker, \
                FakeBambu(tmp_path / "l", fixture(FX), code="87654321") as lan:
            link(cloud, broker)
            d, seen = await drive(0, lan.mqtt_port, connection="local")          # saved code: 12345678 (old)
            d.start(); await asyncio.sleep(1.5); await d.stop()
            return d, seen
    d, seen = asyncio.run(go())
    online = [s for s in seen if s.connection == ConnState.ONLINE]
    assert online and online[-1].link == "local"
    assert d.learned_access_code == "87654321"                                   # manager saves it


def test_wrong_code_and_no_account_still_says_so(tmp_path):
    bambu_cloud.set_account(None)                    # (other tests may leave a linked account behind)
    async def go():
        async with FakeBambu(tmp_path, fixture(FX), code="87654321") as lan:
            d, seen = await drive(0, lan.mqtt_port, connection="local")
            d.start()
            for _ in range(50):                      # up to 5 s (a fixed 1 s wait was flaky under load)
                if any(s.connection == ConnState.AUTH_ERROR for s in seen):
                    break
                await asyncio.sleep(0.1)
            await d.stop()
            return d, seen
    d, seen = asyncio.run(go())
    assert any(s.connection == ConnState.AUTH_ERROR for s in seen), [(s.connection, s.connection_detail) for s in seen]
    assert d.learned_access_code is None
