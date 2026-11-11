"""Step-1 tests: class hierarchy, registry/catalog, SQLite store, lifecycle states."""
from __future__ import annotations

import asyncio

import pytest

from app.drivers import (AuthFailed, PrinterBase, PrinterBusy, PrinterUnreachable, all_drivers,
                         catalog, families, get_driver)
from app.drivers.bambu import BambuPrinter
from app.drivers.klipper import KlipperPrinter
from app.drivers.sdcp import SDCPPrinter
from app.models import ConnState, PrinterStatus
from app.store import DuplicatePrinter, PrinterConfig, PrinterStore


# --------------------------------------------------------------------------- hierarchy / registry
def test_hierarchy_matches_spec():
    assert issubclass(get_driver("bambu.x1c"), BambuPrinter)
    assert issubclass(get_driver("bambu.p1p"), BambuPrinter)
    assert issubclass(get_driver("bambu.a1"), BambuPrinter)
    assert issubclass(get_driver("bambu.a1mini"), BambuPrinter)
    assert issubclass(get_driver("snapmaker.u1"), KlipperPrinter)
    assert issubclass(get_driver("klipper.generic"), KlipperPrinter)
    assert issubclass(get_driver("elegoo.cc1"), SDCPPrinter)
    for fam in (BambuPrinter, KlipperPrinter, SDCPPrinter):
        assert issubclass(fam, PrinterBase)
        assert fam.abstract is True
    assert all(cls.abstract is False for cls in all_drivers().values())


def test_families_one_per_protocol():
    assert {f.family for f in families()} == {"bambu", "klipper", "sdcp"}


def test_catalog_for_add_form():
    cat = {m["maker"]: m["models"] for m in catalog()}
    assert {m["model"] for m in cat["Bambu Lab"]} == {"X1 Carbon", "P1P", "A1", "A1 Mini"}
    assert [m["model"] for m in cat["Snapmaker"]] == ["U1"]
    assert [m["model"] for m in cat["Elegoo"]] == ["Centauri Carbon"]
    bambu_fields = {f["key"]: f for f in cat["Bambu Lab"][0]["fields"]}
    assert bambu_fields["access_code"]["kind"] == "secret"
    assert bambu_fields["serial"]["required"] is False


def test_model_differences():
    x1, p1, a1 = get_driver("bambu.x1c"), get_driver("bambu.p1p"), get_driver("bambu.a1")
    assert x1.full_reports and not p1.full_reports
    assert x1.has_chamber_sensor and not p1.has_chamber_sensor
    assert not a1.has_aux_fan and p1.has_aux_fan
    assert get_driver("elegoo.cc1").mode == "poll"
    assert get_driver("snapmaker.u1").mode == "poll"          # Moonraker HTTP polling (spec O9)


def test_unknown_driver():
    with pytest.raises(KeyError):
        get_driver("nope.nope")


# --------------------------------------------------------------------------- store
@pytest.fixture
def store(tmp_path):
    s = PrinterStore(tmp_path / "dash.db")
    yield s
    s.close()


def test_store_add_list_remove(store):
    a = store.add("X1C", "bambu.x1c", "10.0.0.90", serial="00M099000000008",
                  secrets={"access_code": "x"})
    b = store.add("U1-A", "snapmaker.u1", "10.0.0.107", serial="8110000000000000TEST")
    assert [p.name for p in store.list()] == ["X1C", "U1-A"]
    assert store.get(a.id).secrets == {"access_code": "x"}
    assert store.remove(b.id) and not store.remove(b.id)
    assert [p.id for p in store.list()] == [a.id]


def test_store_duplicate_serial_rejected(store):
    store.add("X1C", "bambu.x1c", "10.0.0.90", serial="S1")
    with pytest.raises(DuplicatePrinter):
        store.add("X1C again", "bambu.x1c", "10.0.0.91", serial="S1")


def test_store_reorder_persists(store, tmp_path):
    ids = [store.add(n, "snapmaker.u1", h).id for n, h in
           [("U1-A", ".107"), ("U1-B", ".154"), ("U1-C", ".151")]]
    store.reorder([ids[2], ids[0], ids[1]])
    store.close()
    again = PrinterStore(tmp_path / "dash.db")
    assert [p.name for p in again.list()] == ["U1-C", "U1-A", "U1-B"]
    with pytest.raises(ValueError):
        again.reorder(ids[:2])
    again.close()


def test_store_find_for_discovery(store):
    p = store.add("A1-A", "bambu.a1", "10.0.0.142", serial="039199000000017")
    assert store.find("bambu.a1", serial="039199000000017").id == p.id
    assert store.find("bambu.a1", host="10.0.0.142").id == p.id
    assert store.find("bambu.a1", serial="other") is None


def test_public_view_hides_secrets(store):
    p = store.add("X1C", "bambu.x1c", "h", secrets={"access_code": "bbbb2222"})
    pub = p.to_public()
    assert "secrets" not in pub and pub["secrets_set"] == {"access_code": True}
    assert "bbbb2222" not in str(pub)


def test_update(store):
    p = store.add("CC", "elegoo.cc1", "10.0.0.202", settings={"poll_interval": 20})
    p2 = store.update(p.id, name="Centauri", settings={"poll_interval": 30})
    assert p2.name == "Centauri" and p2.settings["poll_interval"] == 30
    with pytest.raises(ValueError):
        store.update(p.id, driver="bambu.x1c")


# --------------------------------------------------------------------------- lifecycle
class _Script(PrinterBase):
    """Test driver whose behaviour is a script of outcomes."""
    family = "test"
    abstract = True
    backoff_steps = (0.01, 0.01)
    busy_retry = 0.01

    def __init__(self, cfg, outcomes, mode="live"):
        self.outcomes = list(outcomes)
        type(self).mode = mode
        super().__init__(cfg, self._record)
        self.seen: list[ConnState] = []

    def _record(self, st: PrinterStatus):
        if not self.seen or self.seen[-1] != st.connection:
            self.seen.append(st.connection)

    async def _next(self):
        if not self.outcomes:
            await asyncio.sleep(3600)
        o = self.outcomes.pop(0)
        if isinstance(o, Exception):
            raise o
        await self._set_conn(ConnState.ONLINE)
        await asyncio.sleep(o)

    async def _session(self):
        await self._next()

    async def _poll_once(self):
        await self._next()


def _cfg(**settings):
    return PrinterConfig(id="t1", name="t", driver="test", host="h", settings=settings)


def run(coro):
    return asyncio.run(coro)


def test_live_offline_then_online():
    async def go():
        d = _Script(_cfg(), [PrinterUnreachable("no route"), 0.05])
        d.start()
        await asyncio.sleep(0.2)
        await d.stop()
        return d.seen
    assert run(go())[:3] == [ConnState.CONNECTING, ConnState.OFFLINE, ConnState.ONLINE]


def test_auth_error_state_and_detail():
    async def go():
        d = _Script(_cfg(), [AuthFailed("wrong access code")])
        d.start()
        await asyncio.sleep(0.05)
        st = d.status.model_copy()
        await d.stop()
        return st
    st = run(go())
    assert st.connection == ConnState.AUTH_ERROR and st.connection_detail == "wrong access code"


def test_poll_busy_is_distinct_then_recovers():
    async def go():
        d = _Script(_cfg(poll_interval=0.01), [PrinterBusy("connection slots full"), 0, 0], mode="poll")
        d.start()
        await asyncio.sleep(0.2)
        await d.stop()
        return d.seen
    seen = run(go())
    assert ConnState.BUSY in seen
    assert seen.index(ConnState.BUSY) < seen.index(ConnState.ONLINE)


def test_unexpected_exception_does_not_kill_loop():
    async def go():
        d = _Script(_cfg(), [RuntimeError("boom"), 0.05])
        d.start()
        await asyncio.sleep(0.2)
        await d.stop()
        return d.seen
    seen = run(go())
    assert ConnState.ERROR in seen and ConnState.ONLINE in seen


def test_stop_is_prompt():
    async def go():
        d = _Script(_cfg(), [])
        d.start()
        await asyncio.sleep(0.02)
        await asyncio.wait_for(d.stop(), timeout=1)
    run(go())


def test_status_json_roundtrip():
    st = PrinterStatus(printer_id="p")
    assert PrinterStatus.model_validate_json(st.model_dump_json()).printer_id == "p"


def test_last_seen_only_moves_while_online():
    """Offline cards say "Last seen ...": the time the printer itself last reported, not when we gave up."""
    async def go():
        d = _Script(_cfg(), [0.05, PrinterUnreachable("gone")])
        d.start()
        await asyncio.sleep(0.03)
        seen_online = d.status.last_seen_at
        await asyncio.sleep(0.15)
        await d.stop()
        return seen_online, d.status
    seen_online, st = run(go())
    assert seen_online is not None and st.connection == ConnState.OFFLINE
    assert st.last_seen_at is not None and st.last_seen_at < st.updated_at     # froze when it dropped
