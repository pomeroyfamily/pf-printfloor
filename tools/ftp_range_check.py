"""Step-by-step check of ranged FTPS reads on a Bambu printer (read-only).

  python tools/ftp_range_check.py --host 10.0.0.189 --code aaaa1111 --file /cache/Santas_Helper_LightFury.3mf

Tries each piece the fast thumbnail path needs and reports which one the printer refuses:
SIZE, REST + partial RETR (early close), control-channel sync afterwards, zip directory in the tail.
"""
from __future__ import annotations

import argparse
import ftplib
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.transports import ftps  # noqa: E402


def step(name, fn):
    t = time.time()
    try:
        r = fn()
        shown = f"{len(r)} bytes" if isinstance(r, (bytes, bytearray)) else (
            f"{len(r)} entries" if isinstance(r, dict) else ("" if r is None or not isinstance(r, (str, int)) else r))
        print(f"OK    {name:<34} {time.time() - t:5.1f}s  {shown}")
        return r
    except Exception as e:  # noqa: BLE001
        print(f"FAIL  {name:<34} {time.time() - t:5.1f}s  {type(e).__name__}: {e}")
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", required=True)
    ap.add_argument("--code", required=True)
    ap.add_argument("--file", required=True, help="e.g. /cache/Santas_Helper_LightFury.3mf")
    ap.add_argument("--port", type=int, default=990)
    ap.add_argument("--full-layout", action="store_true",
                    help="keep streaming to the end to list every entry (slow: whole file)")
    a = ap.parse_args()

    ftp = step("connect + login + PROT P", lambda: ftps.connect(a.host, "bblp", a.code, port=a.port))
    if not ftp:
        return
    print(f"      server welcome: {ftp.welcome!r}")
    step("FEAT", lambda: ftp.sendcmd("FEAT").replace("\n", " | ")[:200])
    step("TYPE I", lambda: ftp.voidcmd("TYPE I"))
    size = step("SIZE", lambda: ftp.size(a.file))
    if not size:
        print("      -> SIZE refused: trying MLST / LIST for the size instead")
        step("MLST", lambda: ftp.sendcmd(f"MLST {a.file}").replace("\n", " | ")[:200])
        lines = []
        step("LIST", lambda: (ftp.retrlines(f"LIST {a.file}", lines.append), lines[:1])[1])
        return
    from app.drivers.bambu import thumbnails  # noqa: E402
    trace: list = []
    t0 = time.time()
    first_png = {}

    def stop(kept, names):
        if kept and not first_png:
            first_png.update(name=next(iter(kept)), t=time.time() - t0, entries=len(names))
        return bool(kept) and not a.full_layout

    res = step("stream until plate image (no REST)", lambda: ftps.stream_zip_members(
        ftp, a.file, lambda n: bool(thumbnails._PLATE_PNG.match(n)), stop, trace=trace))
    if first_png:
        print(f"      first plate image {first_png['name']} after {first_png['t']:.1f}s")
    if res:
        kept, names, nread = res
        print(f"      read {nread / 1e6:.2f} MB of {size / 1e6:.2f} MB; images {list(kept)}")
    print("      entry layout (offset MB, method 0=stored 8=deflate, flags):")
    for name, at, method, flags in trace:
        print(f"        {at / 1e6:8.2f}  m{method} f{flags:#06x}  {name}")
    try:
        ftp.close()
    except Exception:  # noqa: BLE001
        pass
    ftp = step("reconnect", lambda: ftps.connect(a.host, "bblp", a.code, port=a.port))
    if not ftp:
        return
    head = step("REST 0 + read 4 bytes", lambda: ftps.read_range(ftp, a.file, 0, 4))
    print(f"      first bytes: {head!r} (expect b'PK\\x03\\x04')")
    step("NOOP after early close (sync)", lambda: ftp.voidcmd("NOOP"))
    tail_len = min(256 * 1024, size)
    tail = step(f"REST {size - tail_len} + read tail", lambda: ftps.read_range(ftp, a.file, size - tail_len, tail_len))
    if tail:
        print(f"      got {len(tail)} of {tail_len} bytes")
        idx = step("parse zip directory", lambda: ftps._parse_central_directory(tail, size - tail_len, size))
        if idx:
            pngs = [n for n in idx if n.startswith("Metadata/plate_") and n.endswith(".png")]
            print(f"      {len(idx)} entries, plate images: {pngs}")
            if pngs:
                data = step(f"read {pngs[0]} only", lambda: ftps.zip_member_ranged(ftp, a.file, idx[pngs[0]]))
                if data:
                    is_png = data[:8] == bytes.fromhex("89504e470d0a1a0a")
                    print(f"      {len(data)} bytes, valid PNG: {is_png}")
        elif tail.rfind(b"PK\x05\x06") < 0:
            print("      no end-of-zip marker in the tail")
        else:
            eocd = tail.rfind(b"PK\x05\x06")
            print(f"      EOCD found; cd size/offset = {struct.unpack('<II', tail[eocd + 12:eocd + 20])} (zip64 if 0xFFFFFFFF)")
    try:
        ftp.quit()
    except ftplib.all_errors:
        ftp.close()


if __name__ == "__main__":
    main()
