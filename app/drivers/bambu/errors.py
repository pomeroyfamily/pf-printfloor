"""Plain-English text for Bambu error codes (print_error and HMS).

Data: `hms_en.json.gz` from ha-bambulab (github.com/greghesp/ha-bambulab, pybambu/hms_error_text, MIT License,
Copyright (c) 2023 ha-bambulab contributors), copied 2026-11-02. Structure:
  {"device_error": {"07008006": {"<text>": [models...]}}, "device_hms": {"0300010000010001": {...}}}
An empty model list = the default text; a non-empty list = text specific to those printers.
Verified on a real A1 Mini 2026-11-02: PRINT_ERROR 0700 8006 = "Unable to feed filament into the extruder...".
"""
from __future__ import annotations

import functools
import gzip
import json
import re
from pathlib import Path
from typing import Optional

_FILE = Path(__file__).parent / "hms_en.json.gz"


@functools.lru_cache(maxsize=1)
def _data() -> dict:
    try:
        with gzip.open(_FILE, "rt", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _pick(entry: Optional[dict], model: Optional[str]) -> Optional[str]:
    if not entry:
        return None
    default = None
    for text, models in entry.items():
        if models and model and model in models:
            return text
        if not models and default is None:
            default = text
    return default or next(iter(entry), None)


def print_error_text(code: int, model: Optional[str] = None) -> Optional[str]:
    return _pick(_data().get("device_error", {}).get(f"{code:08X}"), model)


def hms_text(attr: int, code: int, model: Optional[str] = None) -> Optional[str]:
    return _pick(_data().get("device_hms", {}).get(f"{attr:08X}{code:08X}"), model)


def short(text: Optional[str]) -> Optional[str]:
    """First sentence, for the card ("Unable to feed filament into the extruder.")."""
    if not text:
        return None
    m = re.match(r"(.+?[.!?])(\s|$)", text.strip())
    return (m.group(1) if m else text.strip())
