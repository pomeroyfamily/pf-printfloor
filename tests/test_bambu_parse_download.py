from app.drivers.bambu import parse


def _st(**kw):
    s = {"gcode_state": "PREPARE", "subtask_name": "Night Fury_plate_1", "gcode_file": "/data/Metadata/plate_1.gcode"}
    s.update(kw)
    return parse.to_status("x", s, None, has_chamber_sensor=False, has_aux_fan=False, has_chamber_fan=False)


def test_bambu_download_percent():
    st = _st(gcode_file_prepare_percent="42")
    assert st.job.transferring and st.job.transfer_pct == 42 and st.job.sub_state == "downloading file 42%"


def test_bambu_download_done_or_lan_job():
    assert not _st(gcode_file_prepare_percent="100").job.transferring
    assert not _st().job.transferring                              # LAN prints never send the field
    assert not _st(gcode_state="RUNNING", gcode_file_prepare_percent="42").job.transferring


# ---- why it paused (a real A1 Mini 2026-11-02: Paused, PRINT_ERROR_07008006, no stage reason) ----
from app.drivers.bambu import errors as bambu_errors  # noqa: E402


def _paused(**kw):
    s = {"gcode_state": "PAUSE", "subtask_name": "salt_pepper", "gcode_file": "/data/Metadata/plate_1.gcode"}
    s.update(kw)
    return parse.to_status("x", s, None, has_chamber_sensor=False, has_aux_fan=False, has_chamber_fan=False,
                           model="A1MINI")


def test_pause_reason_from_print_error_real_a1_mini():
    st = _paused(print_error=0x07008006, stg_cur=255)
    assert st.state.value == "paused"
    assert st.errors[0].code == "PRINT_ERROR_07008006"
    assert st.errors[0].message.startswith("Unable to feed filament into the extruder.")
    assert st.reason == "Unable to feed filament into the extruder."          # first sentence for the card


def test_pause_reason_from_stage_wins():
    st = _paused(stg_cur=6)                                                     # "Paused: filament runout"
    assert st.reason == "Paused: filament runout"
    assert _paused(stg_cur=16).reason == "Paused by user"


def test_no_reason_when_printing_or_unknown_code():
    s = parse.to_status("x", {"gcode_state": "RUNNING", "print_error": 0x07008006}, None, has_chamber_sensor=False,
                        has_aux_fan=False, has_chamber_fan=False, model="A1")
    assert s.reason is None
    assert _paused(print_error=0x7FFFFFFF).reason is None                       # not in the table: no made-up text
    assert _paused(print_error=0x7FFFFFFF).errors[0].message is None


def test_hms_codes_get_text_and_printer_specific_text_wins():
    data = bambu_errors._data()
    assert len(data["device_error"]) > 900 and len(data["device_hms"]) > 4000
    code, entry = next((k, v) for k, v in data["device_error"].items() if any(v.values()) and len(v) > 1)
    specific_model = next(m for ms in entry.values() for m in ms)
    specific_text = next(t for t, ms in entry.items() if specific_model in ms)
    assert bambu_errors.print_error_text(int(code, 16), specific_model) == specific_text
    hcode = next(iter(data["device_hms"]))
    assert bambu_errors.hms_text(int(hcode[:8], 16), int(hcode[8:], 16))
