"""Make tests/fixtures/upgrade/vX.Y.Z.db: a database as the current version writes it (fake data only).
Run before a release that changes the database:  python tools/make_upgrade_fixture.py <empty temp folder>
(then rename the output file to the release version)."""
import sys, pathlib, sqlite3
sys.path.insert(0, ".")
from fastapi.testclient import TestClient
from app.web import create_app
from app.history import usage_key
out = pathlib.Path("tests/fixtures/upgrade/v0.1.0.db")
tmp = pathlib.Path(sys.argv[1]) / "db.sqlite"
with TestClient(create_app(tmp, start_drivers=False, ssdp_ports=())) as c:
    p = c.post("/api/printers", json={"name": "Demo P1P", "driver": "bambu.p1p", "host": "10.0.0.189",
                                      "serial": "01S090000000000", "secrets": {"access_code": "12345678"}}).json()
    c.post("/api/printers", json={"name": "Demo U1", "driver": "klipper.snapmaker_u1", "host": "10.0.0.154"})
    c.put("/api/dashboard", json={"name": "Test Farm", "banner": True})
    m = c.app.state.mgr
    cfg = m.store.get(p["id"])
    m.history.db.execute("INSERT INTO jobs (usage_key, printer_id, printer_name, job_name, started_at, ended_at, "
                         "print_seconds, result, source) VALUES (?, ?, 'Demo P1P', 'benchy', '2026-11-01T00:00:00Z', "
                         "'2026-11-01T01:00:00Z', 3600, 'finished', 'tracked')", (usage_key(cfg), p["id"]))
    m.history.db.commit()
    print(c.get("/api/printers").json()[0]["name"], c.get("/api/layout").json()["dashboard"])
con = sqlite3.connect(tmp); con.execute("PRAGMA wal_checkpoint(TRUNCATE)"); con.execute("VACUUM INTO ?", (str(out),)); con.close()
print("written", out, out.stat().st_size)
