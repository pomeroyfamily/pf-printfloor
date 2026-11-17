"""Notifications: who gets what (defaults -> person -> printer), no repeats, restart safety, offline grace,
masked addresses, address checks and the API."""
import asyncio

import pytest
from fastapi.testclient import TestClient

from app import config, notify as N
from app.cloud import bambu as bc
from app.manager import Manager
from app.models import ConnState, Job, JobState, PrinterStatus
from app.web import create_app

NTFY = "ntfy://pf-test-8f3k2q9xa1"
NTFY2 = "ntfy://pf-other-zz99yy88"


class Store:                         # just the app-settings part of PrinterStore
    def __init__(self):
        self.s = {}

    def get_app_setting(self, k):
        import copy
        return copy.deepcopy(self.s.get(k))

    def set_app_setting(self, k, v):
        import copy
        self.s[k] = copy.deepcopy(v)


def notifier(store=None):
    n = N.Notifier(store or Store(), lambda pid: {"a1m2": "A1 mini-2", "x1c": "X1C"}.get(pid), lambda: "Shop")
    n.sent = []
    n._enqueue = lambda title, body, urls, event, pid: n.sent.append((event, pid, title, body, sorted(urls)))
    return n


def st(pid, state, conn=ConnState.ONLINE, job="Benchy", reason=None):
    return PrinterStatus(printer_id=pid, connection=conn, state=state, job=Job(name=job), reason=reason)


def people(n):
    n.save_person(None, "Danny", [{"url": NTFY}], {}, {"a1m2": {e: False for e in N.EVENTS}, "x1c": {"started": True}},
                  {"a1m2", "x1c"})
    n.save_person(None, "Bethany", [{"url": NTFY2}], {e: False for e in N.EVENTS},
                  {"a1m2": {"finished": True, "paused": True}}, {"a1m2", "x1c"})


def test_layers_defaults_person_printer():
    n = notifier()
    people(n)
    c = n.config()
    danny, beth = c["people"]
    # Danny: follows defaults, nothing from the A1 mini-2, plus "started" on the X1C only
    assert N.effective(c, danny, "x1c", "finished") and N.effective(c, danny, "x1c", "started")
    assert not any(N.effective(c, danny, "a1m2", e) for e in N.EVENTS)
    assert not N.effective(c, danny, "p1", "started")
    # Bethany: only finished + paused on the A1 mini-2
    assert N.effective(c, beth, "a1m2", "finished") and N.effective(c, beth, "a1m2", "paused")
    assert not N.effective(c, beth, "a1m2", "failed") and not N.effective(c, beth, "x1c", "finished")
    # changing a default reaches people who didn't change that box
    n.set_defaults({**N.DEFAULTS, "started": True})
    c = n.config()
    assert N.effective(c, c["people"][0], "p1", "started")         # Danny follows the default
    assert not N.effective(c, c["people"][1], "p1", "started")     # Bethany unticked it for herself


def test_routing_one_alert_per_change_and_no_repeats():
    n = notifier()
    people(n)
    n.observe(st("a1m2", JobState.PRINTING))                       # first sight: remembered, no alert
    n.on_job_event({"type": "job_started", "printer_id": "a1m2", "job": "Benchy"})
    n.observe(st("a1m2", JobState.PAUSED, reason="Filament ran out"))
    n.observe(st("a1m2", JobState.PAUSED, reason="Filament ran out"))    # same state again: nothing
    n.observe(st("a1m2", JobState.UNKNOWN))                             # in-between report: not a change
    n.observe(st("a1m2", JobState.PAUSED))
    n.observe(st("a1m2", JobState.PRINTING))
    n.observe(st("a1m2", JobState.PAUSED))                              # paused again after resuming: new alert
    n.on_job_event({"type": "job_ended", "printer_id": "a1m2", "result": "finished", "job": "Benchy",
                    "print_seconds": 4500})
    ev = [(e, sorted(u)) for e, _, _, _, u in n.sent]
    assert ev == [("paused", [NTFY2]), ("paused", [NTFY2]), ("finished", [NTFY2])]   # only Bethany, started off
    title, body = n.sent[0][2], n.sent[0][3]
    assert title == "A1 mini-2: Paused" and "Filament ran out" in body and "Benchy" in body and "Shop" in body
    assert "Print time 1h 15m" in n.sent[2][3]


def test_failed_cancelled_and_idle_error():
    n = notifier()
    people(n)
    n.on_job_event({"type": "job_ended", "printer_id": "x1c", "result": "cancelled", "job": "Gear"})
    n.on_job_event({"type": "job_ended", "printer_id": "x1c", "result": "failed", "job": "Gear"})
    n.observe(st("x1c", JobState.IDLE))
    n.observe(st("x1c", JobState.ERROR, reason="Nozzle clog"))           # error with no job running
    n.observe(st("x1c", JobState.PRINTING))
    n.observe(st("x1c", JobState.ERROR))                                 # during a job: reported by job_ended only
    assert [(e, t) for e, _, t, _, _ in n.sent] == [
        ("failed", "X1C: Cancelled"), ("failed", "X1C: Failed"), ("failed", "X1C: Failed")]
    assert "Nozzle clog" in n.sent[2][3]


def test_restart_does_not_resend_and_offline_grace(monkeypatch):
    store = Store()
    n = notifier(store)
    people(n)
    n.observe(st("x1c", JobState.PRINTING))
    n.observe(st("x1c", JobState.PAUSED))
    assert len(n.sent) == 1
    n2 = notifier(store)                                                 # dashboard restarted
    n2.observe(st("x1c", JobState.PAUSED))
    assert n2.sent == []
    n2.observe(st("x1c", JobState.PRINTING))
    clock = [1000.0]
    monkeypatch.setattr(N.time, "monotonic", lambda: clock[0])
    n2.observe(st("x1c", JobState.UNKNOWN, conn=ConnState.OFFLINE))
    clock[0] += 60
    n2.check_offline()
    assert n2.sent == []                                                 # a blip isn't reported
    n2.observe(st("x1c", JobState.PRINTING))                             # back within the grace time
    n2.observe(st("x1c", JobState.UNKNOWN, conn=ConnState.OFFLINE))
    clock[0] += N.OFFLINE_GRACE_S + 1
    n2.check_offline()
    n2.observe(st("x1c", JobState.UNKNOWN, conn=ConnState.OFFLINE))
    clock[0] += N.OFFLINE_GRACE_S + 1
    n2.check_offline()                                                   # still offline: no second alert
    assert [e for e, *_ in n2.sent] == ["offline"]
    # an idle printer going offline isn't an alert
    n2.observe(st("x1c", JobState.IDLE))
    n2.observe(st("x1c", JobState.IDLE, conn=ConnState.OFFLINE))
    clock[0] += N.OFFLINE_GRACE_S + 1
    n2.check_offline()
    assert [e for e, *_ in n2.sent] == ["offline"]


def test_address_checks_and_masking():
    assert N.check_url("") and N.check_url("http://example.com/x") and N.check_url("file:///etc/passwd")
    assert N.check_url("ntfy://") is not None                           # incomplete
    assert N.check_url(NTFY) is None and N.check_url("tgram://123456:ABCdef/987654") is None
    lbl = N.label_of(NTFY)
    assert "8f3k2q9" not in lbl and lbl.endswith("…q9xa1)".replace("q9xa1", "9xa1"))


def test_save_rules():
    n = notifier()
    with pytest.raises(ValueError):
        n.save_person(None, "  ", [], {}, {}, set())
    with pytest.raises(ValueError):
        n.save_person(None, "X", [{"url": "http://10.0.0.1/admin"}], {}, {}, set())
    p = n.save_person(None, "X", [{"url": NTFY}], {"bogus": True, "started": None}, {"gone": {"finished": True}}, {"x1c"})
    assert p["all"] == {} and p["printers"] == {}                       # unknown events / printers dropped
    with pytest.raises(ValueError):                                     # can't reference an address it doesn't have
        n.save_person(p["id"], "X", [{"id": "nope"}], {}, {}, set())
    keep = n.save_person(p["id"], "X2", [{"id": p["urls"][0]["id"]}], {}, {}, set())
    assert keep["urls"][0]["url"] == NTFY and keep["name"] == "X2"
    n.forget_printer("x1c")


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "THUMB_DIR", tmp_path / "thumbs")
    monkeypatch.setattr(Manager, "_start", lambda self, cfg: None)
    bc.set_account(None)
    with TestClient(create_app(tmp_path / "db.sqlite", start_drivers=False, ssdp_ports=())) as c:
        yield c


def test_api_never_returns_addresses(client, monkeypatch):
    sent = []

    async def fake_send(self, title, body, urls):
        sent.append((title, urls))
        return True, None
    monkeypatch.setattr(N.Notifier, "send_now", fake_send)
    v = client.post("/api/notify/people", json={"name": "Danny", "urls": [{"url": NTFY}], "all": {"started": True}}).json()
    assert NTFY not in str(v) and "8f3k2q9" not in str(v)
    pid, uid = v["people"][0]["id"], v["people"][0]["urls"][0]["id"]
    assert client.get("/api/notify").text.find("8f3k2q9") == -1
    r = client.post(f"/api/notify/people/{pid}/test", json={"url_id": uid}).json()
    assert r == {"ok": True, "error": None} and sent[0][1] == [NTFY]
    bad = client.post("/api/notify/people", json={"name": "Y", "urls": [{"url": "http://192.168.1.1/"}]})
    assert bad.status_code == 400
    assert client.put("/api/notify/people/nope", json={"name": "Z"}).status_code == 404
    v = client.put("/api/notify/defaults", json={"defaults": {"started": True, "bogus": True}}).json()
    assert v["defaults"]["started"] is True and "bogus" not in v["defaults"]
    assert client.delete(f"/api/notify/people/{pid}").json()["people"] == []


def test_manager_wiring_job_and_pause_reach_people(tmp_path):
    """Real Manager + job history: a print that starts, pauses and finishes alerts the right people once each."""
    from app.store import PrinterStore
    store = PrinterStore(tmp_path / "db.sqlite")
    cfg = store.add("X1C", "bambu.x1c", "10.0.0.2", "00M00A000000001")
    m = Manager(store)
    sent = []
    m.notifier._enqueue = lambda title, body, urls, event, pid: sent.append((event, title, urls))
    m.notifier.save_person(None, "Danny", [{"url": NTFY}], {"started": True}, {}, {cfg.id})

    def upd(state, job="Dragon.3mf", conn=ConnState.ONLINE):
        asyncio.run(m._on_update(PrinterStatus(printer_id=cfg.id, connection=conn, state=state,
                                               job=Job(name="Dragon", file_name=job, elapsed_s=10))))
    upd(JobState.IDLE, job=None)
    upd(JobState.PRINTING)
    upd(JobState.PRINTING)
    upd(JobState.PAUSED)
    upd(JobState.PRINTING)
    upd(JobState.FINISHED)
    upd(JobState.FINISHED)
    assert [(e, t) for e, t, _ in sent] == [("started", "X1C: Started"), ("paused", "X1C: Paused"),
                                            ("finished", "X1C: Finished")]
    asyncio.run(m.remove(cfg.id))                     # removing the printer forgets its state
    assert cfg.id not in (store.get_app_setting(N.SEEN_KEY) or {})
