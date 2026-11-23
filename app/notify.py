"""Notifications (2026-11-17): alerts to people's phones / chats through Apprise (ntfy, Telegram, Discord, email,
Pushover, ...). Each person runs their own service and pastes its address; nothing is hosted for them.

Events: started, finished, paused (needs attention), failed (failed / error / cancelled), offline (went offline
mid-print and stayed offline OFFLINE_GRACE_S).

Who gets what - three layers of checkboxes, each only stored where it was changed:
  defaults[event]  ->  person["all"][event]  ->  person["printers"][printer_id][event]
so a person follows the defaults (and later changes to them) except where a box was ticked/unticked for them, and
a printer row follows the person's "all printers" row except where it was changed.

No repeats: one alert per state entry. Paused alerts again only after the printer left Paused; offline only after
it came back online. The last state seen is saved, so a dashboard restart doesn't re-send.

Secrets: notification addresses contain tokens. They stay on the server; the API only ever returns a masked label.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from .models import ConnState, JobState, PrinterStatus

log = logging.getLogger(__name__)

EVENTS = ("finished", "paused", "failed", "offline", "started")
DEFAULTS = {"finished": True, "paused": True, "failed": True, "offline": True, "started": False}
ACTIVE = {JobState.PRINTING, JobState.PREPARING, JobState.PAUSED}
IDLE_STATES = {JobState.IDLE.value, JobState.FINISHED.value, JobState.UNKNOWN.value}
GONE = {ConnState.OFFLINE, ConnState.ERROR}
IDLE_ERROR_COOLDOWN_S = 30 * 60
OFFLINE_GRACE_S = 120          # a printer that drops for a moment (Wi-Fi blip, reconnect) isn't worth an alert
TICK_S = 15
MAX_PER_MIN = 30               # safety valve against a misbehaving printer flooding everyone
MAX_PEOPLE = 20
MAX_URLS = 5
MAX_URL_LEN = 1000
SETTINGS_KEY = "notify"
SEEN_KEY = "notify_seen"
SEND_TIMEOUT_S = 30

# Services people can pick (Apprise URL schemes). Generic webhooks are included for Home Assistant and similar.
ALLOWED_SCHEMES = {
    "ntfy", "ntfys", "tgram", "discord", "mailto", "mailtos", "pover", "bark", "barks", "gotify", "gotifys",
    "pbul", "slack", "msteams", "matrix", "matrixs", "signal", "signals", "pushed", "join", "line", "rocket",
    "rockets", "mmost", "mmosts", "json", "jsons", "form", "forms", "ifttt", "simplepush", "spush", "zulip",
    "whatsapp", "apprise", "apprises", "hassio", "hassios", "lametric", "lametrics", "pushplus", "wxteams",
}

TITLES = {"started": "Started", "finished": "Finished", "paused": "Paused", "failed": "Failed",
          "offline": "Offline mid-print"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def scheme_of(url: str) -> str:
    return url.split("://", 1)[0].lower() if "://" in url else ""


def check_url(url: str) -> Optional[str]:
    """Why this address can't be used (None = fine)."""
    url = (url or "").strip()
    if not url:
        return "enter an address"
    if len(url) > MAX_URL_LEN:
        return "that address is too long"
    sch = scheme_of(url)
    if sch not in ALLOWED_SCHEMES:
        return (f'"{sch}://" isn\'t a supported service' if sch else
                "an address starts with the service, e.g. ntfy:// or tgram://")
    try:
        import apprise
        obj = apprise.Apprise.instantiate(url)
        if obj is None or (sch in ("ntfy", "ntfys") and not getattr(obj, "topics", None)):
            return "that address isn't complete for this service - check it against the guide"
    except Exception:  # noqa: BLE001
        return "that address couldn't be read"
    return None


def label_of(url: str) -> str:
    """What the UI shows for a saved address: the service, plus the server for self-hosted ones. Never any part of
    a token, topic, key or password (for ntfy the topic name IS the secret)."""
    name = scheme_of(url)
    try:
        import apprise
        obj = apprise.Apprise.instantiate(url)
        if obj is not None:
            name = getattr(obj, "service_name", None) or name
            host = getattr(obj, "host", None)
            if host and getattr(obj, "mode", None) != "cloud" and "." in str(host) \
                    and scheme_of(url) not in ("tgram", "discord", "pover", "mailto", "mailtos"):
                return f"{name} ({host})"
    except Exception:  # noqa: BLE001
        pass
    return str(name)


def effective(cfg: dict, person: dict, printer_id: str, event: str) -> bool:
    """Defaults -> person's all-printers row -> this printer's row, each only where it was set."""
    v = (person.get("printers") or {}).get(printer_id, {}).get(event)
    if v is not None:
        return bool(v)
    v = (person.get("all") or {}).get(event)
    if v is not None:
        return bool(v)
    return bool({**DEFAULTS, **(cfg.get("defaults") or {})}.get(event))


def _fmt_dur(s: Optional[float]) -> str:
    if not s:
        return ""
    m = int(s // 60)
    return f"{m // 60}h {m % 60:02d}m" if m >= 60 else f"{m}m"


class Notifier:
    def __init__(self, store, name_of: Callable[[str], Optional[str]], dashboard_name: Callable[[], str]):
        self.store = store
        self.name_of = name_of
        self.dashboard_name = dashboard_name
        self._seen: dict[str, dict] = dict(store.get_app_setting(SEEN_KEY) or {})
        self._offline_since: dict[str, float] = {}
        self._online: set[str] = set()                   # printers seen online since the dashboard started
        self._idle_error_at: dict[str, float] = {}
        self._queue: Optional[asyncio.Queue] = None
        self._tasks: list[asyncio.Task] = []
        self._sent_at: deque[float] = deque()
        self.recent: deque[dict] = deque(maxlen=30)      # what was sent / failed, for the settings screen

    # ------------------------------------------------------------------ settings
    def config(self) -> dict:
        c = self.store.get_app_setting(SETTINGS_KEY) or {}
        c.setdefault("defaults", {})
        c.setdefault("people", [])
        return c

    def _save(self, c: dict) -> None:
        self.store.set_app_setting(SETTINGS_KEY, c)

    def view(self) -> dict:
        """Settings for the browser: addresses masked, nothing secret."""
        c = self.config()
        return {
            "events": list(EVENTS),
            "titles": TITLES,
            "defaults": {**DEFAULTS, **c["defaults"]},
            "people": [{
                "id": p["id"], "name": p["name"],
                "urls": [{"id": u["id"], "label": u["label"]} for u in p.get("urls", [])],
                "all": p.get("all") or {}, "printers": p.get("printers") or {},
            } for p in c["people"]],
            "recent": list(self.recent),
        }

    def set_defaults(self, defaults: dict) -> None:
        c = self.config()
        c["defaults"] = {**c["defaults"], **{e: bool(defaults[e]) for e in EVENTS if e in defaults}}
        self._save(c)

    def save_person(self, pid: Optional[str], name: str, urls: list[dict], all_: dict,
                    printers: dict[str, dict], known_printers: set[str]) -> dict:
        """Create (pid None) or replace a person. urls: [{"id": existing}] keeps a saved address, [{"url": new}] adds
        one. all_ / printers: only the boxes that were changed (true/false); missing = follow the layer above."""
        name = " ".join((name or "").split())[:40]
        if not name:
            raise ValueError("enter a name")
        c = self.config()
        old = next((p for p in c["people"] if p["id"] == pid), None) if pid else None
        if pid and not old:
            raise KeyError(pid)
        if not old and len(c["people"]) >= MAX_PEOPLE:
            raise ValueError(f"at most {MAX_PEOPLE} people")
        saved = {u["id"]: u for u in (old or {}).get("urls", [])}
        out_urls = []
        for u in urls[:MAX_URLS + 1]:
            if u.get("id"):
                if u["id"] not in saved:
                    raise ValueError("an address was removed meanwhile - reload and try again")
                out_urls.append(saved[u["id"]])
            else:
                url = (u.get("url") or "").strip()
                why = check_url(url)
                if why:
                    raise ValueError(why)
                out_urls.append({"id": secrets.token_hex(6), "url": url, "label": label_of(url)})
        if len(out_urls) > MAX_URLS:
            raise ValueError(f"at most {MAX_URLS} addresses per person")
        clean = lambda d: {e: bool(v) for e, v in (d or {}).items() if e in EVENTS and v is not None}  # noqa: E731
        person = {
            "id": pid or secrets.token_hex(6), "name": name, "urls": out_urls, "all": clean(all_),
            "printers": {k: v for k, v in ((p, clean(d)) for p, d in (printers or {}).items() if p in known_printers)
                         if v},
        }
        c["people"] = [person if p["id"] == person["id"] else p for p in c["people"]] if old else \
            c["people"] + [person]
        self._save(c)
        return person

    def delete_person(self, pid: str) -> bool:
        c = self.config()
        n = len(c["people"])
        c["people"] = [p for p in c["people"] if p["id"] != pid]
        self._save(c)
        return len(c["people"]) < n

    def forget_printer(self, printer_id: str) -> None:
        """Printer removed: drop its rows and its remembered state."""
        c = self.config()
        for p in c["people"]:
            (p.get("printers") or {}).pop(printer_id, None)
        self._save(c)
        if self._seen.pop(printer_id, None) is not None:
            self.store.set_app_setting(SEEN_KEY, self._seen)
        self._offline_since.pop(printer_id, None)

    # ------------------------------------------------------------------ events
    def observe(self, st: PrinterStatus) -> None:
        """Every status update. Paused / error-without-a-job / offline-mid-print; job start/end come from history."""
        pid = st.printer_id
        rec = self._seen.get(pid)
        if st.connection == ConnState.ONLINE:
            self._online.add(pid)
            self._offline_since.pop(pid, None)
            if st.state == JobState.UNKNOWN:
                return                                   # in-between report: not a change
            if rec is None:                              # first time seen (or after a restart with no record)
                self._remember(pid, state=st.state.value, offline_sent=False)
                return
            rec = dict(rec)
            was = rec.get("state")
            if rec.get("offline_sent"):                  # back online: an offline alert can go out again later
                rec["offline_sent"] = False
                self._remember(pid, **rec)
            if was == st.state.value:
                return
            if st.state == JobState.PAUSED:
                self.emit(pid, "paused", job=st.job.name or st.job.file_name, reason=st.reason)
            elif st.state == JobState.ERROR and was in IDLE_STATES:
                # An error during a job is reported once, as the job's end ("failed"). This is a printer that was
                # idle; one alert per IDLE_ERROR_COOLDOWN_S so an error that comes and goes can't spam.
                last = self._idle_error_at.get(pid)
                if last is None or time.monotonic() - last >= IDLE_ERROR_COOLDOWN_S:
                    self._idle_error_at[pid] = time.monotonic()
                    self.emit(pid, "failed", reason=st.reason or "printer error")
            self._remember(pid, **{**rec, "state": st.state.value, "offline_sent": False})
        elif (st.connection in GONE and pid in self._online and rec and not rec.get("offline_sent")
              and rec.get("state") in {s.value for s in ACTIVE}):
            # Only a printer this dashboard saw printing and then lost. Not "busy" (Elegoo slots full), not a wrong
            # access code, and not one that was printing when the dashboard stopped and is off now.
            self._offline_since.setdefault(pid, time.monotonic())

    def check_offline(self) -> None:
        now = time.monotonic()
        for pid, since in list(self._offline_since.items()):
            if now - since >= OFFLINE_GRACE_S:
                del self._offline_since[pid]
                rec = self._seen.get(pid) or {}
                if rec.get("offline_sent"):
                    continue
                self.emit(pid, "offline", job=rec.get("job"))
                self._remember(pid, **{**rec, "offline_sent": True})

    def on_job_event(self, ev: dict) -> None:
        pid = ev.get("printer_id")
        if not pid:
            return
        if ev["type"] == "job_started":
            self.emit(pid, "started", job=ev.get("job"))
            rec = self._seen.get(pid) or {}
            self._remember(pid, **{**rec, "job": ev.get("job")})
        elif ev["type"] == "job_ended":
            result = ev.get("result")
            if result == "finished" or result == "unknown":
                self.emit(pid, "finished", job=ev.get("job"), took=ev.get("print_seconds"),
                          note=None if result == "finished" else "ended")
            elif result in ("failed", "cancelled"):
                self.emit(pid, "failed", job=ev.get("job"), took=ev.get("print_seconds"), reason=ev.get("reason"),
                          note="was cancelled" if result == "cancelled" else None)

    def _remember(self, pid: str, **rec) -> None:
        if self._seen.get(pid) != rec:
            self._seen[pid] = rec
            try:
                self.store.set_app_setting(SEEN_KEY, self._seen)
            except Exception:  # noqa: BLE001
                log.warning("notifications: could not save state")

    # ------------------------------------------------------------------ sending
    def message(self, pid: str, event: str, job=None, reason=None, took=None, note=None) -> tuple[str, str]:
        printer = self.name_of(pid) or "Printer"
        if event == "failed" and note == "was cancelled":
            title = f"{printer}: Cancelled"
        elif event == "finished" and note == "ended":
            title = f"{printer}: Print ended"
        else:
            title = f"{printer}: {TITLES[event]}"
        lines = []
        if job:
            lines.append(str(job))
        if reason:
            lines.append(str(reason))
        if took:
            lines.append(f"Print time {_fmt_dur(took)}")
        if event == "offline":
            lines.append(f"Not reachable for {OFFLINE_GRACE_S // 60} minutes while it was printing.")
        lines.append(self.dashboard_name())
        return title, "\n".join(lines)

    def emit(self, pid: str, event: str, **kw) -> None:
        c = self.config()
        urls = [u["url"] for p in c["people"] if effective(c, p, pid, event) for u in p.get("urls", [])]
        if not urls:
            return
        title, body = self.message(pid, event, **kw)
        self._enqueue(title, body, urls, event, pid)

    def _enqueue(self, title: str, body: str, urls: list[str], event: str, pid: Optional[str]) -> None:
        now = time.monotonic()
        while self._sent_at and now - self._sent_at[0] > 60:
            self._sent_at.popleft()
        if len(self._sent_at) >= MAX_PER_MIN:
            log.warning("notifications: more than %d a minute - dropping %s", MAX_PER_MIN, title)
            return
        self._sent_at.append(now)
        if self._queue is None:
            return                                       # not started (tests without a loop)
        try:
            self._queue.put_nowait({"title": title, "body": body, "urls": urls, "event": event, "printer_id": pid})
        except asyncio.QueueFull:
            log.warning("notifications: send queue full - dropping %s", title)

    async def send_now(self, title: str, body: str, urls: list[str]) -> tuple[bool, Optional[str]]:
        """Send one message to these addresses (in a thread: Apprise is blocking). (ok, error)"""
        def go() -> bool:
            import apprise
            a = apprise.Apprise()
            for u in urls:
                a.add(u)
            return bool(a.notify(title=title, body=body))
        try:
            ok = await asyncio.wait_for(asyncio.to_thread(go), SEND_TIMEOUT_S)
            return ok, None if ok else "the service didn't accept it - check the address"
        except asyncio.TimeoutError:
            return False, "the service didn't answer in time"
        except Exception as e:  # noqa: BLE001
            return False, type(e).__name__

    async def test(self, person_id: str, url_id: Optional[str] = None) -> tuple[bool, Optional[str]]:
        c = self.config()
        p = next((x for x in c["people"] if x["id"] == person_id), None)
        if not p:
            raise KeyError(person_id)
        urls = [u["url"] for u in p.get("urls", []) if url_id in (None, u["id"])]
        if not urls:
            raise ValueError("no address to test")
        return await self.send_now(f"{self.dashboard_name()}: test",
                                   f"Notifications for {p['name']} work.", urls)

    async def _worker(self) -> None:
        assert self._queue is not None
        while True:
            m = await self._queue.get()
            ok, err = await self.send_now(m["title"], m["body"], m["urls"])
            self.recent.appendleft({"at": _now().isoformat(timespec="seconds"), "title": m["title"],
                                    "ok": ok, "error": err})
            if not ok:
                log.warning("notification %r not delivered: %s", m["title"], err)

    async def _ticker(self) -> None:
        while True:
            await asyncio.sleep(TICK_S)
            try:
                self.check_offline()
            except Exception:  # noqa: BLE001
                log.exception("notifications: offline check failed")

    def start(self) -> None:
        self._queue = asyncio.Queue(maxsize=200)
        self._tasks = [asyncio.create_task(self._worker()), asyncio.create_task(self._ticker())]

    def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        self._tasks = []
