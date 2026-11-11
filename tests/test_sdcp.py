import asyncio
import json
from pathlib import Path

import pytest

from app import config
from app.drivers import get_driver
from app.drivers.sdcp import parse
from app.models import ConnState, JobState, PrinterStatus
from app.store import PrinterConfig
from tests.fakes.sdcp_printer import FakeCC
from tests.test_bambu_session import png

DUMP = json.loads((Path(__file__).parent / "fixtures/sdcp/cc1_fw1449_printing.json").read_text())
TASK = DUMP["status"]["PrintInfo"]["TaskId"]


@pytest.fixture(autouse=True)
def thumb_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "THUMB_DIR", tmp_path / "thumbs")


def st(status=None, canvas=None):
    return parse.to_status("cc", status or DUMP["status"], DUMP["attributes"], canvas)


# ---------------------------------------------------------------------- parse (a real dump)
def test_printing_from_real_dump():
    s = st()
    assert s.state == JobState.PRINTING and s.device_name == "Centauri Carbon" and s.firmware == "V1.4.49"
    j = s.job
    assert j.name.startswith("ECC_0.4_elegooLight Fury Dragons") and not j.name.endswith(".gcode")
    assert (j.progress_pct, j.layer, j.total_layers) == (15.0, 11, 143)
    assert j.elapsed_s == 5430 and j.remaining_s == int(33477 - 5430.345368048002) and j.eta   # ticks = seconds
    t = {x.key: x for x in s.temps}
    assert round(t["nozzle"].current) == 258 and t["nozzle"].target == 255 and round(t["chamber"].current) == 31
    assert t["chamber"].target is None                                         # TempTargetBox 0 = no target
    assert {f.label: f.percent for f in s.fans} == {"Part cooling": 20, "Aux": 0, "Chamber": 68}
    assert s.errors == [] and s.filament == []


def test_mid_print_substate_keeps_printing():
    status = json.loads(json.dumps(DUMP["status"]))
    status["PrintInfo"]["Status"] = 20                   # seen at 43% on the real printer
    s = st(status)
    assert s.state == JobState.PRINTING and s.job.sub_state == "preheat done"


def test_finished_paused_idle_error():
    def with_(cs, ps, **pi):
        status = json.loads(json.dumps(DUMP["status"]))
        status["CurrentStatus"] = [cs]
        status["PrintInfo"].update(Status=ps, **pi)
        return st(status)
    assert with_(0, 9).state == JobState.FINISHED and with_(0, 9).job.progress_pct == 100
    assert with_(0, 8).state == JobState.CANCELLED
    assert with_(1, 6).state == JobState.PAUSED
    assert with_(0, 0, Filename="").state == JobState.IDLE
    e = with_(1, 14, ErrorNumber=3)
    assert e.state == JobState.ERROR and e.errors[0].code == "E3"
    assert with_(1, 16, CurrentLayer=0).state == JobState.PREPARING


def test_canvas_units():
    from tests.fakes.sdcp_printer import CANVAS
    (u,) = st(canvas=CANVAS).filament
    assert u.kind == "Canvas" and len(u.slots) == 4
    assert [x.present for x in u.slots] == [True, True, False, True]
    assert u.slots[1].active and u.slots[1].color_hex == "#E72F1D" and u.slots[1].brand == "ELEGOO"
    assert u.slots[0].material == "PETG" and u.slots[0].sub_type == "PETG Pro"


# ---------------------------------------------------------------------- driver against the fake CC
async def run(cc: FakeCC, seconds=1.2, serial=None, **settings):
    seen: list[PrinterStatus] = []
    cfg = PrinterConfig(id="cc1", name="cc", driver="elegoo.cc1", host="127.0.0.1", serial=serial,
                        settings={**cc.settings(), **settings})
    d = get_driver("elegoo.cc1")(cfg, lambda s: seen.append(s.model_copy(deep=True)))
    d.start()
    await asyncio.sleep(seconds)
    await d.stop()
    return d, seen


def test_driver_polls_discovers_and_gets_thumbnail():
    img = png(64, 64, (231, 47, 29))
    cc = FakeCC(DUMP, thumbnails={f"/board-resource/history_image/{TASK}.png": img})
    try:
        d, seen = asyncio.run(run(cc))
    finally:
        cc.close()
    online = [s for s in seen if s.connection == ConnState.ONLINE]
    assert online and online[-1].job.progress_pct == 15.0 and online[-1].device_name == "Centauri Carbon"
    assert d.learned_serial == DUMP["mainboard_id"]                     # learned via UDP M99999
    assert online[-1].job.thumbnail_url and (config.THUMB_DIR / "cc1.png").read_bytes() == img
    assert cc.connections >= 2                                           # connect -> read -> disconnect per poll
    assert set(cc.cmds) <= {0, 1, 324}                                   # printing: status + Canvas only


def test_busy_when_slots_full():
    cc = FakeCC(DUMP, busy=True)
    try:
        _, seen = asyncio.run(run(cc, 0.8, serial=DUMP["mainboard_id"]))
    finally:
        cc.close()
    assert seen[-1].connection == ConnState.BUSY and "slots" in seen[-1].connection_detail


def test_canvas_read_only_when_idle_and_cached():
    cc = FakeCC(DUMP, idle=True)
    try:
        _, seen = asyncio.run(run(cc, 1.5, serial=DUMP["mainboard_id"]))
    finally:
        cc.close()
    assert cc.cmds.count(324) == 1                                       # once, then cached
    last = seen[-1]
    assert last.state == JobState.FINISHED and last.filament and last.filament[0].kind == "Canvas"
    assert not any(s.active for s in last.filament[0].slots)             # not printing: none active


def test_canvas_stops_if_printer_drops_connection():
    cc = FakeCC(DUMP, idle=True, canvas="close")
    try:
        d, seen = asyncio.run(run(cc, 2.0, serial=DUMP["mainboard_id"]))
    finally:
        cc.close()
    assert cc.cmds.count(324) == 1 and d._canvas_off                     # asked once, never again
    assert seen[-1].connection == ConnState.ONLINE                       # dashboard keeps working


def test_unreachable_is_offline():
    cc = FakeCC(DUMP)
    s = cc.settings()
    cc.close()
    _, seen = asyncio.run(run(type("X", (), {"settings": lambda self: s})(), 0.8, serial="x"))
    assert seen[-1].connection == ConnState.OFFLINE


def test_discovery_reply_to_row():
    from app.discovery import sdcp_scan
    row = sdcp_scan.to_row(DUMP["discovery"]["reply"], "10.0.0.202")            # a real reply
    assert (row["name"], row["host"], row["serial"], row["driver"], row["firmware"]) == \
        ("Centauri Carbon", "10.0.0.202", "0123456789abcdef0000000000000000", "elegoo.cc1", "V1.4.49")
    other = sdcp_scan.to_row({"Data": {"Name": "Saturn 4", "MachineName": "Saturn 4 Ultra", "MainboardID": "ab"}}, "1.2.3.4")
    assert other["driver"] == "elegoo.unsupported" and other["host"] == "1.2.3.4"


def test_file_transfer_shows_as_receiving():
    from app.drivers.sdcp import parse
    st = parse.to_status("cc", {"CurrentStatus": [2], "PrintInfo": {"Status": 0}}, {})
    assert st.state.value == "preparing" and st.job.transferring and st.job.sub_state == "receiving file"


def test_real_canvas_reply_idle_after_finish():
    """a real CC 2026-10-30 07:22: idle after a finished print, Canvas answered Cmd 324 (connected = 1, not true)."""
    import json
    from pathlib import Path
    from app.drivers.sdcp import parse
    d = json.loads((Path(__file__).parent / "fixtures/sdcp/cc1_fw1449_idle_canvas.json").read_text())
    st = parse.to_status("cc", d["status"], d["attributes"], canvas=d["responses"]["324"])
    assert st.state.value == "finished"
    (unit,) = st.filament
    assert unit.kind == "Canvas"
    assert [(s.color_hex, s.material, s.brand, s.present, s.active) for s in unit.slots] == [
        ("#000000", "PETG", "Generic", True, False), ("#FFFFFF", "PETG", "Generic", True, False),
        ("#48A7FA", "PETG", "Generic", True, False), ("#F72221", "PETG", "Generic", True, False)]
    off = parse.canvas_units({"canvas_list": [{"canvas_id": 0, "connected": 0, "tray_list": [{"filament_type": "PLA"}]}]}, False)
    assert off == []


IDLE_DUMP = json.loads((Path(__file__).parent / "fixtures/sdcp/cc1_fw1449_idle_canvas.json").read_text())
REAL_HISTORY = IDLE_DUMP["responses"]["321"]["HistoryDetailList"]          # a real CC, 3 finished jobs


def test_history_items_from_real_cmd321():
    items = parse.history_items(REAL_HISTORY + [{"TaskId": "running", "BeginTime": 5, "EndTime": 0}])
    assert len(items) == 3 and {i["result"] for i in items} == {"finished"}
    first = items[0]
    assert first["external_id"] == "bcd3e4e6-4543-4562-814a-e6359bf81265"
    assert first["job_name"].startswith("ECC_0.4_elegooLight Fury Dragons") and not first["job_name"].endswith(".gcode")
    assert round(first["print_seconds"]) == 31083                          # slicer-side print_time, 8h 38m
    assert parse.history_items([{**REAL_HISTORY[0], "TaskStatus": 3}])[0]["result"] == "cancelled"
    assert parse.history_items([{**REAL_HISTORY[0], "ErrorStatusReason": 5}])[0]["result"] == "failed"


def test_driver_reads_history_only_when_idle_and_only_new_ids():
    cc = FakeCC(DUMP, idle=True, history=REAL_HISTORY)
    try:
        d, _ = asyncio.run(run(cc, 1.5, serial=DUMP["mainboard_id"]))
    finally:
        cc.close()
    assert cc.cmds.count(320) == 1 and cc.requested_ids == [[h["TaskId"] for h in REAL_HISTORY]]
    assert len(d.pending_jobs) == 3 and len(d._hist_seen) == 3          # handed to the manager; ids remembered


def test_driver_no_history_while_printing():
    cc = FakeCC(DUMP, history=REAL_HISTORY)
    try:
        d, _ = asyncio.run(run(cc, 1.0, serial=DUMP["mainboard_id"]))
    finally:
        cc.close()
    assert 320 not in cc.cmds and 321 not in cc.cmds and not d.pending_jobs



# a real CC mid-print 2026-10-30 14:12 (cc_probe --canvas-while-printing): printing from the Canvas, black tray 0
MIDPRINT_CANVAS = {"Ack": 0, "active_canvas_id": 0, "active_tray_id": 0, "auto_refill": 1, "canvas_list": [
    {"canvas_id": 0, "connected": 1, "tray_list": [
        {"tray_id": i, "brand": "Generic", "filament_type": "PETG", "filament_name": "PETG", "filament_code": "0x00000",
         "filament_color": c, "min_nozzle_temp": 230, "max_nozzle_temp": 260, "status": st}
        for i, (c, st) in enumerate([("#000000", 2), ("#FFFFFF", 0), ("#48A7FA", 0), ("#F72221", 0)])]}]}


def test_canvas_read_mid_print_highlights_the_tray_in_use():
    cc = FakeCC(DUMP, canvas_reply=MIDPRINT_CANVAS)                     # printing
    try:
        _, seen = asyncio.run(run(cc, 1.5, serial=DUMP["mainboard_id"]))
    finally:
        cc.close()
    assert cc.cmds.count(324) == 1                                       # once, then every 2 min
    last = seen[-1]
    assert last.state == JobState.PRINTING
    slots = last.filament[0].slots
    assert [s.active for s in slots] == [True, False, False, False]      # black in use
    assert [s.present for s in slots] == [True, False, False, False]     # the test printer had unloaded trays 1-3 (status 0)
    assert slots[0].color_hex == "#000000" and slots[1].color_hex is None  # unloaded: no stale color shown


def test_canvas_while_printing_can_be_switched_off():
    cc = FakeCC(DUMP, canvas_reply=MIDPRINT_CANVAS)
    try:
        asyncio.run(run(cc, 1.2, serial=DUMP["mainboard_id"], canvas_while_printing=False))
    finally:
        cc.close()
    assert 324 not in cc.cmds
