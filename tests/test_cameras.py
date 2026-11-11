import time

import pytest
from fastapi.testclient import TestClient

from app import cameras as C
from app import config
from app.manager import Manager
from app.web import create_app
from tests.fakes.cameras import FakeBambuCamera, FakeHttpCameras
from tests.fakes.sdcp_printer import FakeCC
from tests.test_sdcp import DUMP


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "THUMB_DIR", tmp_path / "thumbs")
    monkeypatch.setattr(Manager, "_start", lambda self, cfg: None)
    monkeypatch.setattr(C, "LINGER_S", 1.0)
    monkeypatch.setattr(C, "LEASE_S", 1.0)
    with TestClient(create_app(tmp_path / "db.sqlite", start_drivers=False, ssdp_ports=())) as c:
        yield c


def add(client, name, driver, host="127.0.0.1", serial=None, settings=None, secrets=None):
    return client.app.state.mgr.store.add(name, driver, host, serial, settings or {}, secrets or {}).id


def snap(client, pid, after=0):
    return client.get(f"/api/printers/{pid}/camera.jpg", params={"after": after, "wait": 1.5})


def wait_until(fn, t=6.0):
    end = time.time() + t
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.1)
    return False


def size(img):
    from tools.camera_probe import jpeg_size  # same SOF parser the probe uses
    return jpeg_size(img)


def test_bambu_a1_camera_frames_then_stops_after_viewers_leave(client):
    cam = FakeBambuCamera("11112222", fps=10)
    try:
        pid = add(client, "A1-A", "bambu.a1", serial="039199000000017", settings={"camera_port": cam.port},
                  secrets={"access_code": "11112222"})
        assert client.get("/api/printers").json()[0]["camera"]["kind"] == "builtin"
        r = snap(client, pid)
        assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg" and size(r.content) == (1536, 1080)
        seq = int(r.headers["X-Frame-Seq"])
        r2 = snap(client, pid, after=seq)
        assert r2.status_code == 200 and int(r2.headers["X-Frame-Seq"]) > seq        # a newer frame
        assert cam.connections == 1                                                   # shared upstream
        assert wait_until(lambda: cam.open == 0, 8)                                   # lease + linger expired -> closed
    finally:
        cam.close()


def test_wrong_access_code_reports_error(client):
    cam = FakeBambuCamera("right")
    try:
        pid = add(client, "P1", "bambu.p1p", settings={"camera_port": cam.port}, secrets={"access_code": "wrong"})
        r = client.get(f"/api/printers/{pid}/camera.jpg")
        assert r.status_code == 503 and r.json()["detail"]
    finally:
        cam.close()


def test_u1_stock_wakes_camera_and_extended_firmware_is_detected(client):
    stock, ext = FakeHttpCameras(), FakeHttpCameras(extended=True)
    try:
        a = add(client, "U1-A", "snapmaker.u1", settings={"port": stock.port})
        r = snap(client, a)
        assert r.status_code == 200 and size(r.content) == (1920, 1080)
        assert stock.wakes and stock.wakes[0]["method"] == "camera.start_monitor" \
            and stock.wakes[0]["params"] == {"domain": "lan", "interval": 0}
        assert "stock" in client.get(f"/api/printers/{a}/camera").json()["label"]
        b = add(client, "U1-C", "snapmaker.u1", settings={"port": ext.port})
        assert snap(client, b).status_code == 200
        info = client.get(f"/api/printers/{b}/camera").json()
        assert "extended" in info["label"] and not ext.wakes and info["player_url"].endswith("/webcam/player")
    finally:
        stock.close(); ext.close()


def test_centauri_turns_stream_on_and_off(client):
    http = FakeHttpCameras()
    cc = FakeCC(DUMP, video_url=f"127.0.0.1:{http.port}/video")
    try:
        pid = add(client, "Centauri Carbon", "elegoo.cc1", serial=DUMP["mainboard_id"], settings=cc.settings())
        r = snap(client, pid)
        assert r.status_code == 200 and size(r.content) == (640, 360)
        assert cc.camera_calls[0] == 1
        assert wait_until(lambda: cc.camera_calls[-1] == 0, 10)                      # freed after viewers leave
        assert wait_until(lambda: http.mjpeg_clients == 0, 5)
    finally:
        cc.close(); http.close()


def test_external_url_overrides_and_x1c_has_none(client):
    http = FakeHttpCameras()
    try:
        x1c = add(client, "X1C", "bambu.x1c", serial="00M099000000008", secrets={"access_code": "x"})
        assert client.get("/api/printers").json()[0]["camera"]["kind"] is None
        assert client.get(f"/api/printers/{x1c}/camera.jpg").status_code == 404
        bad = client.patch(f"/api/printers/{x1c}", json={"camera_url": "rtsp://cam/stream"})
        assert bad.status_code == 400
        r = client.patch(f"/api/printers/{x1c}", json={"camera_url": f"http://admin:secret@127.0.0.1:{http.port}/mjpeg"})
        cam = r.json()["camera"]
        assert cam["kind"] == "external" and "secret" not in cam["url"] and "***" in cam["url"]
        # sending the masked URL back (e.g. saving the form unchanged) keeps the real one
        client.patch(f"/api/printers/{x1c}", json={"camera_url": cam["url"], "camera_kind": "auto"})
        assert client.app.state.mgr.store.get(x1c).settings["camera_url"].endswith("secret@127.0.0.1:%d/mjpeg" % http.port)
        r = snap(client, x1c)
        assert r.status_code == 200 and size(r.content) == (640, 360)
        assert "MJPEG" in client.get(f"/api/printers/{x1c}/camera").json()["label"]
        # snapshot URL
        client.patch(f"/api/printers/{x1c}", json={"camera_url": f"http://127.0.0.1:{http.port}/snap.jpg"})
        r = snap(client, x1c)
        assert r.status_code == 200 and size(r.content) == (800, 600)
        # clearing it -> back to no camera
        assert client.patch(f"/api/printers/{x1c}", json={"camera_url": ""}).json()["camera"]["kind"] is None
    finally:
        http.close()


def test_mjpeg_stream_real_server_viewer_counted_and_released(tmp_path, monkeypatch):
    """Real uvicorn: the popup's MJPEG stream counts as a viewer and is released when the browser disconnects."""
    import socket as so
    import threading
    import urllib.request

    import uvicorn
    monkeypatch.setattr(config, "THUMB_DIR", tmp_path / "thumbs")
    monkeypatch.setattr(Manager, "_start", lambda self, cfg: None)
    monkeypatch.setattr(C, "LINGER_S", 1.0)
    http = FakeHttpCameras()
    app = create_app(tmp_path / "db.sqlite", start_drivers=False, ssdp_ports=())
    s = so.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    try:
        assert wait_until(lambda: server.started, 10)
        mgr = app.state.mgr
        pid = mgr.store.add("Cam", "bambu.x1c", "127.0.0.1", None,
                            {"camera_url": f"http://127.0.0.1:{http.port}/snap.jpg"}, {}).id
        r = urllib.request.urlopen(f"http://127.0.0.1:{port}/api/printers/{pid}/camera.mjpg", timeout=10)
        assert r.headers["Content-Type"].startswith("multipart/x-mixed-replace")
        got = b""
        while got.count(b"\xff\xd9") < 2:
            got += r.read1(65536)
        assert got.startswith(b"--frame\r\nContent-Type: image/jpeg")
        feed = mgr.cameras.feeds[pid]
        assert feed.viewers == 1 and feed.running
        r.close()
        assert wait_until(lambda: feed.viewers == 0, 25)        # disconnect noticed (next frame or 15 s wait)
        assert wait_until(lambda: not feed.running, 8)          # upstream closed after linger
    finally:
        server.should_exit = True
        t.join(10)
        http.close()


def test_paxx_parallel_snapshots_beat_request_latency(client):
    """Paxx answers each snapshot in ~0.37 s: 3 requests in flight should give well over 1/0.37 = 2.7 frames/s,
    and frames must never go backwards."""
    ext = FakeHttpCameras(extended=True, ext_delay=0.37)
    try:
        pid = add(client, "U1-C", "snapmaker.u1", settings={"port": ext.port})
        r = snap(client, pid)
        assert r.status_code == 200
        feed = client.app.state.mgr.cameras.feeds[pid]
        seen, t0, last = [], time.time(), 0
        while time.time() - t0 < 3.0:
            r = snap(client, pid, after=last)
            if r.status_code == 200:
                last = int(r.headers["X-Frame-Seq"])
                seen.append(int(r.content[r.content.index(b"\xff\xfe") + 4:].split(b".")[0]))
        fps = feed.seq / (time.time() - t0 + 0.5)
        assert fps > 4.5, fps
        assert seen == sorted(seen)                                  # never backwards
    finally:
        ext.close()
