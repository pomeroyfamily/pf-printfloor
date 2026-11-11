"""Bambu parser against real dumps from real printers (tests/fixtures/bambu)."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.drivers import get_driver
from app.drivers.bambu.parse import clean_job_name, merge_report, plate_from_name, to_status
from app.models import JobState

FIX = Path(__file__).parent / "fixtures" / "bambu"
NOW = datetime(2026, 10, 29, 16, 0, tzinfo=timezone.utc)


def load(name: str, driver: str):
    d = json.loads((FIX / name).read_text())
    cls = get_driver(driver)
    st = to_status("p", d["merged_state"], d["version"], has_chamber_sensor=cls.has_chamber_sensor,
                   has_aux_fan=cls.has_aux_fan, has_chamber_fan=cls.has_chamber_fan, now=NOW)
    return st, d


def temp(st, key):
    return next(t for t in st.temps if t.key == key)


def fan(st, key):
    return next(f for f in st.fans if f.key == key)


def unit(st, uid):
    return next(u for u in st.filament if u.id == uid)


# --------------------------------------------------------------------------- X1C fw 01.12
def test_x1c_job_and_temps():
    st, _ = load("x1c_fw011200_running_cloud.json", "bambu.x1c")
    assert st.state == JobState.PRINTING
    assert st.firmware == "01.12.00.00"
    assert st.job.name == "Night Fury 2026-Toothless tail-ALL VERSIONS-MULTICOLOR-U1_plate_1"
    assert st.job.progress_pct == 31 and (st.job.layer, st.job.total_layers) == (23, 147)
    assert (st.job.plate, st.job.plate_count) == (1, 12)
    assert st.job.remaining_s == 520 * 60
    assert temp(st, "nozzle").target == 255
    assert temp(st, "chamber").current == 34          # device.ctc.info.temp (chamber_temper removed)
    assert st.job.file_name == st.job.name             # gcode_file is internal /data/... -> use subtask


def test_x1c_ams_units_and_kinds():
    st, _ = load("x1c_fw011200_running_cloud.json", "bambu.x1c")
    assert [u.kind for u in st.filament] == ["AMS 2 Pro", "AMS", "External spool"]
    ams2 = unit(st, "ams1")
    hf = ams2.slots[2]
    assert (hf.material, hf.sub_type, hf.color_hex, hf.brand, hf.rfid) == (
        "PETG", "PETG HF", "#00AE42", "Bambu Lab", True)
    assert hf.remaining_pct is None                    # printer reports 0 -> unknown
    assert ams2.slots[0].active and ams2.slots[0].color_hex == "#161616"   # tray_now = 4
    assert unit(st, "ams0").slots[0].present is False
    assert unit(st, "ams0").humidity_pct == 26
    assert unit(st, "external").slots[0].present is False


def test_x1c_hms_code():
    st, _ = load("x1c_fw011200_running_cloud.json", "bambu.x1c")
    assert [e.code for e in st.errors] == ["HMS_0300_1100_0002_0001"]


# --------------------------------------------------------------------------- P1P fw 01.10
def test_p1p_no_chamber_sensor_and_fans():
    st, d = load("p1p_fw011000_running_cloud.json", "bambu.p1p")
    assert d["merged_state"]["chamber_temper"] == 5    # placeholder value on P1P
    assert temp(st, "chamber").current is None
    assert fan(st, "part").percent == 53 and fan(st, "chamber").percent == 73
    assert st.job.name == "Santas_Helper_LightFury" and st.job.file_name == "Santas_Helper_LightFury.3mf"
    assert st.job.eta is not None


def test_p1p_untagged_remaining_unknown_and_brand():
    st, _ = load("p1p_fw011000_running_cloud.json", "bambu.p1p")
    slots = unit(st, "ams0").slots
    assert all(s.remaining_pct is None for s in slots)  # remain = -1
    assert slots[1].brand == "Bambu Lab" and slots[1].material == "PLA"   # GFA00 preset
    assert slots[0].active


def test_p1p_local_stage_label():
    st, _ = load("p1p_fw011000_running_local.json", "bambu.p1p")
    assert st.job.name == "plate_cleaner_petg"
    assert st.job.sub_state == "Waiting for heatbed temperature"   # stg_cur 54 in this capture
    assert unit(st, "ams0").slots[3].present is False


# --------------------------------------------------------------------------- A1 / A1 Mini
def test_a1_external_spool_and_full_ams():
    st, _ = load("a1_fw010801_running_local_extspool.json", "bambu.a1")
    assert [u.kind for u in st.filament] == ["AMS", "External spool"]
    ext = unit(st, "external").slots[0]
    assert (ext.present, ext.material, ext.color_hex, ext.brand, ext.active) == (
        True, "PETG", "#FFFFFF", "Generic", False)
    assert fan(st, "aux").percent is None and fan(st, "chamber").percent is None
    assert st.job.plate == 5
    assert st.job.file_name == "Halloween+Flexi+Kawaii+Candy+Corn+Frikarte3D.stl_plate_5.3mf"


def test_a1mini_failed_hides_stale_eta():
    st, _ = load("a1mini_fw010801_failed.json", "bambu.a1mini")
    assert st.state == JobState.FAILED
    assert st.job.remaining_s is None and st.job.eta is None   # printer still says 491 min
    assert st.job.name == "Santas_Helper_LightFury"
    assert {e.code for e in st.errors} == {"HMS_0300_1A00_0002_0001", "HMS_0300_0200_0001_0009"}
    assert all(not s.present for s in unit(st, "ams0").slots)


# --------------------------------------------------------------------------- merging
def test_merge_deltas_from_real_p1p_session():
    d = json.loads((FIX / "p1p_fw011000_running_local.json").read_text())
    state: dict = {}
    for m in d["messages"]:
        if "print" in m["payload"]:
            merge_report(state, m["payload"]["print"])
    assert state["mc_percent"] == 10 and state["mc_remaining_time"] == 46
    assert state["nozzle_temper"] == 239.28125                 # from the last delta
    assert len(state["ams"]["ams"][0]["tray"]) == 4              # full object kept


def test_merge_new_job_resets_inherited_progress():
    state: dict = {}
    merge_report(state, {"task_id": "A", "subtask_name": "a", "mc_percent": 100, "layer_num": 60})
    assert merge_report(state, {"task_id": "B", "subtask_name": "b", "gcode_state": "PREPARE"}) is True
    assert state["mc_percent"] == 0 and state["layer_num"] == 0
    assert merge_report(state, {"mc_percent": 3}) is False and state["mc_percent"] == 3


@pytest.mark.parametrize("name,expected", [
    ("plate_cleaner_petg.gcode.3mf", "plate_cleaner_petg"), ("X.3mf", "X"), ("Y", "Y"), (None, None)])
def test_clean_job_name(name, expected):
    assert clean_job_name(name) == expected


def test_plate_from_name():
    assert plate_from_name("Halloween+Flexi.stl_plate_5.3mf") == 5
    assert plate_from_name("Night Fury_plate_13_plate_13.gcode") == 13
    assert plate_from_name("Cube") is None
