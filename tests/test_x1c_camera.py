"""X1C camera: RTSPS H.264 -> JPEG frames, against a fake printer (TLS, Digest auth, TCP-interleaved RTP)."""
import threading
import time

import pytest

from app.cameras import BambuRtsps, CameraUnavailable, RTSP_FPS, builtin_kind, source_for
from app.store import PrinterConfig

av = pytest.importorskip("av")            # in the Docker image; not installable on every Python the tests run on
from tests.fakes.x1c_rtsps import FakeX1cCamera  # noqa: E402

CODE = "ab/cd12"                          # a '/' must survive being put in the URL


def cfg(port, code=CODE):
    return PrinterConfig(id="x1", name="X1C", driver="bambu.x1c", host="127.0.0.1",
                         secrets={"access_code": code}, settings={"camera_port": port})


@pytest.fixture
def cam(tmp_path):
    c = FakeX1cCamera(tmp_path, CODE)
    yield c
    c.close()


def run_for(src, seconds):
    frames, errors, stop = [], [], threading.Event()

    def go():
        try:
            src.run(lambda b: frames.append((time.monotonic(), b)), stop)
        except Exception as e:  # noqa: BLE001
            errors.append(e)
    t = threading.Thread(target=go, daemon=True)
    t.start()
    time.sleep(seconds)
    stop.set()
    t.join(12)
    return frames, errors, t.is_alive()


def test_x1c_has_a_builtin_camera():
    c = cfg(322)
    assert isinstance(source_for(c), BambuRtsps) and builtin_kind(c) == "builtin"


def test_frames_are_jpegs_at_the_set_rate(cam):
    frames, errors, alive = run_for(BambuRtsps(cfg(cam.port)), 4)
    assert not errors and not alive                       # stops promptly when asked
    assert len(frames) >= 10
    assert all(b.startswith(b"\xff\xd8") and b.endswith(b"\xff\xd9") for _, b in frames)
    fps = (len(frames) - 1) / (frames[-1][0] - frames[0][0])
    assert RTSP_FPS * 0.7 <= fps <= RTSP_FPS * 1.2          # the fake sends 30 a second; we pass on ~RTSP_FPS


def test_wrong_access_code_is_named_and_not_leaked(cam):
    with pytest.raises(CameraUnavailable, match="refused the access code"):
        BambuRtsps(cfg(cam.port, "wrong-code")).run(lambda b: None, threading.Event())
    assert cam.auth_failures >= 1


def test_closed_port_says_turn_on_liveview(tmp_path):
    from tests.fakes.bambu_server import free_port
    with pytest.raises(CameraUnavailable, match="LAN Mode Liveview"):
        BambuRtsps(cfg(free_port())).run(lambda b: None, threading.Event())


def test_error_text_never_contains_the_access_code():
    src = BambuRtsps(cfg(322, "s3cr/et99"))
    msg = src._clean(Exception("Error opening 'rtsps://bblp:s3cr%2Fet99@10.0.0.5:322/streaming/live/1': s3cr/et99"))
    assert "s3cr" not in msg and "et99" not in msg and "10.0.0.5:322" in msg
