import time
import pytest
from fastapi.testclient import TestClient

from app import config
from app import plugs as P
from app.manager import Manager
from app.models import ConnState, Job, JobState, PrinterStatus, Temperature
from app.web import create_app
from tests.fakes.govee import KEY, FakeGovee


@pytest.fixture
def env(tmp_path, monkeypatch):
    g = FakeGovee()
    monkeypatch.setattr(P, "BASE", g.base)
    monkeypatch.setattr(config, "THUMB_DIR", tmp_path / "thumbs")
    monkeypatch.setattr(Manager, "_start", lambda self, cfg: None)
    monkeypatch.setattr(P, "VERIFY_READS", (0.05, 0.05, 0.05))
    with TestClient(create_app(tmp_path / "db.sqlite", start_drivers=False, ssdp_ports=())) as c:
        m = c.app.state.mgr
        ids = {n: m.store.add(n, d, "127.0.0.1", None, {}, {}).id for n, d in [
            ("X1C", "bambu.x1c"), ("P1", "bambu.p1p"), ("P2", "bambu.p1p"), ("A1-A", "bambu.a1"),
            ("U1-C", "snapmaker.u1"), ("Centauri Carbon", "elegoo.cc1")]}
        yield c, g, m, ids
    g.close()


def set_status(m, pid, state=JobState.IDLE, nozzle=25.0, conn=ConnState.ONLINE):
    m.status[pid] = PrinterStatus(printer_id=pid, connection=conn, state=state, job=Job(name="Benchy"),
                                  temps=[Temperature(key="nozzle", label="Nozzle", current=nozzle, target=0)])


def test_link_rejects_bad_key_and_lists_only_plugs(env):
    c, g, m, ids = env
    assert c.put("/api/govee", json={"key": "wrong-key-xyz"}).status_code == 400
    v = c.put("/api/govee", json={"key": KEY}).json()
    assert v["linked"] and len(v["plugs"]) == 8 and all(p["sku"] == "H5086" for p in v["plugs"])   # lights ignored
    names = {p["device"]: p["name"] for p in v["plugs"]}
    sug = {pid: names[d] for pid, d in v["suggestions"].items()}
    assert sug[ids["X1C"]] == "X1" and sug[ids["U1-C"]] == "U1-C"            # exact / prefix name matches
    assert ids["P1"] not in sug and ids["Centauri Carbon"] not in sug           # ambiguous / none: user picks
    assert "key" not in str(c.get("/api/govee").json()).lower().replace("linked", "")  # key never sent back


def test_assign_power_on_off_logged_and_state_shown(env):
    c, g, m, ids = env
    plugs = {p["name"]: p["device"] for p in c.put("/api/govee", json={"key": KEY}).json()["plugs"]}
    pid = ids["A1-A"]
    r = c.put(f"/api/printers/{pid}/plug", json={"device": plugs["A1"]})
    assert r.status_code == 200 and r.json()["power"]["name"] == "A1"
    assert c.put(f"/api/printers/{ids['P1']}/plug", json={"device": plugs["A1"]}).status_code == 409   # one plug, one printer
    set_status(m, pid)                                                       # idle, cold: no confirmation needed
    assert c.post(f"/api/printers/{pid}/power", json={"on": False}).json() == {"ok": True, "on": False}
    assert g.power[plugs["A1"]] == 0 and g.controls == [(plugs["A1"], 0)]
    printer = next(p for p in c.get("/api/printers").json() if p["id"] == pid)
    assert printer["power"]["on"] is False                                   # card shows "Powered off"
    set_status(m, pid, conn=ConnState.OFFLINE)
    assert c.post(f"/api/printers/{pid}/power", json={"on": True}).json()["wait_s"] > 0     # 15 s cool-down
    assert g.controls == [(plugs["A1"], 0)]
    m.plugs.powered_off_at[pid] -= P.OFF_COOLDOWN_S
    assert c.post(f"/api/printers/{pid}/power", json={"on": True}).json()["on"] is True
    printer = next(p for p in c.get("/api/printers").json() if p["id"] == pid)
    assert printer["power"]["on"] is True and printer["power"]["starting"] is True   # "Starting up"
    log = c.get(f"/api/printers/{pid}/power").json()["log"]
    assert [(l["action"], l["ok"]) for l in log] == [("on", True), ("off", True)]
    assert c.put(f"/api/printers/{pid}/plug", json={"device": None}).json()["power"] is None


@pytest.mark.parametrize("state,nozzle", [(JobState.PRINTING, 220), (JobState.PAUSED, 200),
                                          (JobState.PREPARING, 150), (JobState.IDLE, 180)])
def test_emergency_stop_needs_typed_name(env, state, nozzle):
    c, g, m, ids = env
    plugs = {p["name"]: p["device"] for p in c.put("/api/govee", json={"key": KEY}).json()["plugs"]}
    pid = ids["X1C"]
    c.put(f"/api/printers/{pid}/plug", json={"device": plugs["X1"]})
    set_status(m, pid, state=state, nozzle=nozzle)
    r = c.post(f"/api/printers/{pid}/power", json={"on": False}).json()
    assert r["needs_confirm"] and r["type"] == "X1C" and r["reason"]
    assert c.post(f"/api/printers/{pid}/power", json={"on": False, "confirm": "x1"}).json()["needs_confirm"]
    assert g.controls == []                                                  # nothing switched yet
    ok = c.post(f"/api/printers/{pid}/power", json={"on": False, "confirm": " x1c "}).json()
    assert ok == {"ok": True, "on": False} and g.controls == [(plugs["X1"], 0)]
    note = c.get(f"/api/printers/{pid}/power").json()["log"][0]["note"]
    assert note.startswith("emergency stop")


def test_plug_failure_is_reported_and_logged(env):
    c, g, m, ids = env
    plugs = {p["name"]: p["device"] for p in c.put("/api/govee", json={"key": KEY}).json()["plugs"]}
    pid = ids["U1-C"]
    c.put(f"/api/printers/{pid}/plug", json={"device": plugs["U1-C"]})
    g.fail_control = True
    r = c.post(f"/api/printers/{pid}/power", json={"on": True})
    assert r.status_code == 502 and "device offline" in r.json()["detail"]
    assert c.get(f"/api/printers/{pid}/power").json()["log"][0]["ok"] is False


def test_refresh_reads_state_of_assigned_plugs(env):
    import asyncio
    c, g, m, ids = env
    plugs = {p["name"]: p["device"] for p in c.put("/api/govee", json={"key": KEY}).json()["plugs"]}
    c.put(f"/api/printers/{ids['P2']}/plug", json={"device": plugs["P1-2"]})
    g.power[plugs["P1-2"]] = 0                                               # turned off in the Govee app
    c.portal.call(m.plugs.refresh)
    printer = next(p for p in c.get("/api/printers").json() if p["id"] == ids["P2"])
    assert printer["power"]["on"] is False and printer["power"]["online"] is True


def _setup_x1(c, m, ids):
    plugs = {p["name"]: p["device"] for p in c.put("/api/govee", json={"key": KEY}).json()["plugs"]}
    c.put(f"/api/printers/{ids['X1C']}/plug", json={"device": plugs["X1"]})
    set_status(m, ids["X1C"], state=JobState.PRINTING, nozzle=220)
    return plugs


def _settle(g):
    """Wait until no Govee request has arrived for 3 x 0.1 s (background reads started earlier finish)."""
    n, quiet = -1, 0
    while quiet < 3:
        time.sleep(0.1)
        quiet = quiet + 1 if len(g.requests) == n else 0
        n = len(g.requests)


def test_polling_stops_at_budget_but_emergency_stop_still_works(env, monkeypatch):
    c, g, m, ids = env
    plugs = _setup_x1(c, m, ids)
    m.plugs.usage.sent = P.POLL_BUDGET                                       # our polling allowance used up
    _settle(g)                                                               # a read already in flight may land
    before = len(g.requests)
    c.portal.call(m.plugs.refresh)
    assert len(g.requests) == before                                         # no background reads at all
    r = c.post(f"/api/printers/{ids['X1C']}/power", json={"on": False, "confirm": "X1C"}).json()
    assert r == {"ok": True, "on": False} and g.controls == [(plugs["X1"], 0)]
    assert c.get("/api/govee").json()["usage"]["polling"] is False


def test_polling_stops_when_govee_says_few_left(env):
    c, g, m, ids = env
    plugs = _setup_x1(c, m, ids)
    g.remaining = P.RESERVE - 1
    set_status(m, ids["X1C"], conn=ConnState.OFFLINE)                       # only offline printers' plugs are read
    c.portal.call(m.plugs.refresh)                                           # this read learns "few left"
    n = len(g.requests)
    c.portal.call(m.plugs.refresh)
    assert len(g.requests) == n and m.plugs.usage.remaining == P.RESERVE - 1
    set_status(m, ids["X1C"], state=JobState.PRINTING, nozzle=220)
    assert c.post(f"/api/printers/{ids['X1C']}/power", json={"on": False, "confirm": "x1c"}).json()["ok"]


def test_429_pauses_polling_but_power_is_still_attempted(env):
    c, g, m, ids = env
    plugs = _setup_x1(c, m, ids)
    g.limit_reads = True
    set_status(m, ids["X1C"], conn=ConnState.OFFLINE)
    c.portal.call(m.plugs.refresh)                                           # 429 -> polling paused
    assert m.plugs.usage.limited_until > 0 and not m.plugs.usage.polling_allowed()
    n = len(g.requests)
    c.portal.call(m.plugs.refresh)
    assert len(g.requests) == n
    r = c.post(f"/api/printers/{ids['X1C']}/power", json={"on": False, "confirm": "X1C"})
    assert r.status_code == 200 and g.controls == [(plugs["X1"], 0)]       # control path never blocked by us
    assert r.json()["unconfirmed"] is True                                   # read-back refused: said so, not faked


def test_details_refresh_is_throttled(env):
    c, g, m, ids = env
    import time
    plugs = _setup_x1(c, m, ids)
    set_status(m, ids["X1C"], conn=ConnState.OFFLINE)
    dev = plugs["X1"]
    c.get(f"/api/printers/{ids['X1C']}/power")                               # first open (or the wake-up poll) reads it
    end = time.time() + 5
    while dev not in m.plugs._state_at and time.time() < end:
        time.sleep(0.05)
    time.sleep(0.3)
    n = len(g.requests)
    c.get(f"/api/printers/{ids['X1C']}/power"); c.get(f"/api/printers/{ids['X1C']}/power")
    assert len(g.requests) == n                                              # within a minute: no more reads
    m.plugs._state_at[dev] -= P.ON_DEMAND_GAP_S + 1
    c.get(f"/api/printers/{ids['X1C']}/power")
    assert len(g.requests) == n + 1


def test_power_command_not_blocked_by_a_saturated_default_pool(env):
    """Even with every default worker thread stuck (slow printers), the emergency stop goes out at once."""
    import threading
    import time
    c, g, m, ids = env
    plugs = _setup_x1(c, m, ids)
    gate = threading.Event()

    async def clog():
        import asyncio
        loop = asyncio.get_running_loop()
        for _ in range(64):
            loop.run_in_executor(None, gate.wait, 10)
    c.portal.call(clog)
    try:
        t0 = time.time()
        r = c.post(f"/api/printers/{ids['X1C']}/power", json={"on": False, "confirm": "X1C"})
        assert r.status_code == 200 and time.time() - t0 < 3 and g.controls == [(plugs["X1"], 0)]
    finally:
        gate.set()


def test_online_printers_plugs_are_not_polled(env):
    c, g, m, ids = env
    plugs = _setup_x1(c, m, ids)                                             # X1C online (printing)
    c.put(f"/api/printers/{ids['P2']}/plug", json={"device": plugs["P1-2"]})
    set_status(m, ids["P2"], conn=ConnState.OFFLINE)
    import time
    n, quiet = -1, 0                                                         # let the wake-up poll finish (was a
    while quiet < 3:                                                         # fixed 0.5 s sleep: flaky under load)
        time.sleep(0.2)
        quiet = quiet + 1 if len(g.requests) == n else 0
        n = len(g.requests)
    c.portal.call(m.plugs.refresh)
    new = g.requests[n:]
    assert new == ["/router/api/v1/device/state"]                            # only the offline printer's plug
    x1 = next(p for p in c.get("/api/printers").json() if p["id"] == ids["X1C"])["power"]
    # on=True either way; from_printer is only set if the first wake-up poll ran after the X1C came online
    # (a setup race, not product behaviour) - so only "on" is checked
    assert x1["on"] is True


def test_turn_on_cooldown_after_off(env):
    c, g, m, ids = env
    plugs = _setup_x1(c, m, ids)
    set_status(m, ids["X1C"])                                                # idle, cold
    c.post(f"/api/printers/{ids['X1C']}/power", json={"on": False})
    view = next(p for p in c.get("/api/printers").json() if p["id"] == ids["X1C"])["power"]
    assert 13 <= view["on_wait_s"] <= 15
    r = c.post(f"/api/printers/{ids['X1C']}/power", json={"on": True}).json()
    assert "wait_s" in r and g.controls == [(plugs["X1"], 0)]                # refused, nothing sent


def test_just_powered_off_printer_still_online_is_not_taken_as_plug_on(env):
    """Right after an emergency stop the printer can still look online for a while; the plug's real state wins."""
    c, g, m, ids = env
    plugs = _setup_x1(c, m, ids)                                             # X1C printing, online
    c.post(f"/api/printers/{ids['X1C']}/power", json={"on": False, "confirm": "X1C"})
    c.portal.call(m.plugs.refresh)                                           # printer status still says online
    view = next(p for p in c.get("/api/printers").json() if p["id"] == ids["X1C"])["power"]
    assert view["on"] is False and not view["from_printer"]


def test_command_accepted_but_ignored_is_resent_and_confirmed(env):
    """a real U1-C plug: Govee said OK to 'on' but the plug stayed off. Read back, re-send, confirm."""
    c, g, m, ids = env
    plugs = {p["name"]: p["device"] for p in c.put("/api/govee", json={"key": KEY}).json()["plugs"]}
    pid = ids["U1-C"]
    c.put(f"/api/printers/{pid}/plug", json={"device": plugs["U1-C"]})
    g.power[plugs["U1-C"]] = 0
    set_status(m, pid, conn=ConnState.OFFLINE)
    g.ignore_controls = 1
    r = c.post(f"/api/printers/{pid}/power", json={"on": True})
    assert r.status_code == 200 and r.json()["on"] is True
    assert g.controls == [(plugs["U1-C"], 1), (plugs["U1-C"], 1)] and g.power[plugs["U1-C"]] == 1
    assert c.get(f"/api/printers/{pid}/power").json()["log"][0]["note"] == "sent twice"


def test_plug_that_never_switches_is_reported_not_faked(env):
    c, g, m, ids = env
    plugs = {p["name"]: p["device"] for p in c.put("/api/govee", json={"key": KEY}).json()["plugs"]}
    pid = ids["U1-C"]
    c.put(f"/api/printers/{pid}/plug", json={"device": plugs["U1-C"]})
    set_status(m, pid)
    g.ignore_controls = 5
    r = c.post(f"/api/printers/{pid}/power", json={"on": False})
    assert r.status_code == 502 and "still reports on" in r.json()["detail"]
    view = next(p for p in c.get("/api/printers").json() if p["id"] == pid)["power"]
    assert view["on"] is True                                                # shows the truth, not what we asked for
    assert c.get(f"/api/printers/{pid}/power").json()["log"][0]["ok"] is False


def test_suggestions_give_each_plug_to_its_closest_printer():
    from app.plugs import suggest_plugs
    plugs = [{"device": d, "name": n} for d, n in [("d1", "A1"), ("d2", "X1"), ("d3", "U1-1"), ("d4", "P1-2")]]
    printers = [("a11", "A1-1"), ("am1", "A1 mini-1"), ("am2", "A1 mini-2"), ("x1c", "X1C"), ("u11", "U1-1"),
                ("p12", "P1-2"), ("cc", "Centauri Carbon")]
    got = suggest_plugs(printers, plugs, set())
    assert got == {"a11": "d1", "x1c": "d2", "u11": "d3", "p12": "d4"}         # not A1 for the A1 minis
    assert suggest_plugs(printers, plugs, {"d1"}).get("a11") is None           # a taken plug isn't suggested


def test_unplugged_plug_shows_offline_even_when_its_printer_is_online(env, monkeypatch):
    c, g, m, ids = env
    plugs = _setup_x1(c, m, ids)                       # X1C online; its plug is unplugged (printer on another outlet)
    g.offline.add(plugs["X1"])                         # Govee: offline, last known powerSwitch = 1
    m.plugs._state_at.clear(); m.plugs.states.clear()
    c.portal.call(m.plugs.refresh)                     # never read yet -> read even though the printer is online
    view = lambda: next(p for p in c.get("/api/printers").json() if p["id"] == ids["X1C"])["power"]
    assert view()["online"] is False and not view()["from_printer"]
    c.portal.call(m.plugs.refresh)                     # read recently: not read again, still offline (not "on")
    assert view()["online"] is False and not view()["from_printer"]
    assert next(p for p in c.get("/api/govee").json()["plugs"] if p["device"] == plugs["X1"])["online"] is False
    g.offline.clear()                                  # plugged back in: picked up at the next 30-minute read
    monkeypatch.setattr(P, "ONLINE_CHECK_S", 0)
    c.portal.call(m.plugs.refresh)
    assert view()["online"] is True and view()["on"] is True


def test_offline_plug_checked_on_assign_window_and_before_switching(env):
    c, g, m, ids = env
    plugs = {p["name"]: p["device"] for p in c.put("/api/govee", json={"key": KEY}).json()["plugs"]}
    g.offline.add(plugs["P1-1"])
    # Govee window opened: every plug on the account is read, assigned or not
    v = c.get("/api/govee?fresh=1").json()
    assert next(p for p in v["plugs"] if p["device"] == plugs["P1-1"])["online"] is False
    assert all(p["online"] is True for p in v["plugs"] if p["device"] != plugs["P1-1"])
    # assigning reads the plug right away, even though the printer is online
    pid = ids["P1"]
    set_status(m, pid)
    m.plugs._state_at.clear()
    r = c.put(f"/api/printers/{pid}/plug", json={"device": plugs["P1-1"]}).json()
    assert r["power"]["online"] is False and not r["power"]["from_printer"]
    # turning it on or off: checked first, nothing is sent to an offline plug
    for on in (False, True):
        r = c.post(f"/api/printers/{pid}/power", json={"on": on})
        assert r.status_code >= 400 and "offline" in r.json()["detail"]
    assert g.controls == []
    log = c.get(f"/api/printers/{pid}/power").json()["log"]
    assert log and not log[0]["ok"] and "offline" in log[0]["note"]
