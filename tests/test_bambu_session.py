"""Bambu driver end-to-end against an in-process fake printer (MQTT/TLS + implicit FTPS)."""
from __future__ import annotations

import asyncio
import json
import os
import struct
import zipfile
import zlib
from pathlib import Path

import pytest

from app import config
from app.drivers import get_driver
from app.drivers.bambu import thumbnails
from app.models import ConnState, JobState, PrinterStatus
from app.store import PrinterConfig
from app.transports import ftps
from tests.fakes.bambu_server import FakeBambu

FIX = Path(__file__).parent / "fixtures" / "bambu"


def png(w: int, h: int, rgb=(255, 106, 19)) -> bytes:
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))
    ch = lambda t, d: struct.pack("!I", len(d)) + t + d + struct.pack("!I", zlib.crc32(t + d) & 0xFFFFFFFF)  # noqa: E731
    return (b"\x89PNG\r\n\x1a\n" + ch(b"IHDR", struct.pack("!IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + ch(b"IDAT", zlib.compress(raw)) + ch(b"IEND", b""))


class _Unseekable:
    """File wrapper without tell/seek: makes zipfile write data descriptors."""
    def __init__(self, f):
        self.f = f

    def write(self, b):
        return self.f.write(b)

    def flush(self):
        self.f.flush()


def make_3mf(path: Path, plates: dict[int, bytes], sliced: list[int], padding_mb: float = 0,
             images_first: bool = False, descriptors: bool = False, deflate: bool = False, gcode_mb: float = 0) -> None:
    fh = open(path, "wb")
    comp = zipfile.ZIP_DEFLATED if deflate else zipfile.ZIP_STORED
    with zipfile.ZipFile(_Unseekable(fh) if descriptors else fh, "w", compression=comp) as z:
        def images():
            for n, img in plates.items():
                z.writestr(f"Metadata/plate_{n}.png", img)
                z.writestr(f"Metadata/plate_{n}_small.png", png(4, 4))
        z.writestr("3D/3dmodel.model", "<model/>")
        if images_first:
            images()
        if padding_mb:   # big incompressible member, like a real 3MF's gcode
            z.writestr("3D/Objects/big.model", os.urandom(int(padding_mb * 1024 * 1024)))
        if not images_first:
            images()
        for n in sliced:
            z.writestr(f"Metadata/plate_{n}.gcode",
                       os.urandom(int(gcode_mb * 1024 * 1024)) if gcode_mb else "G28\n")
    fh.close()


def fixture(name: str) -> dict:
    return json.loads((FIX / name).read_text())


async def run_driver(server: FakeBambu, driver: str, code: str, seconds: float, **settings):
    seen: list[PrinterStatus] = []
    cfg = PrinterConfig(id="t1", name="t", driver=driver, host="127.0.0.1",
                        settings={"mqtt_port": server.mqtt_port, "ftps_port": server.ftps_port, **settings},
                        secrets={"access_code": code})
    d = get_driver(driver)(cfg, lambda s: seen.append(s.model_copy(deep=True)))
    d.start()
    await asyncio.sleep(seconds)
    await d.stop()
    return d, seen


@pytest.fixture(autouse=True)
def thumb_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "THUMB_DIR", tmp_path / "thumbs")


def test_p1p_cloud_session_status_and_thumbnail(tmp_path):
    async def go():
        fx = fixture("p1p_fw011000_running_cloud.json")
        async with FakeBambu(tmp_path, fx, deltas=[{"mc_percent": 78, "nozzle_temper": 250.5}]) as srv:
            make_3mf(srv.ftp_root / "cache" / "Santas_Helper_LightFury.3mf", {1: png(64, 64)}, [1])
            d, seen = await run_driver(srv, "bambu.p1p", srv.code, 2.5)
            return srv, d, seen
    srv, d, seen = asyncio.run(go())
    online = [s for s in seen if s.connection == ConnState.ONLINE]
    assert online, [s.connection for s in seen]
    last = online[-1]
    assert last.state == JobState.PRINTING and last.job.progress_pct == 78
    assert next(t for t in last.temps if t.key == "nozzle").current == 250.5
    assert last.job.thumbnail_url and last.job.thumbnail_url.startswith("/api/printers/t1/thumbnail")
    assert (config.THUMB_DIR / "t1.png").read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    # read-only: only pushall + get_version were ever sent
    cmds = {(list(r["payload"])[0], list(r["payload"].values())[0]["command"]) for r in srv.requests}
    assert cmds == {("pushing", "pushall"), ("info", "get_version")}
    assert d._serial == srv.serial                       # learned from the TLS certificate


def test_wrong_access_code_is_auth_error(tmp_path):
    async def go():
        async with FakeBambu(tmp_path, fixture("p1p_fw011000_running_cloud.json")) as srv:
            _, seen = await run_driver(srv, "bambu.p1p", "wrong", 2.0)   # paho loop_stop takes ~1 s
            return seen
    seen = asyncio.run(go())
    assert seen[-1].connection == ConnState.AUTH_ERROR
    assert seen[-1].connection_detail == "wrong access code"


def test_unreachable_is_offline():
    async def go():
        cfg = PrinterConfig(id="t2", name="t", driver="bambu.x1c", host="127.0.0.1",
                            serial="00M099000000008", settings={"mqtt_port": 1}, secrets={"access_code": "x"})
        seen = []
        d = get_driver("bambu.x1c")(cfg, lambda s: seen.append(s.connection))
        d.start()
        await asyncio.sleep(0.5)
        await d.stop()
        return seen
    assert ConnState.OFFLINE in asyncio.run(go())


def test_silent_printer_goes_offline(tmp_path):
    cls = get_driver("bambu.x1c")

    class QuickX1C(cls):
        full_reports = False
        pushall_min_interval = 0.2       # -> idle timeout = 90.2 s by default; patch below

    async def go():
        async with FakeBambu(tmp_path, fixture("x1c_fw011200_running_cloud.json"),
                             serial="00M099000000008") as srv:
            cfg = PrinterConfig(id="t3", name="t", driver="bambu.x1c", host="127.0.0.1",
                                settings={"mqtt_port": srv.mqtt_port}, secrets={"access_code": srv.code})
            seen = []
            d = QuickX1C(cfg, lambda s: seen.append(s.connection))
            # shrink the silence watchdog for the test
            import app.transports.mqtt as m
            orig = m.MqttConnection.messages

            def fast(self, idle_timeout):
                return orig(self, 0.6)
            m.MqttConnection.messages = fast
            try:
                d.start()
                await asyncio.sleep(1.5)
                await d.stop()
            finally:
                m.MqttConnection.messages = orig
            return seen
    seen = asyncio.run(go())
    assert ConnState.ONLINE in seen and ConnState.OFFLINE in seen
    assert seen.index(ConnState.ONLINE) < seen.index(ConnState.OFFLINE)


# --------------------------------------------------------------------------- thumbnails
def test_exact_gcode_file_beats_other_plate_file():
    listing = {"/": [], "/cache": ["Halloween+Flexi.stl_plate_3.3mf", "Halloween+Flexi.stl_plate_5.3mf"]}
    got = thumbnails.find_job_file(listing, "Halloween+Flexi.stl_plate_5.3mf",
                                   "Halloween+Flexi.stl_plate_5.3mf", prefer_cache=False)
    assert got == "/cache/Halloween+Flexi.stl_plate_5.3mf"   # the A1 bug from 2026-10-29


def test_find_local_root_and_cloud_cache_and_truncated():
    listing = {"/": ["plate_cleaner_petg.gcode.3mf",
                     "Cube + Cube + Cube + Cube + Cube + Cube + Cube +....gcode.3mf"],
               "/cache": ["Santas_Helper_LightFury.3mf"]}
    assert thumbnails.find_job_file(listing, "plate_cleaner_petg.gcode.3mf", "plate_cleaner_petg.gcode.3mf",
                                    False) == "/plate_cleaner_petg.gcode.3mf"
    assert thumbnails.find_job_file(listing, "Santas_Helper_LightFury.3mf", "Santas_Helper_LightFury",
                                    True) == "/cache/Santas_Helper_LightFury.3mf"
    long = "Cube + Cube + Cube + Cube + Cube + Cube + Cube + Cube + Cube"
    assert thumbnails.find_job_file(listing, None, long, False).endswith("....gcode.3mf")
    assert thumbnails.find_job_file(listing, "/data/Metadata/plate_1.gcode", "Nothing", True) is None


def test_pick_plate_uses_sliced_gcode():
    names = ["Metadata/plate_1.png", "Metadata/plate_2.png", "Metadata/plate_3.png", "Metadata/plate_3.gcode"]
    assert thumbnails.pick_plate_image(names, plate_hint=None) == "Metadata/plate_3.png"
    assert thumbnails.pick_plate_image(names[:3], plate_hint=2) == "Metadata/plate_2.png"
    assert thumbnails.pick_plate_image(names[:3], plate_hint=None) == "Metadata/plate_1.png"


def test_ranged_read_avoids_full_download(tmp_path, monkeypatch):
    async def go():
        async with FakeBambu(tmp_path, fixture("p1p_fw011000_running_cloud.json")) as srv:
            img = png(80, 80, (10, 200, 30))
            make_3mf(srv.ftp_root / "cache" / "Big.3mf", {1: png(8, 8), 2: img}, [2], padding_mb=4)

            def no_full(*a, **k):
                raise AssertionError("full download used")
            monkeypatch.setattr(ftps, "download", no_full)
            data, how = await asyncio.to_thread(thumbnails.fetch_thumbnail, "127.0.0.1", srv.code,
                                                "Big.3mf", "Big", None, True, True, srv.ftps_port)
            return img, data, how
    img, data, how = asyncio.run(go())
    assert data == img and "(ranged)" in how and "plate_2.png" in how


def test_ranged_disabled_streams(tmp_path):
    async def go():
        async with FakeBambu(tmp_path, fixture("p1p_fw011000_running_cloud.json")) as srv:
            img = png(20, 20)
            make_3mf(srv.ftp_root / "Local.gcode.3mf", {1: img}, [1])
            return img, await asyncio.to_thread(thumbnails.fetch_thumbnail, "127.0.0.1", srv.code,
                                                "Local.gcode.3mf", "Local.gcode.3mf", None, False,
                                                False, srv.ftps_port)
    img, (data, how) = asyncio.run(go())
    assert data == img and "(stream" in how


def _fetch_no_rest(tmp_path, monkeypatch, plate_hint, **mk):
    async def go():
        async with FakeBambu(tmp_path, fixture("p1p_fw011000_running_cloud.json"), no_rest=True) as srv:
            img = png(80, 80, (200, 30, 30))
            make_3mf(srv.ftp_root / "cache" / "Big.3mf", {1: png(8, 8), 2: img}, [2], padding_mb=4, **mk)
            reads = []
            real = ftps.stream_zip_members

            def spy(*a, **k):
                r = real(*a, **k)
                reads.append(r[2])
                return r
            monkeypatch.setattr(ftps, "stream_zip_members", spy)
            data, how = await asyncio.to_thread(thumbnails.fetch_thumbnail, "127.0.0.1", srv.code,
                                                "Big.3mf", "Big", plate_hint, True, True, srv.ftps_port)
            return img, data, how, reads
    return asyncio.run(go())


def test_no_rest_streams_and_stops_early(tmp_path, monkeypatch):
    monkeypatch.setattr(ftps, "download", lambda *a, **k: (_ for _ in ()).throw(AssertionError("full download")))
    img, data, how, reads = _fetch_no_rest(tmp_path, monkeypatch, plate_hint=2, images_first=True)
    assert data == img and "(stream" in how and "plate_2.png" in how
    assert reads and reads[0] < 1024 * 1024          # hung up long before the 4 MB padding ended


def test_no_rest_stream_finds_sliced_plate_without_hint(tmp_path, monkeypatch):
    monkeypatch.setattr(ftps, "download", lambda *a, **k: (_ for _ in ()).throw(AssertionError("full download")))
    img, data, how, _ = _fetch_no_rest(tmp_path, monkeypatch, plate_hint=None)
    assert data == img and "(stream" in how and "plate_2.png" in how


def test_no_rest_stored_with_descriptors_streams(tmp_path, monkeypatch):
    """P1P cloud job: plate PNGs are stored (not deflated) with data descriptors (2026-10-29)."""
    monkeypatch.setattr(ftps, "download", lambda *a, **k: (_ for _ in ()).throw(AssertionError("full download")))
    img, data, how, reads = _fetch_no_rest(tmp_path, monkeypatch, plate_hint=2, images_first=True,
                                           descriptors=True)
    assert data == img and "(stream" in how
    assert reads[0] < 1024 * 1024


def test_stored_descriptor_with_fake_signature_inside(tmp_path):
    """Data that happens to contain PK\\x07\\x08 must not end the entry early."""
    body = b"\x89PNG" + b"PK\x07\x08" + b"\x00" * 40 + b"PK\x07\x08junk" + b"IEND"
    async def go():
        async with FakeBambu(tmp_path, fixture("p1p_fw011000_running_cloud.json"), no_rest=True) as srv:
            fh = open(srv.ftp_root / "x.3mf", "wb")
            with zipfile.ZipFile(_Unseekable(fh), "w") as z:
                z.writestr("Metadata/plate_1.png", body)
                z.writestr("Metadata/plate_1.gcode", "G28")
            fh.close()

            def run():
                ftp = ftps.connect("127.0.0.1", "bblp", srv.code, port=srv.ftps_port)
                try:
                    return ftps.stream_zip_members(ftp, "/x.3mf", lambda n: n.endswith(".png"),
                                                   lambda k, n: False)
                finally:
                    ftp.close()
            return await asyncio.to_thread(run)
    kept, names, _ = asyncio.run(go())
    assert kept["Metadata/plate_1.png"] == body and names == ["Metadata/plate_1.png", "Metadata/plate_1.gcode"]


def test_no_rest_deflated_descriptors_stream(tmp_path, monkeypatch):
    """Bambu Studio writes deflated entries with data descriptors (P1P, 2026-10-29)."""
    monkeypatch.setattr(ftps, "download", lambda *a, **k: (_ for _ in ()).throw(AssertionError("full download")))
    img, data, how, reads = _fetch_no_rest(tmp_path, monkeypatch, plate_hint=2, images_first=True,
                                           descriptors=True, deflate=True)
    assert data == img and "(stream" in how
    assert reads[0] < 1024 * 1024


def test_no_rest_deflated_descriptors_images_last(tmp_path, monkeypatch):
    monkeypatch.setattr(ftps, "download", lambda *a, **k: (_ for _ in ()).throw(AssertionError("full download")))
    img, data, how, _ = _fetch_no_rest(tmp_path, monkeypatch, plate_hint=None, descriptors=True, deflate=True)
    assert data == img and "(stream" in how and "plate_2.png" in how


def test_no_rest_real_p1p_layout_stops_at_gcode_header(tmp_path, monkeypatch):
    """Layout seen on the P1P (2026-10-29): plate images first, then a ~29 MB plate_1.gcode.
    No plate hint (P1P sends no plate_idx): stop once the gcode header names the plate."""
    monkeypatch.setattr(ftps, "download", lambda *a, **k: (_ for _ in ()).throw(AssertionError("full download")))

    async def go():
        async with FakeBambu(tmp_path, fixture("p1p_fw011000_running_cloud.json"), no_rest=True) as srv:
            img = png(80, 80, (5, 90, 5))
            make_3mf(srv.ftp_root / "cache" / "Big.3mf", {1: img}, [1], images_first=True,
                     descriptors=True, deflate=True, gcode_mb=4)
            reads = []
            real = ftps.stream_zip_members

            def spy(*a, **k):
                r = real(*a, **k)
                reads.append(r[2])
                return r
            monkeypatch.setattr(ftps, "stream_zip_members", spy)
            data, how = await asyncio.to_thread(thumbnails.fetch_thumbnail, "127.0.0.1", srv.code,
                                                "Big.3mf", "Big", None, True, True, srv.ftps_port)
            return img, data, how, reads
    img, data, how, reads = asyncio.run(go())
    assert data == img and "(stream" in how
    assert reads[0] < 1024 * 1024
