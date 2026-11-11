"""Updating must never lose anyone's setup: a database written by an older release opens in this one with its
printers, access codes, history and settings intact (the app upgrades the schema itself on start).
When a release changes the database, add its own fixture with tools/make_upgrade_fixture.py."""
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config
from app.cloud import bambu as bc
from app.manager import Manager
from app.web import create_app

FIXTURES = sorted((Path(__file__).parent / "fixtures" / "upgrade").glob("v*.db"))


@pytest.mark.parametrize("old", FIXTURES, ids=[f.stem for f in FIXTURES])
def test_old_database_opens_with_everything(tmp_path, monkeypatch, old):
    monkeypatch.setattr(config, "THUMB_DIR", tmp_path / "thumbs")
    monkeypatch.setattr(Manager, "_start", lambda self, cfg: None)
    bc.set_account(None)
    db = tmp_path / "dashboard.db"
    shutil.copy(old, db)
    with TestClient(create_app(db, start_drivers=False, ssdp_ports=())) as c:
        printers = c.get("/api/printers").json()
        assert [p["name"] for p in printers] == ["Demo P1P"]
        assert c.app.state.mgr.store.get(printers[0]["id"]).secrets == {"access_code": "12345678"}
        assert c.get("/api/layout").json()["dashboard"]["name"] == "Test Farm"
        assert [j["job"] for j in c.get("/api/history").json()] == ["benchy"]
        r = c.post("/api/printers", json={"name": "New one", "driver": "bambu.a1", "host": "10.0.0.5",
                                          "secrets": {"access_code": "87654321"}})
        assert r.status_code in (200, 201)                                   # still writable after the upgrade
    with TestClient(create_app(db, start_drivers=False, ssdp_ports=())) as c:  # and again after a restart
        assert {p["name"] for p in c.get("/api/printers").json()} == {"Demo P1P", "New one"}
