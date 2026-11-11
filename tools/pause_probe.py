"""Pause probe - READ ONLY. Captures everything a Snapmaker U1 (Klipper/Moonraker) or Elegoo Centauri Carbon reports,
so we can see WHY it paused. Run it while the printer is paused.

From a shell on the Docker host (a short summary prints on screen):

    docker exec -i pf-printfloor python - "U1-B" \
        < <source folder>/tools/pause_probe.py \
        > <source folder>/pause_U1-B.json

Attach the summary and pause_U1-B.json to an issue (check it first: it contains your printer's status).
U1: GET /printer/objects/list, then GET /printer/objects/query for EVERY object, plus /printer/info and the last
    history job. Nothing is sent to Klipper.
CC: SDCP Cmd 0 (status) + Cmd 1 (attributes) + Cmd 320/321 (last job details) + Cmd 324 (Canvas trays - safe
    mid-print, verified 2026-10-30). Nothing else. (2026-11-02: the CC's "filament or spool may be jammed" pause was
    NOT in status/attributes; Canvas added to look there.)
"""
from __future__ import annotations

import json
import secrets
import socket
import sys
import time
import urllib.parse
import urllib.request
import uuid

from app import config
from app.store import PrinterStore
from app.transports.websocket import WebSocket


def say(*a):
    print(*a, file=sys.stderr)


def u1(cfg) -> dict:
    port = int(cfg.settings.get("port") or 80)
    base = f"http://{cfg.host}" + ("" if port == 80 else f":{port}")

    def get(path):
        with urllib.request.urlopen(base + path, timeout=10) as r:
            return json.loads(r.read())
    out = {"printer_info": get("/printer/info").get("result")}
    objects = get("/printer/objects/list")["result"]["objects"]
    out["objects"] = objects
    q = "&".join(urllib.parse.quote(o) for o in objects)
    out["status"] = get("/printer/objects/query?" + q)["result"]["status"]
    try:
        out["last_job"] = get("/server/history/list?limit=1&order=desc")["result"]
    except Exception as e:  # noqa: BLE001
        out["last_job"] = f"unavailable: {e}"
    st = out["status"]
    ps = st.get("print_stats") or {}
    say(f"  print_stats.state  : {ps.get('state')}")
    say(f"  print_stats.message: {ps.get('message')!r}")
    say(f"  pause_resume       : {st.get('pause_resume')}")
    say(f"  display_status     : {st.get('display_status')}")
    say(f"  webhooks           : {st.get('webhooks')}")
    for name, v in st.items():
        if any(k in name for k in ("filament", "runout", "motion_sensor", "switch_sensor")):
            say(f"  {name}: {json.dumps(v)[:200]}")
    return out


def cc(cfg) -> dict:
    mb = cfg.serial
    ws = WebSocket(cfg.host, int(cfg.settings.get("ws_port") or 3030))
    ws.settimeout(1)

    def req(cmd, data=None):
        ws.send_text(json.dumps({"Id": uuid.uuid4().hex, "Data": {"Cmd": cmd, "Data": data or {}, "RequestID":
                     secrets.token_hex(8), "MainboardID": mb, "TimeStamp": int(time.time() * 1000), "From": 1},
                     "Topic": f"sdcp/request/{mb}"}))
    msgs: list = []
    req(1); req(0); req(320); req(324)
    deadline, asked_321 = time.monotonic() + 8, False
    while time.monotonic() < deadline:
        try:
            txt = ws.recv_text()
        except socket.timeout:
            continue
        if not txt or txt == "pong":
            if txt is None:
                break
            continue
        m = json.loads(txt)
        msgs.append(m)
        d = m.get("Data") or {}
        if d.get("Cmd") == 320 and not asked_321:
            ids = ((d.get("Data") or {}).get("HistoryData") or [])[:2]
            if ids:
                req(321, {"Id": ids})
                asked_321, deadline = True, time.monotonic() + 5
    ws.sock.close()
    status = next((m.get("Status") for m in reversed(msgs) if m.get("Status")), {}) or {}
    pi = status.get("PrintInfo") or {}
    say(f"  CurrentStatus : {status.get('CurrentStatus')}")
    say(f"  PrintInfo     : Status {pi.get('Status')}  ErrorNumber {pi.get('ErrorNumber')}  "
        f"Layer {pi.get('CurrentLayer')}/{pi.get('TotalLayer')}")
    other = {k: v for k, v in pi.items() if k not in ("Status", "CurrentLayer", "TotalLayer", "CurrentTicks",
                                                      "TotalTicks", "Filename", "TaskId", "Progress")}
    say(f"  PrintInfo rest: {json.dumps(other)[:300]}")
    canvas = next(((m.get("Data") or {}).get("Data") for m in msgs if (m.get("Data") or {}).get("Cmd") == 324), None)
    if canvas:
        say(f"  Canvas        : active canvas {canvas.get('active_canvas_id')}  active tray {canvas.get('active_tray_id')}"
            f"  other keys {[k for k in canvas if k not in ('canvas_list', 'active_canvas_id', 'active_tray_id', 'Ack')]}")
        for c in canvas.get("canvas_list") or []:
            for t in c.get("tray_list") or []:
                extra = {k: v for k, v in t.items() if k not in ("tray_id", "brand", "filament_type", "filament_name",
                         "filament_code", "filament_color", "min_nozzle_temp", "max_nozzle_temp", "status")}
                say(f"    tray {t.get('tray_id')}: {t.get('filament_color')} status {t.get('status')}"
                    + (f" {json.dumps(extra)}" if extra else ""))
    else:
        say("  Canvas        : no reply")
    return {"messages": msgs}


def main(name: str) -> int:
    store = PrinterStore(config.DB_PATH)
    cfg = next((p for p in store.list() if p.name.lower() == name.lower()), None)
    if not cfg:
        say("not found. Names: " + ", ".join(p.name for p in store.list()))
        return 1
    say(f"=== {cfg.name} ({cfg.driver} at {cfg.host}) ===")
    if cfg.driver.startswith("snapmaker.") or cfg.driver.startswith("klipper."):
        data = u1(cfg)
    elif cfg.driver.startswith("elegoo."):
        data = cc(cfg)
    else:
        say("Bambus already report the reason in the dashboard (Details > Errors).")
        return 1
    print(json.dumps({"printer": cfg.name, "driver": cfg.driver, "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                      **data}, indent=1, default=str))
    say("  full dump written to the file you redirected to")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else ""))
