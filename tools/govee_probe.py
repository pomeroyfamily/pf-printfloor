"""Govee plug probe - READ ONLY. Lists the plugs on your Govee account, whether each offers on/off through the
Govee Platform API, and its current on/off state. Never switches anything.

Run from a shell on the Docker host (the key is typed at a hidden prompt, so it stays out of the shell history):

    read -s -p "Govee API key: " GOVEE_KEY; echo
    docker exec -i -e GOVEE_KEY="$GOVEE_KEY" pf-printfloor python - \
        < <source folder>/tools/govee_probe.py

API (developer.govee.com, Platform API): header Govee-API-Key;
  GET  /router/api/v1/user/devices    (30 requests/min per account)
  POST /router/api/v1/device/state    {"requestId", "payload": {"sku", "device"}}  (read; 30/min per device)
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
import uuid

BASE = os.environ.get("GOVEE_BASE", "https://openapi.api.govee.com")


def call(method, path, key, body=None):
    req = urllib.request.Request(BASE + path, method=method, data=json.dumps(body).encode() if body else None,
                                 headers={"Govee-API-Key": key, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return {"code": e.code, "message": e.read().decode(errors="replace")[:200]}


def main():
    key = (os.environ.get("GOVEE_KEY") or "").strip()
    if not key:
        print("No key: set GOVEE_KEY (see the instructions at the top of this file).")
        return 1
    r = call("GET", "/router/api/v1/user/devices", key)
    if r.get("code") != 200:
        print(f"Device list failed: code {r.get('code')}: {r.get('message')}")
        return 1
    devices = r.get("data") or []
    print(f"{len(devices)} device(s) on the account\n")
    for d in devices:
        caps = d.get("capabilities") or []
        names = sorted({f"{c.get('type', '').split('.')[-1]}/{c.get('instance')}" for c in caps})
        onoff = any(c.get("type") == "devices.capabilities.on_off" and c.get("instance") == "powerSwitch" for c in caps)
        toggles = [c.get("instance") for c in caps if c.get("type") == "devices.capabilities.toggle"]
        print(f"- {d.get('deviceName')!r}  sku {d.get('sku')}  type {str(d.get('type', '')).split('.')[-1]}")
        print(f"    on/off via API: {'YES (powerSwitch)' if onoff else 'no'}"
              + (f" | toggles: {', '.join(toggles)}" if toggles else ""))
        print(f"    capabilities: {', '.join(names) or 'none'}")
        if onoff or str(d.get("type", "")).endswith("socket"):
            s = call("POST", "/router/api/v1/device/state", key,
                     {"requestId": str(uuid.uuid4()), "payload": {"sku": d.get("sku"), "device": d.get("device")}})
            states = {}
            for c in ((s.get("payload") or {}).get("capabilities") or []):
                states[c.get("instance")] = (c.get("state") or {}).get("value")
            if s.get("code") == 200:
                pw = states.get("powerSwitch")
                print(f"    state now: power {'ON' if pw == 1 else 'OFF' if pw == 0 else pw}"
                      + (f" | online {states.get('online')}" if "online" in states else "")
                      + (f" | other: {json.dumps({k: v for k, v in states.items() if k not in ('powerSwitch', 'online')})}"
                         if len(states) > 2 else ""))
            else:
                print(f"    state read failed: code {s.get('code')}: {s.get('message')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
