import json

import pytest
from fastapi.testclient import TestClient

from app import config
from app.cloud import bambu as bc
from app.manager import Manager
from app.web import create_app
from tests.fakes.bambu_cloud import FakeCloud

BAMBU = {"driver": "bambu.p1p", "name": "P2", "host": "10.0.0.189", "serial": "01S099000000014",
         "secrets": {"access_code": "aaaa1111"}}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "THUMB_DIR", tmp_path / "thumbs")
    monkeypatch.setattr(Manager, "_start", lambda self, cfg: None)    # no live connections in these tests
    bc.set_account(None)
    with TestClient(create_app(tmp_path / "db.sqlite", start_drivers=False, ssdp_ports=())) as c:
        yield c
    bc.set_account(None)


def test_page_and_catalog(client):
    assert "PF PrintFloor" in client.get("/").text
    cat = {m["maker"]: m for m in client.get("/api/catalog").json()}
    assert all(x["available"] for x in cat["Bambu Lab"]["models"])
    assert all(m["available"] for maker in cat.values() for m in maker["models"])   # every driver is built


def test_add_list_edit_reorder_remove(client):
    a = client.post("/api/printers", json=BAMBU).json()
    b = client.post("/api/printers", json={**BAMBU, "name": "P1", "serial": "01S099000000015"}).json()
    listed = client.get("/api/printers").json()
    assert [p["name"] for p in listed] == ["P2", "P1"]
    assert "aaaa1111" not in json.dumps(listed) and listed[0]["secrets_set"] == {"access_code": True}
    assert listed[0]["model"] == "P1P" and listed[0]["maker"] == "Bambu Lab"

    r = client.patch(f"/api/printers/{a['id']}", json={"name": "P2 renamed", "secrets": {"access_code": ""}})
    assert r.status_code == 200 and r.json()["name"] == "P2 renamed" and r.json()["secrets_set"]["access_code"]

    assert client.put("/api/printers/order", json={"ids": [b["id"], a["id"]]}).status_code == 204
    assert [p["name"] for p in client.get("/api/printers").json()] == ["P1", "P2 renamed"]
    assert client.put("/api/printers/order", json={"ids": [a["id"]]}).status_code == 400

    assert client.delete(f"/api/printers/{a['id']}").status_code == 204
    assert client.delete(f"/api/printers/{a['id']}").status_code == 404
    assert [p["name"] for p in client.get("/api/printers").json()] == ["P1"]


def test_add_rejections(client):
    assert client.post("/api/printers", json={**BAMBU, "secrets": {}}).status_code == 400          # no access code
    assert client.post("/api/printers", json={**BAMBU, "driver": "nope"}).status_code == 400
    assert client.post("/api/printers", json=BAMBU).status_code == 201
    assert client.post("/api/printers", json=BAMBU).status_code == 409                              # same serial


def test_thumbnail_media_type(client):
    pid = client.post("/api/printers", json=BAMBU).json()["id"]
    assert client.get(f"/api/printers/{pid}/thumbnail").status_code == 404
    config.THUMB_DIR.mkdir(parents=True)
    (config.THUMB_DIR / f"{pid}.png").write_bytes(b"\xff\xd8\xff\xe0" + b"0" * 20)
    r = client.get(f"/api/printers/{pid}/thumbnail")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"


def test_bambu_account_flow(client, monkeypatch):
    fc = FakeCloud(tasks=[], covers={}, mode="tfa")
    monkeypatch.setitem(bc.REGIONS, "global", (fc.base, fc.base))
    try:
        assert client.get("/api/bambu-account").json()["linked"] is False
        r = client.post("/api/bambu-account/login", json={"email": fc.email, "password": "bad"})
        assert r.status_code == 400 and "wrong password" in r.json()["detail"]
        r = client.post("/api/bambu-account/login", json={"email": fc.email, "password": fc.password})
        assert r.json() == {"step": "tfa"}
        assert client.post("/api/bambu-account/code", json={"code": "000000"}).status_code == 400
        assert client.post("/api/bambu-account/code", json={"code": fc.code}).json()["step"] == "done"
        st = client.get("/api/bambu-account").json()
        assert st["linked"] and st["state"] == "linked" and st["email"] == fc.email
        assert fc.token not in json.dumps(st)
        assert bc.get_account() is not None
        assert client.delete("/api/bambu-account").status_code == 204
        assert client.get("/api/bambu-account").json()["linked"] is False
    finally:
        fc.close()


def test_tags_per_printer_list_rename_delete(client):
    a = client.post("/api/printers", json=BAMBU).json()
    b = client.post("/api/printers", json={**BAMBU, "name": "P1", "serial": "01S099000000015"}).json()
    r = client.patch(f"/api/printers/{a['id']}", json={"tags": ["  Hardened   Steel ", "hardened steel", "0.6 nozzle", ""]})
    assert r.json()["tags"] == ["Hardened Steel", "0.6 nozzle"]                 # trimmed, de-duplicated
    client.patch(f"/api/printers/{b['id']}", json={"tags": ["hardened steel"]})
    assert [(t["name"], t["count"]) for t in client.get("/api/tags").json()] == [("0.6 nozzle", 1), ("Hardened Steel", 2)]
    assert client.post("/api/tags/retag", json={"old": "HARDENED STEEL", "new": "HS nozzle"}).json() == {"changed": 2}
    tags = {p["name"]: p["tags"] for p in client.get("/api/printers").json()}
    assert tags == {"P2": ["HS nozzle", "0.6 nozzle"], "P1": ["HS nozzle"]}
    assert client.post("/api/tags/retag", json={"old": "hs nozzle"}).json() == {"changed": 2}      # delete
    assert {p["name"]: p["tags"] for p in client.get("/api/printers").json()} == {"P2": ["0.6 nozzle"], "P1": []}


def test_manage_tags_create_assign_and_keep_empty(client):
    a = client.post("/api/printers", json=BAMBU).json()
    b = client.post("/api/printers", json={**BAMBU, "name": "P1", "serial": "01S099000000015"}).json()
    client.patch(f"/api/printers/{a['id']}", json={"tags": ["0.6 nozzle"]})
    # a tag no printer has yet is kept and listed
    r = client.put("/api/tags", json={"name": "  Hardened  steel ", "printers": []})
    assert r.status_code == 200 and r.json() == {"name": "Hardened steel", "count": 0, "printers": []}
    names = lambda: {t["name"]: sorted(t["printers"]) for t in client.get("/api/tags").json()}
    assert names() == {"0.6 nozzle": [a["id"]], "Hardened steel": []}
    # choose exactly which printers have it; other tags untouched
    client.put("/api/tags", json={"name": "hardened STEEL", "printers": [a["id"], b["id"]]})
    tags = {p["name"]: p["tags"] for p in client.get("/api/printers").json()}
    assert tags == {"P2": ["0.6 nozzle", "Hardened steel"], "P1": ["Hardened steel"]}   # existing spelling kept
    client.put("/api/tags", json={"name": "Hardened steel", "printers": [b["id"]]})
    client.put("/api/tags", json={"name": "0.6 nozzle", "printers": []})
    assert names() == {"0.6 nozzle": [], "Hardened steel": [b["id"]]}                 # emptied tag survives
    # rename and delete also update the defined list
    client.post("/api/tags/retag", json={"old": "0.6 nozzle", "new": "0.4 nozzle"})
    assert set(names()) == {"0.4 nozzle", "Hardened steel"}
    client.post("/api/tags/retag", json={"old": "0.4 nozzle"})
    assert set(names()) == {"Hardened steel"}
    assert client.put("/api/tags", json={"name": "x", "printers": ["nope"]}).status_code == 404
    assert client.put("/api/tags", json={"name": "   ", "printers": []}).status_code == 400


def test_tag_edit_keeps_other_settings_and_does_not_reconnect(client, monkeypatch):
    p = client.post("/api/printers", json={**BAMBU, "name": None}).json()          # auto_name placeholder
    restarts = []
    monkeypatch.setattr(Manager, "_restart", lambda self, cfg: restarts.append(cfg.id) or _noop())
    r = client.patch(f"/api/printers/{p['id']}", json={"tags": ["x"], "host": BAMBU["host"]}).json()
    assert r["tags"] == ["x"] and r["settings"].get("auto_name") is True and not restarts
    client.patch(f"/api/printers/{p['id']}", json={"host": "10.0.0.200"})
    assert restarts == [p["id"]]


async def _noop():
    return None


def test_usage_edit_audit_and_history_endpoints(client):
    a = client.post("/api/printers", json=BAMBU).json()
    u = client.get(f"/api/printers/{a['id']}/usage").json()
    assert u["total_seconds"] == 0 and u["recent"] == [] and u["linkable"] == []
    r = client.put(f"/api/printers/{a['id']}/usage", json={"hours": 5000, "jobs": 900})
    assert r.status_code == 200 and r.json()["total_seconds"] == 5000 * 3600
    assert r.json()["baseline_source"] == "manual" and r.json()["edits"]
    s = client.get("/api/history/summary").json()
    assert s["fleet"]["total_seconds"] == 5000 * 3600 and s["printers"][0]["audit_flagged"] is False
    assert client.get("/api/history").json() == []
    csv = client.get("/api/history.csv")
    assert csv.status_code == 200 and csv.text.splitlines()[0].startswith("id,") or "printer" in csv.text.splitlines()[0]
    assert client.put(f"/api/printers/{a['id']}/usage", json={"hours": -1, "jobs": 0}).status_code in (400, 422)


def test_bambu_hours_from_account_use_per_model_units(client):
    import asyncio
    mgr = client.app.state.mgr
    x1c = client.post("/api/printers", json={**BAMBU, "driver": "bambu.x1c", "name": "X1C", "serial": "00M099000000008"}).json()
    p1 = client.post("/api/printers", json={**BAMBU, "driver": "bambu.p1p", "name": "P1", "serial": "01S099000000015"}).json()
    a1 = client.post("/api/printers", json={**BAMBU, "driver": "bambu.a1", "name": "A1-A", "serial": "039199000000017"}).json()

    class Cloud:
        def devices(self):
            return [{"dev_id": "00M099000000008", "total_print_time": 30042000},   # seconds -> 8,345 h
                    {"dev_id": "01S099000000015", "total_print_time": 718680},     # minutes -> 11,978 h
                    {"dev_id": "039199000000017", "total_print_time": 368263}]     # minutes -> 6,137 h
    acct = bc.Account(Cloud(), "d@example.com")
    bc.set_account(acct)
    n = asyncio.run(mgr.refresh_bambu_hours())
    assert n == 3
    hrs = lambda p: round(client.get(f"/api/printers/{p['id']}/usage").json()["total_seconds"] / 3600)
    assert hrs(x1c) == 8345 and hrs(p1) == 11978 and hrs(a1) == 6138


def test_page_links_versioned_css_and_js(client):
    """After an update the browser must fetch the new CSS/JS, not a cached copy (2026-10-30)."""
    import re
    html = client.get("/").text
    css = re.search(r'href="/static/app\.css\?v=([0-9a-f]{10})"', html)
    js = re.search(r'src="/static/app\.js\?v=([0-9a-f]{10})"', html)
    assert css and js and css.group(1) == js.group(1)
    assert client.get(f"/static/app.css?v={css.group(1)}").status_code == 200


def test_stalled_live_stream_is_dropped_and_ends(client):
    m = client.app.state.mgr
    q = m.subscribe()
    for _ in range(501):                      # a tab that stopped reading fills its queue
        m._broadcast({"type": "ping"})
    assert not m.subscribed(q)                # dropped, so its stream ends and the browser reconnects


def test_installable_app_manifest_uses_dashboard_name(client):
    m = client.get("/manifest.webmanifest")
    assert m.status_code == 200 and m.headers["content-type"].startswith("application/manifest+json")
    assert m.json()["name"] == "PF PrintFloor" and m.json()["display"] == "standalone"
    for icon in m.json()["icons"]:
        assert client.get(icon["src"]).status_code == 200
    assert client.get("/static/icons/icon-180.png").status_code == 200           # iOS home screen icon
    client.put("/api/dashboard", json={"name": 'Shop "A" <1>'})
    assert client.get("/manifest.webmanifest").json()["name"] == 'Shop "A" <1>'
    page = client.get("/").text
    assert "<title>Shop &quot;A&quot; &lt;1&gt;</title>" in page
    assert 'name="apple-mobile-web-app-title" content="Shop &quot;A&quot; &lt;1&gt;"' in page
