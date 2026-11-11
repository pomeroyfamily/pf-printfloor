"""Names that follow the printer: blank name on add -> placeholder, then the name the printer announces (SSDP),
and later renames on the printer. A typed name stops following; "Use the printer's name" turns it back on."""
import json
import socket
import time

import pytest
from fastapi.testclient import TestClient

from app import config
from app.cloud import bambu as bc
from app.discovery.bambu_ssdp import parse
from app.manager import Manager
from app.web import create_app

# Shape of the NOTIFY real printers sent (bambu_discover.py output 2026-10-28)
NOTIFY = ("NOTIFY * HTTP/1.1\r\nHOST: 239.255.255.250:1990\r\nServer: UPnP/1.0\r\nLocation: {ip}\r\n"
          "NT: urn:bambulab-com:device:3dprinter:1\r\nUSN: {serial}\r\nCache-Control: max-age=1800\r\n"
          "DevModel.bambu.com: C11\r\nDevName.bambu.com: {name}\r\nDevConnect.bambu.com: cloud\r\n"
          "DevBind.bambu.com: occupied\r\nDevVersion.bambu.com: 01.10.00.00\r\n\r\n")
P2 = {"driver": "bambu.p1p", "host": "10.0.0.189", "secrets": {"access_code": "aaaa1111"}}


def free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def announce(port, name="P2", ip="10.0.0.189", serial="01S099000000014"):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.sendto(NOTIFY.format(ip=ip, serial=serial, name=name).encode(), ("127.0.0.1", port))


def names(c):
    return [p["name"] for p in c.get("/api/printers").json()]


def wait_for(c, want, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if names(c) == want:
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "THUMB_DIR", tmp_path / "thumbs")
    monkeypatch.setattr(Manager, "_start", lambda self, cfg: None)    # no live connections in these tests
    bc.set_account(None)
    port = free_udp_port()
    with TestClient(create_app(tmp_path / "db.sqlite", start_drivers=False, ssdp_ports=(port,))) as c:
        yield c, port


def test_parse_announcement():
    a = parse(NOTIFY.format(ip="10.0.0.90", serial="00M099000000008", name="X1C").encode(), "10.0.0.90")
    assert (a.serial, a.host, a.name, a.model_code) == ("00M099000000008", "10.0.0.90", "X1C", "C11")
    assert parse(b"M-SEARCH * HTTP/1.1\r\n\r\n", "1.2.3.4") is None


def test_blank_name_uses_announced_name(ctx):
    c, port = ctx
    r = c.post("/api/printers", json=P2)
    assert r.status_code == 201 and r.json()["name"] == "P1P at 10.0.0.189"
    announce(port, name="P2")
    assert wait_for(c, ["P2"])
    announce(port, name="P2 Garage")               # renamed on the printer (Handy / Studio): the card follows
    assert wait_for(c, ["P2 Garage"])


def test_already_heard_names_immediately(ctx):
    c, port = ctx
    announce(port, name="P2")
    time.sleep(0.3)
    assert c.post("/api/printers", json=P2).json()["name"] == "P2"


def test_typed_name_is_never_replaced(ctx):
    c, port = ctx
    c.post("/api/printers", json={**P2, "name": "Left P1P"})
    pid = c.post("/api/printers", json={**P2, "host": "10.0.0.147"}).json()["id"]
    c.patch(f"/api/printers/{pid}", json={"name": "Right P1P"})      # renamed before the printer spoke
    announce(port, name="P2")
    announce(port, name="P1", ip="10.0.0.147", serial="01S099000000015")
    time.sleep(0.4)
    assert names(c) == ["Left P1P", "Right P1P"]


def test_follow_box_on_and_off(ctx):
    c, port = ctx
    pid = c.post("/api/printers", json={**P2, "name": "Left P1P"}).json()["id"]
    announce(port, name="P2")
    time.sleep(0.3)
    p = c.get("/api/printers").json()[0]
    assert p["name"] == "Left P1P" and p["follow_name"] is False and p["reported_name"] == "P2"
    r = c.patch(f"/api/printers/{pid}", json={"follow_name": True}).json()          # tick: takes it at once
    assert r["name"] == "P2" and r["follow_name"] is True
    announce(port, name="P2 Garage")
    assert wait_for(c, ["P2 Garage"])
    r = c.patch(f"/api/printers/{pid}", json={"name": "My P1P"}).json()             # typing a name unticks it
    assert r["name"] == "My P1P" and r["follow_name"] is False
    announce(port, name="P2 Again")
    time.sleep(0.3)
    assert names(c) == ["My P1P"]


def test_history_shows_current_name(ctx):
    c, port = ctx
    pid = c.post("/api/printers", json={**P2, "name": "Old name"}).json()["id"]
    m = c.app.state.mgr
    cfg = m.store.get(pid)
    from app.history import usage_key
    m.history.db.execute("INSERT INTO jobs (usage_key, printer_id, printer_name, job_name, started_at, ended_at, "
                         "print_seconds, result, source) VALUES (?, ?, 'Old name', 'benchy', '2026-11-01T00:00:00Z', "
                         "'2026-11-01T01:00:00Z', 3600, 'finished', 'tracked')", (usage_key(cfg), pid))
    m.history.db.commit()
    c.patch(f"/api/printers/{pid}", json={"name": "New name"})
    assert [r["printer_name"] for r in c.get("/api/history").json()] == ["New name"]
    assert "New name" in c.get("/api/history.csv").text


# ---------------------------------------------------------------------- add from the Bambu account
from tests.fakes.bambu_cloud import FakeCloud  # noqa: E402

# Field names and values as returned for a real account 2026-10-29 (codes replaced)
DEVICES = [
    {"dev_id": "01S099000000014", "name": "P2", "online": True, "print_status": "RUNNING", "dev_model_name": "C11",
     "dev_product_name": "P1P", "dev_access_code": "11112222", "nozzle_diameter": 0.4},
    {"dev_id": "00M099000000008", "name": "X1C", "online": True, "print_status": "RUNNING",
     "dev_model_name": "BL-P001", "dev_product_name": "X1 Carbon", "dev_access_code": "33334444"},
    {"dev_id": "0309C9000000016", "name": "Mini-Demo", "online": True, "dev_model_name": "N1",
     "dev_product_name": "A1 mini", "dev_access_code": "55556666"},
    {"dev_id": "01P009000000013", "name": "Friend P1S", "online": False, "dev_model_name": "C12",
     "dev_product_name": "P1S", "dev_access_code": "77778888"},
]


@pytest.fixture
def linked(ctx, monkeypatch):
    c, port = ctx
    fc = FakeCloud(tasks=[], covers={}, mode="direct")
    fc.devices = DEVICES
    monkeypatch.setitem(bc.REGIONS, "global", (fc.base, fc.base))
    assert c.post("/api/bambu-account/login", json={"email": fc.email, "password": fc.password}).json()["step"] == "done"
    yield c, port, fc
    fc.close()


def test_account_list_needs_sign_in(ctx):
    c, _ = ctx
    assert c.get("/api/bambu-account/printers").status_code == 409


def test_account_list_merges_ip_and_added(linked):
    c, port, _ = linked
    announce(port, name="P2", ip="10.0.0.189", serial="01S099000000014")
    announce(port, name="X1C", ip="10.0.0.90", serial="00M099000000008")
    time.sleep(0.3)
    c.post("/api/printers", json={"driver": "bambu.x1c", "host": "10.0.0.90", "serial": "00M099000000008",
                                   "secrets": {"access_code": "x"}})
    rows = {r["name"]: r for r in c.get("/api/bambu-account/printers").json()}
    assert rows["P2"]["host"] == "10.0.0.189" and rows["P2"]["driver"] == "bambu.p1p" and not rows["P2"]["added_id"]
    assert rows["X1C"]["added_id"]
    assert rows["Mini-Demo"]["host"] is None                      # not heard yet
    assert rows["Friend P1S"]["supported"] is False
    assert "11112222" not in json.dumps(rows)                         # access codes never leave the server


def test_import_from_account(linked):
    c, port, _ = linked
    announce(port, name="P2", ip="10.0.0.189", serial="01S099000000014")
    time.sleep(0.3)
    res = c.post("/api/bambu-account/printers", json={"printers": [
        {"serial": "01S099000000014"},                                 # IP heard
        {"serial": "0309C9000000016", "host": "10.0.0.71"},         # IP typed
        {"serial": "00M099000000008"},                                 # IP unknown
        {"serial": "01P009000000013", "host": "1.2.3.4"},              # unsupported model
    ]}).json()
    by = {r["serial"]: r for r in res}
    assert by["01S099000000014"].get("id") and by["0309C9000000016"].get("id")
    assert "IP" in by["00M099000000008"]["error"] and "not supported" in by["01P009000000013"]["error"]
    printers = {p["name"]: p for p in c.get("/api/printers").json()}
    assert printers["P2"]["host"] == "10.0.0.189" and printers["P2"]["serial"] == "01S099000000014"
    assert printers["Mini-Demo"]["driver"] == "bambu.a1mini" and printers["Mini-Demo"]["secrets_set"]["access_code"]
    again = c.post("/api/bambu-account/printers", json={"printers": [{"serial": "01S099000000014"}]}).json()
    assert again[0]["error"] == "already added"
