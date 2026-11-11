"""Dashboard name + locations (2026-11-03)."""
from fastapi.testclient import TestClient

from app.web import create_app


def client(tmp_path):
    return TestClient(create_app(db_path=tmp_path / "d.db", start_drivers=False, ssdp_ports=()))


def add(c, name, host, **kw):
    r = c.post("/api/printers", json={"driver": "snapmaker.u1", "name": name, "host": host, **kw})
    assert r.status_code == 201, r.text
    return r.json()


def test_default_location_and_migration(tmp_path):
    with client(tmp_path) as c:
        lay = c.get("/api/layout").json()
        (home,) = lay["locations"]["items"]
        assert home["name"] == "Home" and lay["locations"]["default"] == home["id"]
        assert lay["dashboard"] == {"name": "PF PrintFloor", "banner": True, "tz": None}
        p = add(c, "U1-A", "10.0.0.5")
        assert p["location"] == home["id"]                       # no location given: the default


def test_locations_edit_move_delete(tmp_path):
    with client(tmp_path) as c:
        home = c.get("/api/layout").json()["locations"]["items"][0]
        r = c.put("/api/locations", json={"items": [{"id": home["id"], "name": "Garage"},
                                                    {"ref": "n1", "name": "Second Shop"}], "default": "n1"})
        assert r.status_code == 200, r.text
        locs = r.json()
        garage, beth = locs["items"]
        assert garage == {"id": home["id"], "name": "Garage"} and beth["name"] == "Second Shop"
        assert locs["default"] == beth["id"]                     # new one picked as default by its ref
        p = add(c, "U1-A", "10.0.0.5", location=garage["id"])
        q = add(c, "U1-B", "10.0.0.6")
        assert p["location"] == garage["id"] and q["location"] == beth["id"]
        r = c.patch(f"/api/printers/{q['id']}", json={"location": garage["id"]})
        assert r.json()["location"] == garage["id"]
        assert c.patch(f"/api/printers/{q['id']}", json={"location": "nope"}).status_code == 400
        # delete Garage: its printers move to the default
        c.put("/api/locations", json={"items": [{"id": beth["id"], "name": "Second Shop"}], "default": beth["id"]})
        assert {x["location"] for x in c.get("/api/printers").json()} == {beth["id"]}
        assert c.put("/api/locations", json={"items": [{"name": "A"}, {"name": "a"}]}).status_code == 400
        assert c.put("/api/locations", json={"items": [{"name": "  "}]}).status_code == 400


def test_rename_keeps_location(tmp_path):
    with client(tmp_path) as c:
        c.put("/api/locations", json={"items": [{"ref": "a", "name": "A"}, {"ref": "b", "name": "B"}], "default": "a"})
        a, b = c.get("/api/layout").json()["locations"]["items"]
        p = add(c, None, "10.0.0.5")                             # auto-named
        r = c.patch(f"/api/printers/{p['id']}", json={"name": "Mine", "location": b["id"]})
        assert r.json()["name"] == "Mine" and r.json()["location"] == b["id"]


def test_dashboard_name_and_banner(tmp_path):
    with client(tmp_path) as c:
        assert c.put("/api/dashboard", json={"name": "  My Print Farm "}).json() == {"name": "My Print Farm", "banner": True, "tz": None}
        assert c.put("/api/dashboard", json={"banner": False}).json() == {"name": "My Print Farm", "banner": False, "tz": None}
        assert c.get("/api/layout").json()["dashboard"]["banner"] is False
