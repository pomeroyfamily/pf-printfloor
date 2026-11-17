"""Request guard: allowed host names, cross-site writes, security headers."""
import pytest
from fastapi.testclient import TestClient

from app import config, guard as G
from app.cloud import bambu as bc
from app.manager import Manager
from app.web import create_app


@pytest.mark.parametrize("host,ok", [
    ("192.168.1.50:8765", True), ("10.0.0.5", True), ("[fe80::1]:8765", True), ("localhost:8765", True),
    ("truenas:8765", True), ("nas.local", True), ("printfloor.home.arpa", True), ("printer.lan:8765", True),
    ("attacker.example.com", False), ("evil.rebind.network:8765", False), ("", False),
])
def test_host_names(host, ok):
    assert G.host_allowed(host) is ok


def test_allowed_hosts_setting():
    assert G.host_allowed("printfloor.example.com", ["printfloor.example.com"])
    assert G.host_allowed("a.example.com:443", ["*.example.com"])
    assert not G.host_allowed("example.org", ["*.example.com"])
    assert G.host_allowed("anything.example", ["*"])


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "THUMB_DIR", tmp_path / "thumbs")
    monkeypatch.setattr(Manager, "_start", lambda self, cfg: None)
    monkeypatch.setenv("PRINTER_DASH_ALLOWED_HOSTS", "printfloor.example.com")
    bc.set_account(None)
    with TestClient(create_app(tmp_path / "db.sqlite", start_drivers=False, ssdp_ports=())) as c:
        yield c


def test_requests(client):
    r = client.get("/")
    assert r.status_code == 200 and r.headers["x-frame-options"] == "DENY"
    csp = r.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in csp and "'sha256-" in csp
    assert client.get("/api/catalog").headers["x-content-type-options"] == "nosniff"
    # DNS rebinding: someone else's name pointing at this machine
    assert client.get("/api/printers", headers={"Host": "evil.example.net"}).status_code == 421
    assert client.get("/api/printers", headers={"Host": "printfloor.example.com"}).status_code == 200
    # cross-site writes are refused, same-site page and tools without browser headers are fine
    body = {"name": "x", "urls": []}
    assert client.post("/api/notify/people", json=body, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert client.post("/api/notify/people", json=body, headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.post("/api/discover/network", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert client.post("/api/notify/people", json=body, headers={"Sec-Fetch-Site": "same-origin"}).status_code == 200
    assert client.post("/api/notify/people", json=body, headers={"Origin": "http://testserver"}).status_code == 200
    assert client.post("/api/notify/people", json=body).status_code == 200            # curl, scripts
    assert client.get("/api/catalog", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 200   # reads ok


def test_csv_cells_are_not_formulas():
    from app.web import _cell
    assert _cell("=HYPERLINK(\"http://x\")") == "'=HYPERLINK(\"http://x\")"
    assert _cell("-cmd") == "'-cmd" and _cell("Benchy") == "Benchy" and _cell(None) is None
