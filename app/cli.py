"""Command-line tools while the web UI is being built.

  python -m app.cli drivers
  python -m app.cli watch --driver bambu.x1c --host 10.0.0.90 --code bbbb2222
  python -m app.cli bambu-cloud login | status | tasks | logout

`watch` runs the real driver against one printer and prints the normalized status
the dashboard cards will show. Read-only. Ctrl+C to stop.
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import sys
import time
from datetime import datetime

from . import config
from .cloud import bambu as bambu_cloud
from .drivers import catalog, get_driver
from .models import PrinterStatus
from .store import PrinterConfig, PrinterStore


def _fmt_dur(sec):
    if sec is None:
        return ""
    sec = int(sec)
    return f"{sec // 3600}h {sec % 3600 // 60:02d}m"


def render(st: PrinterStatus) -> str:
    j = st.job
    lines = [f"[{datetime.now():%H:%M:%S}] {st.connection.value.upper()}"
             + (f" ({st.connection_detail})" if st.connection_detail else "")
             + f" | {st.state.value}" + (f" - {j.sub_state}" if j.sub_state else "")
             + (f" | fw {st.firmware}" if st.firmware else "")]
    if j.name:
        eta = f"  ETA {j.eta.astimezone():%H:%M} ({_fmt_dur(j.remaining_s)} left)" if j.eta else ""
        prog = f"{j.progress_pct:.0f}%" if j.progress_pct is not None else ""
        layer = f"  layer {j.layer}/{j.total_layers}" if j.total_layers else ""
        plate = f"  plate {j.plate}" + (f"/{j.plate_count}" if j.plate_count else "") if j.plate else ""
        lines.append(f"  job      : {j.name}  {prog}{layer}{plate}{eta}")
    if st.connection.value == "connecting" and not st.temps:
        return lines[0]
    temps = "  ".join(f"{t.label} {'' if t.current is None else f'{t.current:.0f}'}"
                      + (f"/{t.target:.0f}" if t.target else "") for t in st.temps)
    lines.append(f"  temps    : {temps}")
    fans = "  ".join(f"{f.label} {'' if f.percent is None else f'{f.percent}%'}" for f in st.fans)
    lines.append(f"  fans     : {fans}")
    for u in st.filament:
        slots = []
        for s in u.slots:
            if not s.present:
                slots.append("--")
                continue
            mat = s.sub_type if (s.sub_type and s.material and s.material in s.sub_type) else \
                " ".join(x for x in (s.material, s.sub_type) if x) or "?"
            txt = f"{mat}" \
                  f" {s.color_hex or ''}{' (' + s.brand + ')' if s.brand else ''}" \
                  f"{f' {s.remaining_pct}%' if s.remaining_pct is not None else ''}"
            slots.append(("*" if s.active else "") + txt)
        hum = f" hum {u.humidity_pct}%" if u.humidity_pct is not None else ""
        lines.append(f"  {u.kind:<9}: " + " | ".join(slots) + hum)
    if st.errors:
        lines.append("  errors   : " + ", ".join(e.code for e in st.errors))
    if j.thumbnail_url:
        lines.append(f"  thumbnail: saved to {config.THUMB_DIR / (st.printer_id + '.png')}"
                     f" (from {j.thumbnail_source})")
    return "\n".join(lines)


async def watch(args) -> None:
    cls = get_driver(args.driver)
    secrets = {}
    if any(f.key == "access_code" for f in cls.fields):
        secrets["access_code"] = args.code or getpass.getpass("LAN access code: ")
    if args.api_key:
        secrets["api_key"] = args.api_key
    settings = {"ranged_ftp": not args.full_ftp}
    if args.mqtt_port:
        settings["mqtt_port"] = args.mqtt_port
    if args.ftps_port:
        settings["ftps_port"] = args.ftps_port
    cfg = PrinterConfig(id=args.id, name=args.id, driver=args.driver, host=args.host, serial=args.serial,
                        settings=settings, secrets=secrets)
    last = {"t": 0.0, "conn": None, "thumb": None}

    def on_update(st: PrinterStatus):
        now = time.monotonic()
        changed = st.connection != last["conn"] or st.job.thumbnail_url != last["thumb"]
        if changed or now - last["t"] >= args.every:
            print(render(st), flush=True)
            last.update(t=now, conn=st.connection, thumb=st.job.thumbnail_url)

    if args.driver.startswith("bambu.") and not args.no_cloud:
        store = PrinterStore(config.DB_PATH)
        acct = bambu_cloud.account_from_store(store)
        store.close()
        bambu_cloud.set_account(acct)
        print("Bambu account: " + (f"{acct.email} ({acct.state})" if acct else
              "not linked - thumbnails from the printer, best effort"), flush=True)
    printer = cls(cfg, on_update)
    print(f"Watching {args.driver} at {args.host} (Ctrl+C to stop) ...", flush=True)
    printer.start()
    try:
        await asyncio.sleep(args.seconds if args.seconds else 10 ** 9)
    finally:
        await printer.stop()


def _fmt_time(t):
    return datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M") if t else "unknown"


def cloud_cmd(args) -> None:
    store = PrinterStore(config.DB_PATH)
    try:
        if args.action == "logout":
            print("unlinked" if store.delete_app_setting("bambu_cloud") else "no account was linked")
            return
        if args.action == "login":
            email = args.email or input("Bambu account email: ").strip()
            cloud = bambu_cloud.BambuCloud(region=args.region)
            try:
                cloud.login(email, getpass.getpass("Password (not shown, not saved): "))
            except bambu_cloud.NeedsEmailCode:
                cloud.login_with_code(email, input("Code from the Bambu email: "))
            except bambu_cloud.NeedsTfa as e:
                cloud.login_tfa(e.tfa_key, input("Authenticator app code: "))
            bambu_cloud.save_link(store, email, cloud)
            exp = bambu_cloud.token_expiry(cloud.token or "")
            print(f"linked {email}; token expires {_fmt_time(exp)}")
            return
        acct = bambu_cloud.account_from_store(store)
        if not acct:
            print("no Bambu account linked (python -m app.cli bambu-cloud login)")
            return
        saved = store.get_app_setting("bambu_cloud") or {}
        print(f"{acct.email} ({saved.get('region')}): {acct.state}; token expires {_fmt_time(saved.get('expires'))}")
        if args.action == "devices":
            def mask(v):
                v = str(v or "")
                return (v[:2] + "*" * (len(v) - 2)) if len(v) > 2 else v
            devs = acct.cloud.devices()
            print(f"  {len(devs)} printer(s) bound to the account")
            for d in devs:
                shown = {k: (mask(v) if "access" in k.lower() or "code" in k.lower() else v) for k, v in d.items()}
                print("  " + ", ".join(f"{k}={v}" for k, v in shown.items()))
        if args.action == "tasks":
            for t in acct.cloud.tasks(limit=args.limit):
                print(f"  {str(t.get('startTime') or '')[:16]:<16}  task {t.get('id')}  printer {t.get('deviceId')}"
                      f"  status {t.get('status')}  {t.get('title') or t.get('designTitle')}"
                      f"  cover: {'yes' if t.get('cover') else 'no'}")
    except bambu_cloud.CloudError as e:
        print(f"error: {e}")
        sys.exit(1)
    finally:
        store.close()


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="python -m app.cli")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("drivers", help="list supported printers")
    w = sub.add_parser("watch", help="show live normalized status for one printer")
    w.add_argument("--driver", required=True, help="e.g. bambu.x1c, bambu.p1p, bambu.a1, bambu.a1mini")
    w.add_argument("--host", required=True)
    w.add_argument("--code", help="Bambu LAN access code (prompted if omitted)")
    w.add_argument("--serial", help="optional; read from the printer certificate if omitted")
    w.add_argument("--api-key", help="Moonraker API key, if required")
    w.add_argument("--every", type=float, default=5, help="print at most every N seconds (default 5)")
    w.add_argument("--seconds", type=float, default=0, help="stop after N seconds (default: run until Ctrl+C)")
    w.add_argument("--full-ftp", action="store_true", help="skip the ranged (REST) read; stream the .3mf from the start")
    w.add_argument("--id", default="cli", help=argparse.SUPPRESS)
    w.add_argument("--mqtt-port", type=int, help=argparse.SUPPRESS)
    w.add_argument("--ftps-port", type=int, help=argparse.SUPPRESS)
    w.add_argument("--no-cloud", action="store_true", help="ignore the linked Bambu account")
    w.add_argument("-v", "--verbose", action="store_true")
    c = sub.add_parser("bambu-cloud", help="link the optional Bambu account (cover images for cloud prints)")
    c.add_argument("action", choices=["login", "status", "tasks", "devices", "logout"])
    c.add_argument("--email")
    c.add_argument("--region", default="global", choices=sorted(bambu_cloud.REGIONS))
    c.add_argument("--limit", type=int, default=10)
    args = ap.parse_args(argv)

    if args.cmd == "drivers":
        for m in catalog():
            for model in m["models"]:
                print(f"{model['driver']:<18} {m['maker']} {model['model']}")
        return
    if args.cmd == "bambu-cloud":
        cloud_cmd(args)
        return
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, stream=sys.stdout,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        asyncio.run(watch(args))
    except KeyboardInterrupt:
        print("stopped")
        sys.exit(0)


if __name__ == "__main__":
    main()
