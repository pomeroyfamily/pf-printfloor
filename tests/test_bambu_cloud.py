import asyncio
import base64
import json

import pytest

from app.cloud import bambu as bc
from app.models import ConnState
from app.store import PrinterStore
from tests.fakes.bambu_cloud import FakeCloud
from tests.fakes.bambu_server import FakeBambu
from tests.test_bambu_session import fixture, png, run_driver
from app.transports import ftps

SERIAL = "01S099000000014"


def _cloud(fc: FakeCloud, token=None) -> bc.BambuCloud:
    return bc.BambuCloud(token, api_base=fc.base, web_base=fc.base)


@pytest.fixture
def fc():
    f = FakeCloud(tasks=[], covers={})
    yield f
    f.close()


def test_login_with_email_code(fc):
    c = _cloud(fc)
    with pytest.raises(bc.NeedsEmailCode):
        c.login(fc.email, fc.password)
    assert "POST /v1/user-service/user/sendemail/code" in fc.calls
    assert c.login_with_code(fc.email, " 123456 ") == fc.token


def test_login_direct_and_wrong_password(fc):
    fc.mode = "direct"
    assert _cloud(fc).login(fc.email, fc.password) == fc.token
    with pytest.raises(bc.CloudError, match="wrong password"):
        _cloud(fc).login(fc.email, "nope")


def test_login_tfa_uses_csrf_cookie_and_token_cookie(fc):
    fc.mode = "tfa"
    c = _cloud(fc)
    with pytest.raises(bc.NeedsTfa) as e:
        c.login(fc.email, fc.password)
    assert c.login_tfa(e.value.tfa_key, "123456") == fc.token
    assert any(x.startswith("GET /api/csrf") for x in fc.calls)


def test_find_task_by_id_then_by_name():
    tasks = [{"id": 1, "deviceId": SERIAL, "title": "A", "startTime": "2026-10-29T10:00:00Z"},
             {"id": 2, "deviceId": "OTHER", "title": "A", "startTime": "2026-10-29T11:00:00Z"},
             {"id": 3, "deviceId": SERIAL, "title": "A", "startTime": "2026-10-29T12:00:00Z"}]
    assert bc.find_task(tasks, SERIAL, "1", "A")["id"] == 1
    assert bc.find_task(tasks, SERIAL, "999", "A")["id"] == 3      # newest same-name on this printer
    assert bc.find_task(tasks, SERIAL, None, "B") is None


def test_token_expiry_from_jwt():
    payload = base64.urlsafe_b64encode(json.dumps({"exp": 1900000000}).encode()).decode().rstrip("=")
    assert bc.token_expiry(f"h.{payload}.s") == 1900000000
    assert bc.token_expiry("not-a-jwt") is None


def test_account_cover_and_expired_token(fc):
    img = png(30, 30, (1, 2, 3))
    fc.covers["c1.png"] = img
    fc.tasks = [{"id": 1000000003, "deviceId": SERIAL, "title": "Santas_Helper_LightFury",
                 "cover": fc.cover_url("c1.png")}]
    acct = bc.Account(_cloud(fc, fc.token), fc.email)
    data, how = acct.cover_for(SERIAL, "1000000003", "Santas_Helper_LightFury")
    assert data == img and "cover" in how
    fc.reject_token = True
    data, how = acct.cover_for(SERIAL, "1000000003", None)
    assert data is None and acct.state == "needs_login"


def test_store_link_roundtrip(tmp_path, fc):
    store = PrinterStore(tmp_path / "db.sqlite")
    assert bc.account_from_store(store) is None
    bc.save_link(store, fc.email, _cloud(fc, fc.token))
    acct = bc.account_from_store(store)
    assert acct and acct.email == fc.email and acct.cloud.token == fc.token
    assert all("tok" not in str(v) for p in store.list() for v in p.to_public().values())
    store.delete_app_setting("bambu_cloud")
    assert bc.account_from_store(store) is None
    store.close()


def test_driver_uses_cloud_cover_and_skips_ftp(tmp_path, monkeypatch, fc):
    img = png(40, 40, (9, 9, 200))
    fc.covers["c1.png"] = img
    fc.tasks = [{"id": 1000000003, "deviceId": SERIAL, "title": "Santas_Helper_LightFury",
                 "cover": fc.cover_url("c1.png")}]
    monkeypatch.setattr("app.config.THUMB_DIR", tmp_path / "thumbs")
    monkeypatch.setattr(ftps, "connect", lambda *a, **k: (_ for _ in ()).throw(AssertionError("FTP used")))
    bc.set_account(bc.Account(_cloud(fc, fc.token), fc.email))
    try:
        async def go():
            async with FakeBambu(tmp_path, fixture("p1p_fw011000_running_cloud.json"), serial=SERIAL) as srv:
                return (await run_driver(srv, "bambu.p1p", srv.code, 3.0))[1]
        seen = asyncio.run(go())
    finally:
        bc.set_account(None)
    last = [s for s in seen if s.connection == ConnState.ONLINE][-1]
    assert last.job.thumbnail_url and last.job.thumbnail_source == "cloud"
    assert (tmp_path / "thumbs" / "t1.png").read_bytes() == img


def test_driver_falls_back_to_printer_when_cloud_has_no_task(tmp_path, monkeypatch, fc):
    from app.drivers.bambu import BambuPrinter
    monkeypatch.setattr(BambuPrinter, "cloud_retry_s", 0.1)
    monkeypatch.setattr(BambuPrinter, "cloud_tries", 2)
    monkeypatch.setattr("app.config.THUMB_DIR", tmp_path / "thumbs")
    bc.set_account(bc.Account(_cloud(fc, fc.token), fc.email))
    try:
        async def go():
            async with FakeBambu(tmp_path, fixture("p1p_fw011000_running_cloud.json"), serial=SERIAL) as srv:
                from tests.test_bambu_session import make_3mf
                img = png(50, 50, (7, 7, 7))
                make_3mf(srv.ftp_root / "cache" / "Santas_Helper_LightFury.3mf", {1: img}, [1])
                return img, (await run_driver(srv, "bambu.p1p", srv.code, 4.0))[1]
        img, seen = asyncio.run(go())
    finally:
        bc.set_account(None)
    last = [s for s in seen if s.connection == ConnState.ONLINE][-1]
    assert last.job.thumbnail_source == "printer"
    assert (tmp_path / "thumbs" / "t1.png").read_bytes() == img


def test_device_lookup_works_right_after_boot(fc, monkeypatch):
    """The machine's monotonic clock starts near 0 at boot. An empty, never-fetched printer list must not count as
    fresh then (it did: cloud fallback was impossible for the first minute after boot)."""
    fc.devices = [{"dev_id": SERIAL, "online": True}]
    monkeypatch.setattr(bc.time, "monotonic", lambda: 5.0)
    acct = bc.Account(_cloud(fc, fc.token), fc.email)
    assert acct.device(SERIAL) == {"dev_id": SERIAL, "online": True}
