"""Bambu reboot probe - READ ONLY. Prints the `sequence_id` counters a Bambu printer sends, so we can see whether
they start over when the printer reboots (a way to spot reboots done at the printer itself).

Uses the printer's own MQTT with the access code the dashboard already has saved (never printed). Sends only
`pushall` (ask for a full status), the same as the dashboard does.

From a shell on the Docker host (use the printer's IP or its dashboard name):

    docker exec -i pf-printfloor python - "10.0.0.119" \
        < <source folder>/tools/bambu_seq_probe.py

Run it once, reboot the printer (idle!), wait until it's back on the dashboard, run it again. Compare (or attach to an issue) both outputs.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time

from app import config
from app.store import PrinterStore
from app.transports.mqtt import MqttConnection

PUSHALL = json.dumps({"pushing": {"sequence_id": "1", "command": "pushall", "version": 1, "push_target": 1}})


async def main(key: str) -> int:
    store = PrinterStore(config.DB_PATH)
    cfg = next((p for p in store.list() if key in (p.host, p.name) or p.name.lower() == key.lower()), None)
    if not cfg or not cfg.driver.startswith("bambu."):
        print("no Bambu printer with that IP or name. Bambus: "
              + ", ".join(f"{p.name} ({p.host})" for p in store.list() if p.driver.startswith("bambu.")))
        return 1
    print(f"=== {cfg.name} ({cfg.driver} at {cfg.host}) {time.strftime('%Y-%m-%d %H:%M:%S')} ===")
    if not cfg.serial:
        print("serial number not known yet - let the dashboard connect to it once first")
        return 1
    conn = MqttConnection(cfg.host, int(cfg.settings.get("mqtt_port", 8883)), "bblp", cfg.secrets.get("access_code", ""))
    await conn.connect()
    seen: list[tuple[float, str, object]] = []
    t0 = time.monotonic()
    async def collect():
        conn.subscribe(f"device/{cfg.serial}/report")
        conn.publish(f"device/{cfg.serial}/request", PUSHALL)
        async for _topic, payload in conn.messages(30):
            try:
                data = json.loads(payload)
            except ValueError:
                continue
            for section, body in data.items():
                if isinstance(body, dict) and "sequence_id" in body:
                    seen.append((time.monotonic() - t0, f"{section}.{body.get('command', '?')}", body["sequence_id"]))
    try:
        await asyncio.wait_for(collect(), 12)            # listen ~12 s
    except asyncio.TimeoutError:
        pass
    except Exception as e:  # noqa: BLE001 - report what we have
        print(f"(stopped early: {e})")
    finally:
        await conn.close()
    for t, what, seq in seen[:25]:
        print(f"  +{t:5.1f}s  {what:<22} sequence_id = {seq}")
    nums = [int(s) for _, _, s in seen if str(s).isdigit()]
    if nums:
        print(f"lowest {min(nums)}, highest {max(nums)}, {len(seen)} messages in ~12 s")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "")))
