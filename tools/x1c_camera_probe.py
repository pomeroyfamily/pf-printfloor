"""X1C camera probe - READ ONLY. Checks whether the X1C's built-in camera stream (RTSPS, port 322) answers.

Asks the printer to DESCRIBE the stream (the RTSP equivalent of "what would you send me?"). Starts no video,
changes no setting, sends nothing over MQTT. Uses the access code the dashboard already has saved; never prints it.

Before running: on the X1C, Settings -> Network -> turn on "LAN Mode Liveview" (gear/octagon icon on the left; LAN Only Mode can stay off; restart
    the printer if the probe still says not reachable).
From a shell on the Docker host:

    docker exec -i pf-printfloor python - "X1C" \
        < <source folder>/tools/x1c_camera_probe.py

Attach what it prints to an issue if asked.
"""
from __future__ import annotations

import base64
import hashlib
import re
import socket
import ssl
import sys

from app import config
from app.store import PrinterStore

PORT = 322
PATH = "/streaming/live/1"
USER = "bblp"


def say(*a):
    print(*a, flush=True)


def rtsp(sock, method: str, url: str, cseq: int, auth: str | None = None) -> tuple[int, dict, str]:
    req = f"{method} {url} RTSP/1.0\r\nCSeq: {cseq}\r\nUser-Agent: pf-printfloor-probe\r\n"
    if method == "DESCRIBE":
        req += "Accept: application/sdp\r\n"
    if auth:
        req += f"Authorization: {auth}\r\n"
    sock.sendall((req + "\r\n").encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("printer closed the connection")
        buf += chunk
    head, _, rest = buf.partition(b"\r\n\r\n")
    lines = head.decode(errors="replace").split("\r\n")
    code = int(lines[0].split()[1])
    headers = {}
    for ln in lines[1:]:
        k, _, v = ln.partition(":")
        headers.setdefault(k.strip().lower(), v.strip())
    need = int(headers.get("content-length", "0"))
    while len(rest) < need:
        chunk = sock.recv(4096)
        if not chunk:
            break
        rest += chunk
    return code, headers, rest[:need].decode(errors="replace")


def digest(h: str, method: str, url: str, password: str) -> str:
    f = dict(re.findall(r'(\w+)="?([^",]*)"?', h))
    ha1 = hashlib.md5(f"{USER}:{f.get('realm', '')}:{password}".encode()).hexdigest()
    ha2 = hashlib.md5(f"{method}:{url}".encode()).hexdigest()
    resp = hashlib.md5(f"{ha1}:{f.get('nonce', '')}:{ha2}".encode()).hexdigest()
    return (f'Digest username="{USER}", realm="{f.get("realm", "")}", nonce="{f.get("nonce", "")}", '
            f'uri="{url}", response="{resp}"')


def main(name: str) -> int:
    store = PrinterStore(config.DB_PATH)
    cfg = next((p for p in store.list() if p.name.lower() == name.lower()), None)
    if not cfg:
        say("not found. Names: " + ", ".join(p.name for p in store.list()))
        return 1
    code = cfg.secrets.get("access_code")
    say(f"=== {cfg.name} ({cfg.driver} at {cfg.host}) ===")
    if not code:
        say("no access code saved for this printer")
        return 1
    url = f"rtsps://{cfg.host}:{PORT}{PATH}"
    ctx = ssl.create_default_context()
    ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE     # printer uses a self-signed certificate
    try:
        raw = socket.create_connection((cfg.host, PORT), timeout=10)
    except OSError as e:
        say(f"port {PORT}: NOT reachable ({e}).")
        say("  -> Usually means LAN Mode Liveview is off (or the printer is asleep / another IP).")
        return 2
    say(f"port {PORT}: open")
    try:
        with ctx.wrap_socket(raw, server_hostname=cfg.host) as s:
            s.settimeout(15)
            say("TLS: ok")
            c, h, _ = rtsp(s, "OPTIONS", url, 1)
            say(f"OPTIONS -> {c}  {h.get('public', '')}")
            c, h, body = rtsp(s, "DESCRIBE", url, 2)
            if c == 401:
                wa = h.get("www-authenticate", "")
                scheme = wa.split()[0] if wa else "?"
                auth = digest(wa, "DESCRIBE", url, code) if scheme.lower() == "digest" else \
                    "Basic " + base64.b64encode(f"{USER}:{code}".encode()).decode()
                c, h, body = rtsp(s, "DESCRIBE", url, 3, auth)
                say(f"DESCRIBE (auth {scheme}) -> {c}")
            else:
                say(f"DESCRIBE -> {c}")
            if c == 200:
                video = re.findall(r"^m=video.*$", body, re.M)
                codec = re.findall(r"^a=rtpmap:\d+ ([\w.-]+/\d+)", body, re.M)
                size = re.findall(r"^a=(?:framesize|x-dimensions|cliprect):.*$", body, re.M)
                say(f"stream: {'YES' if video else 'no video track'}  codec {', '.join(codec) or '?'}  {' '.join(size)}")
                say("RESULT: camera reachable - the dashboard relay can be built on this.")
                return 0
            say("RESULT: printer answered but refused the stream "
                + ("(access code rejected?)" if c in (401, 403) else f"(RTSP {c})"))
            return 3
    except (OSError, ssl.SSLError, ConnectionError, ValueError, IndexError) as e:
        say(f"RESULT: failed during the handshake: {type(e).__name__}: {e}")
        say("  (Forum reports say the stream can be slow to start right after enabling Liveview - try again in a minute.)")
        return 4


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "X1C"))
