"""Claude connector: read-only MCP endpoint at /mcp."""
import pytest
from fastapi.testclient import TestClient

from app import config
from app.cloud import bambu as bc
from app.manager import Manager
from app.models import ConnState, Job, JobState, PrinterStatus
from app.web import create_app

HOST = "10.0.0.189"
SERIAL = "01S099000000014"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "THUMB_DIR", tmp_path / "thumbs")
    monkeypatch.setattr(Manager, "_start", lambda self, cfg: None)
    bc.set_account(None)
    with TestClient(create_app(tmp_path / "db.sqlite", start_drivers=False, ssdp_ports=())) as c:
        yield c


def rpc(c, token, method, params=None, id_=1, **kw):
    body = {"jsonrpc": "2.0", "id": id_, "method": method}
    if params is not None:
        body["params"] = params
    return c.post("/mcp", json=body, headers={"Authorization": f"Bearer {token}", **kw.get("headers", {})})


def add_printer(c, name="Garage X1C"):
    pid = c.post("/api/printers", json={"driver": "bambu.x1c", "host": HOST, "serial": SERIAL, "name": name,
                                        "secrets": {"access_code": "aaaa1111"}}).json()["id"]
    m = c.app.state.mgr
    m.status[pid] = PrinterStatus(printer_id=pid, connection=ConnState.ONLINE, state=JobState.PRINTING,
                                  job=Job(name="Benchy", progress_pct=42.0, remaining_s=1800))
    return pid


def test_off_until_token_created(client):
    assert client.post("/mcp", json={}).status_code == 404
    assert client.get("/api/connector").json()["enabled"] is False


def test_token_required_and_rotates(client):
    tok = client.post("/api/connector").json()["token"]
    assert tok.startswith("pfp_") and len(tok) > 40
    v = client.get("/api/connector").json()
    assert v["enabled"] and v["hint"] == tok[-4:] and "token" not in v
    assert rpc(client, "wrong", "ping").status_code == 401
    assert client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}).status_code == 401
    assert rpc(client, tok, "ping").json()["result"] == {}
    assert client.get("/api/connector").json()["last_used_at"]
    new = client.post("/api/connector").json()["token"]
    assert rpc(client, tok, "ping").status_code == 401 and rpc(client, new, "ping").status_code == 200
    assert client.delete("/api/connector").status_code == 204
    assert rpc(client, new, "ping").status_code == 404
    # the token is only stored hashed
    assert new not in str(client.app.state.mgr.store.get_app_setting("mcp_token"))


def test_wrong_tokens_throttled(client):
    tok = client.post("/api/connector").json()["token"]
    codes = [rpc(client, "nope", "ping").status_code for _ in range(25)]
    assert codes[0] == 401 and codes[-1] == 429
    assert rpc(client, tok, "ping").status_code == 429      # everyone waits a minute; tokens can't be guessed anyway


def test_browser_pages_cant_create_tokens(client):
    r = client.post("/api/connector", headers={"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403


def test_handshake_and_tools(client):
    tok = client.post("/api/connector").json()["token"]
    init = rpc(client, tok, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                          "clientInfo": {"name": "t", "version": "1"}}).json()["result"]
    assert init["protocolVersion"] == "2025-06-18" and "tools" in init["capabilities"]
    odd = rpc(client, tok, "initialize", {"protocolVersion": "1999-01-01"}).json()["result"]
    assert odd["protocolVersion"] == "2025-11-25"
    r = client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                    headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 202
    tools = rpc(client, tok, "tools/list").json()["result"]["tools"]
    assert {t["name"] for t in tools} == {"list_printers", "get_printer", "recent_jobs", "print_stats", "camera_snapshot"}
    assert all(t["annotations"]["readOnlyHint"] for t in tools)
    assert rpc(client, tok, "printers/power").json()["error"]["code"] == -32601
    assert rpc(client, tok, "tools/call", {"name": "set_power", "arguments": {}}).json()["error"]["code"] == -32602


def call(c, tok, name, args=None):
    return rpc(c, tok, "tools/call", {"name": name, "arguments": args or {}}).json()["result"]


def test_list_and_get_never_leak_addresses(client):
    add_printer(client)
    tok = client.post("/api/connector").json()["token"]
    res = call(client, tok, "list_printers")
    text = res["content"][0]["text"]
    assert not res["isError"] and "Garage X1C" in text and "Benchy" in text and '"printing"' in text
    det = call(client, tok, "get_printer", {"printer": "garage"})          # part of the name, any case
    for r in (res, det):
        blob = str(r)
        assert HOST not in blob and SERIAL not in blob and "aaaa1111" not in blob


def test_printer_lookup_errors(client):
    add_printer(client, "Garage X1C")
    tok = client.post("/api/connector").json()["token"]
    r = call(client, tok, "get_printer", {"printer": "nope"})
    assert r["isError"] and "Garage X1C" in r["content"][0]["text"]
    r = call(client, tok, "camera_snapshot", {"printer": "garage"})
    assert r["isError"]                                                  # no camera configured in tests


def test_jobs_and_stats(client):
    add_printer(client)
    tok = client.post("/api/connector").json()["token"]
    r = call(client, tok, "recent_jobs", {"limit": 5})
    assert not r["isError"] and '"jobs"' in r["content"][0]["text"]
    r = call(client, tok, "recent_jobs", {"result": "exploded"})
    assert r["isError"]
    r = call(client, tok, "print_stats")
    assert not r["isError"] and '"fleet"' in r["content"][0]["text"]
    r = call(client, tok, "print_stats", {"printer": "Garage X1C"})
    assert '"fleet"' not in r["content"][0]["text"]


def test_get_and_delete_on_mcp_not_allowed(client):
    client.post("/api/connector")
    assert client.get("/mcp").status_code == 405


def test_camera_snapshot_returns_image(client, monkeypatch):
    add_printer(client)
    tok = client.post("/api/connector").json()["token"]

    class Feed:
        error = None
        def lease(self): pass
        async def next_frame(self, after, timeout): return (7, b"\xff\xd8\xffJPEGDATA")
    monkeypatch.setattr(client.app.state.mgr.cameras, "feed", lambda cfg: Feed())
    r = call(client, tok, "camera_snapshot", {"printer": "Garage X1C"})
    img = r["content"][1]
    assert not r["isError"] and img["type"] == "image" and img["mimeType"] == "image/jpeg"
    import base64
    assert base64.b64decode(img["data"]).startswith(b"\xff\xd8\xff")


def test_secret_url_and_api_key_header(client):
    tok = client.post("/api/connector").json()["token"]
    body = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    assert client.post(f"/mcp/{tok}", json=body).json()["result"] == {}
    assert client.post("/mcp", json=body, headers={"X-API-Key": tok}).status_code == 200
    assert client.post("/mcp/pfp_wrong", json=body).status_code == 401
    assert client.get(f"/mcp/{tok}").status_code == 405


def test_location_is_its_name_not_its_id(client):
    add_printer(client)
    tok = client.post("/api/connector").json()["token"]
    locs = client.app.state.mgr.locations()
    name = next(x["name"] for x in locs["items"] if x["id"] == locs["default"])
    text = call(client, tok, "list_printers")["content"][0]["text"]
    assert f'"location":"{name}"' in text and locs["default"] not in text
    assert f'"location":"{name}"' in call(client, tok, "get_printer", {"printer": "Garage X1C"})["content"][0]["text"]
