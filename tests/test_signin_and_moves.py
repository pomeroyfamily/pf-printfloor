"""A rejected Bambu sign-in is remembered and shown everywhere; a Bambu printer that moved to a new address is
followed (only when it is safe to send it the access code)."""
import time

import pytest
from fastapi.testclient import TestClient

from app import config, manager as manager_mod
from app.cloud import bambu as bc
from app.manager import Manager
from app.store import PrinterStore
from app.web import create_app
from tests.fakes.bambu_cloud import FakeCloud
from tests.test_auto_name import announce, free_udp_port

SERIAL = "01S099000000014"


# ---------------------------------------------------------------------- rejected sign-in
@pytest.fixture
def fc():
    f = FakeCloud(tasks=[], covers={})
    yield f
    f.close()


def _linked(fc, store):
    """Sign in (saved like the real sign-in) and return the account, pointed at the fake cloud."""
    cloud = bc.BambuCloud(fc.token, api_base=fc.base, web_base=fc.base)
    bc.save_link(store, fc.email, cloud)
    acct = bc.account_from_store(store)
    acct.cloud = cloud
    return acct


def test_rejected_signin_is_remembered_and_cleared_by_signing_in(fc, tmp_path):
    store = PrinterStore(tmp_path / "s.db")
    acct = _linked(fc, store)
    seen = []
    acct.on_rejected = lambda: (bc.mark_rejected(store), seen.append(1))
    fc.reject_token = True
    assert acct.device(SERIAL, fresh=True) is None           # e.g. cloud fallback or access-code recovery asking
    assert acct.state == "needs_login" and seen == [1]
    acct.device(SERIAL, fresh=True)                           # told once, not on every failed call
    assert seen == [1]
    assert bc.account_from_store(store).state == "needs_login"       # survives a restart
    fc.reject_token = False
    assert _linked(fc, store).state == "linked"                       # signing in again clears it


def test_rejection_reaches_the_api_and_the_browsers(fc, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "THUMB_DIR", tmp_path / "thumbs")
    monkeypatch.setattr(Manager, "_start", lambda self, cfg: None)
    db = tmp_path / "db.sqlite"
    with TestClient(create_app(db, start_drivers=False, ssdp_ports=())) as c:
        m = c.app.state.mgr
        m.set_bambu_account(_linked(fc, m.store))
        assert c.get("/api/bambu-account").json()["state"] == "linked"
        fc.reject_token = True
        bc.get_account().device(SERIAL, fresh=True)          # a background lookup hits the expired sign-in
        assert c.get("/api/bambu-account").json()["state"] == "needs_login"
        assert c.get("/api/bambu-account/printers").status_code == 409
    bc.set_account(None)
    assert bc.account_from_store(PrinterStore(db)).state == "needs_login"


# ---------------------------------------------------------------------- moved printer
@pytest.fixture
def moves(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "THUMB_DIR", tmp_path / "thumbs")
    monkeypatch.setattr(Manager, "_start", lambda self, cfg: None)
    bc.set_account(None)
    probe = {"old_answers": False, "cn": SERIAL}
    monkeypatch.setattr(manager_mod, "_answers", lambda host, port: probe["old_answers"])
    monkeypatch.setattr(manager_mod, "peer_common_name", lambda host, port: probe["cn"])
    port = free_udp_port()
    with TestClient(create_app(tmp_path / "db.sqlite", start_drivers=False, ssdp_ports=(port,))) as c:
        yield c, port, probe


def _add(c, host="10.0.0.147"):
    return c.post("/api/printers", json={"name": "P1-1", "driver": "bambu.p1p", "host": host, "serial": SERIAL,
                                         "secrets": {"access_code": "12345678"}}).json()["id"]


def _host_after(c, pid, wait=1.5):
    end = time.time() + wait
    while time.time() < end:
        p = next(p for p in c.get("/api/printers").json() if p["id"] == pid)
        if p["host"] != "10.0.0.147":
            return p
        time.sleep(0.05)
    return p


def test_moved_printer_is_followed(moves):
    c, port, _ = moves
    pid = _add(c)
    announce(port, name="P1-1", ip="127.0.0.1", serial=SERIAL)      # new Wi-Fi: it now announces 127.0.0.1
    p = _host_after(c, pid)
    assert p["host"] == "127.0.0.1"
    assert p["settings"]["address_changed"]["from"] == "10.0.0.147"
    c.patch(f"/api/printers/{pid}", json={"host": "10.0.0.5"})     # typed by hand: the note goes away
    p = next(p for p in c.get("/api/printers").json() if p["id"] == pid)
    assert "address_changed" not in p["settings"]


@pytest.mark.parametrize("case", ["old_still_answers", "wrong_certificate", "claims_other_address", "dns_name"])
def test_not_followed_when_unsafe(moves, case):
    c, port, probe = moves
    pid = _add(c, host="p1.local" if case == "dns_name" else "10.0.0.147")
    if case == "old_still_answers":
        probe["old_answers"] = True
    if case == "wrong_certificate":
        probe["cn"] = "SOMETHINGELSE"
    ip = "10.0.0.77" if case == "claims_other_address" else "127.0.0.1"   # packet comes from 127.0.0.1
    announce(port, name="P1-1", ip=ip, serial=SERIAL)
    time.sleep(0.6)
    p = next(p for p in c.get("/api/printers").json() if p["id"] == pid)
    assert p["host"] == ("p1.local" if case == "dns_name" else "10.0.0.147")
