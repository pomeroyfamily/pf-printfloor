"""Find the current job's .3mf on the printer's FTPS storage and extract its plate image.

Rules learned on real printers (docs/SPEC.md 5.1):
* local prints: `gcode_file` is the exact file name (root or /cache)
* P1/A1 cloud prints: /cache/<subtask_name>.3mf
* X1C cloud prints: kept in internal /data/... -> not on FTPS (returns None)
* never strip _plate_N before trying exact names (A1 matched the wrong plate file that way)
* long names are truncated in listings with "...." before the extension
* the sliced plate is the one whose Metadata/plate_N.gcode exists in the 3MF
"""
from __future__ import annotations

import ftplib
import re
import struct
import zlib
from typing import Optional

from ...transports import ftps

FOLDERS = ("/", "/cache")
_PLATE_GCODE = re.compile(r"^Metadata/plate_(\d+)\.gcode$")
_PLATE_SUFFIX = re.compile(r"_plate_\d+$", re.IGNORECASE)


def _variants(name: str) -> list[str]:
    base = name
    for suf in (".gcode.3mf", ".3mf"):
        if base.lower().endswith(suf):
            base = base[: -len(suf)]
            break
    out = [name, f"{base}.gcode.3mf", f"{base}.3mf"]
    return list(dict.fromkeys(out))


def find_job_file(listing: dict[str, list[str]], gcode_file: Optional[str], subtask: Optional[str],
                  prefer_cache: bool) -> Optional[str]:
    folders = ("/cache", "/") if prefer_cache else FOLDERS
    wanted: list[str] = []
    if gcode_file and not gcode_file.startswith("/data/"):
        wanted += _variants(gcode_file.rsplit("/", 1)[-1])
    if subtask:
        wanted += _variants(subtask)
    wanted = list(dict.fromkeys(w for w in wanted if w))

    lower = {f: {n.lower(): n for n in listing.get(f, [])} for f in folders}
    # 1) exact names
    for w in wanted:
        for f in folders:
            hit = lower[f].get(w.lower())
            if hit:
                return f"{f.rstrip('/')}/{hit}"
    # 2) truncated listing entries ("very long nam....3mf")
    for f in folders:
        for n in listing.get(f, []):
            if "...." in n:
                prefix = n.split("....", 1)[0].lower()
                if prefix and any(w.lower().startswith(prefix) for w in wanted):
                    return f"{f.rstrip('/')}/{n}"
    # 3) last resort: project file without the _plate_N suffix (one project, many plates)
    if subtask and _PLATE_SUFFIX.search(subtask):
        base = _PLATE_SUFFIX.sub("", subtask)
        for w in _variants(base):
            for f in folders:
                hit = lower[f].get(w.lower())
                if hit:
                    return f"{f.rstrip('/')}/{hit}"
    return None


def pick_plate_image(names: list[str], plate_hint: Optional[int]) -> Optional[str]:
    sliced = sorted(int(m[1]) for n in names if (m := _PLATE_GCODE.match(n)))
    candidates = []
    if len(sliced) == 1:
        candidates.append(sliced[0])
    if plate_hint:
        candidates.append(plate_hint)
    candidates += sliced
    for p in candidates:
        if f"Metadata/plate_{p}.png" in names:
            return f"Metadata/plate_{p}.png"
    pngs = sorted(n for n in names if re.match(r"^Metadata/plate_\d+\.png$", n))
    return pngs[0] if pngs else None


_PLATE_PNG = re.compile(r"^Metadata/plate_(\d+)\.png$")


def _stream_plate_image(ftp, path: str, plate_hint: Optional[int]) -> tuple[Optional[bytes], str]:
    """Read the .3mf from the start and hang up as soon as the right plate image is in hand."""
    def keep(name: str) -> bool:
        return bool(_PLATE_PNG.match(name))

    def stop(kept: dict[str, bytes], names: list[str]) -> bool:
        sliced = [int(m[1]) for n in names if (m := _PLATE_GCODE.match(n))]
        if any(f"Metadata/plate_{p}.png" in kept for p in sliced):
            return True                       # sliced plate known and its image arrived
        return bool(plate_hint) and f"Metadata/plate_{plate_hint}.png" in kept

    kept, names, nread = ftps.stream_zip_members(ftp, path, keep, stop)
    member = pick_plate_image(names, plate_hint)
    if member not in kept:
        member = pick_plate_image(list(kept), plate_hint)
    size = None
    try:
        size = ftp.size(path)
    except ftplib.all_errors:
        pass
    of = f" of {size / 1e6:.1f} MB" if size else ""
    if not member:
        return None, f"no plate image in {path} (streamed {nread / 1e6:.1f} MB{of})"
    return kept[member], f"{path}:{member} (stream, read {nread / 1e6:.1f} MB{of})"


def fetch_thumbnail(host: str, access_code: str, gcode_file: Optional[str], subtask: Optional[str],
                    plate_hint: Optional[int], prefer_cache: bool, allow_ranged: bool = True,
                    port: int = 990) -> tuple[Optional[bytes], str]:
    """Blocking. Returns (png bytes or None, short description for logs).

    Always reads from the job's file on the printer (no cache: same name can mean different plates).
    Order: ranged read (REST) -> stream from the start and stop early -> full download.
    """
    if gcode_file and gcode_file.startswith("/data/") and not subtask:
        return None, "job file is internal to the printer"
    ftp = ftps.connect(host, "bblp", access_code, port=port)

    def reconnect():
        nonlocal ftp
        try:
            ftp.close()
        except Exception:  # noqa: BLE001
            pass
        ftp = ftps.connect(host, "bblp", access_code, port=port)

    try:
        listing = {f: ftps.list_names(ftp, f) for f in FOLDERS}
        path = find_job_file(listing, gcode_file, subtask, prefer_cache)
        if not path:
            return None, "job file not found on printer storage"
        parse_errors = ftplib.all_errors + (ValueError, struct.error, zlib.error, KeyError)

        # 1) ranged
        if allow_ranged:
            try:
                index = ftps.zip_index_ranged(ftp, path)
                if index is not None:
                    member = pick_plate_image(list(index), plate_hint)
                    if not member:
                        return None, f"no plate image in {path}"
                    return ftps.zip_member_ranged(ftp, path, index[member]), f"{path}:{member} (ranged)"
                why = "zip directory not in file tail"
            except parse_errors as e:
                why = f"{type(e).__name__}: {e}".strip()
                reconnect()
            ftps.log.info("ranged read not possible for %s (%s) - streaming instead", path, why)

        # 2) stream and stop early
        try:
            return _stream_plate_image(ftp, path, plate_hint)
        except ftps.StreamNotPossible as e:
            ftps.log.info("streaming not possible for %s (%s) - downloading whole file", path, e)
        except parse_errors as e:
            ftps.log.info("streaming failed for %s (%s: %s) - downloading whole file", path, type(e).__name__, e)
        reconnect()

        # 3) full download
        z = ftps.ZipOnFtp(ftp, path, allow_ranged=False)
        member = pick_plate_image(z.names(), plate_hint)
        return (z.read(member) if member else None), f"{path}:{member} (full)"
    finally:
        try:
            ftp.quit()
        except Exception:  # noqa: BLE001
            ftp.close()
