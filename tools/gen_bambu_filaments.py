"""Regenerate app/drivers/bambu/filaments.py from a BambuStudio checkout.

  git clone --depth 1 --filter=blob:none --sparse https://github.com/bambulab/BambuStudio
  cd BambuStudio && git sparse-checkout set resources/profiles/BBL/filament
  python tools/gen_bambu_filaments.py <path-to>/BambuStudio/resources/profiles/BBL/filament
"""
import glob
import json
import re
import sys
from pathlib import Path

src = Path(sys.argv[1])
rows = {}
for f in glob.glob(str(src / "**" / "*.json"), recursive=True):
    try:
        d = json.load(open(f, encoding="utf-8"))
    except Exception:  # noqa: BLE001
        continue
    fid = d.get("filament_id")
    if not fid:
        continue
    name = re.sub(r"\s*@.*$", "", d.get("name", "")).strip()
    vendor = (d.get("filament_vendor") or [None])[0] or ("Generic" if name.startswith("Generic") else None)
    rows[fid] = (name, vendor)
out = Path(__file__).resolve().parents[1] / "app" / "drivers" / "bambu" / "filaments.py"
lines = ['"""Bambu filament preset id (tray_info_idx) -> (display name, vendor).', "",
         f"Generated from {src} by tools/gen_bambu_filaments.py. Regenerate when Bambu adds presets.\"\"\"",
         "FILAMENTS: dict[str, tuple[str, str | None]] = {"]
lines += [f"    {k!r}: ({v[0]!r}, {v[1]!r})," for k, v in sorted(rows.items())]
lines.append("}")
out.write_text("\n".join(lines) + "\n", encoding="utf-8")
print(f"wrote {len(rows)} presets to {out}")
