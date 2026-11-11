"""Optional Bambu Lab account link: job cover images for cloud-started prints.

Not an official API. Endpoints come from community clients (docs/SPEC.md "Bambu cloud"):
  POST {api}/v1/user-service/user/login           {account, password}  -> accessToken | loginType
  POST {api}/v1/user-service/user/sendemail/code  {email, type: "codeLogin"}
  POST {api}/v1/user-service/user/login           {account, code}      -> accessToken
  POST {web}/api/sign-in/tfa                      {tfaKey, tfaCode}    (authenticator-app accounts)
       needs GET {web}/api/csrf first (204, cookie bbl_csrf_token), then the token sent back both as
       cookie and as header x-bbl-csrf-token; without the header: 403 "CSRF error: missing_header"
       (verified on a real account 2026-10-29). Session token comes back as cookie `token`.
  GET  {api}/v1/user-service/my/tasks?limit=N&deviceId=SERIAL   Bearer token -> {hits: [task]}
  GET  {api}/v1/iot-service/api/user/bind          Bearer token -> {devices: [...]}  printers bound to the
       account (dev_id, name, dev_model_name, dev_access_code, online - verified on a real account 2026-10-30
       except `online`, which community clients read)
  GET  {api}/v1/design-user-service/my/preference  Bearer token -> {uid: N}  (cloud MQTT username = "u_<uid>")
Cloud MQTT (printers away from the LAN): TLS 8883 at us.mqtt.bambulab.com (cn.mqtt.bambulab.com for China),
username "u_<uid>", password = the account token, same topics/messages as the printer's own MQTT
(device/<serial>/report, device/<serial>/request). [secondary: openHAB Bambu binding docs, ha-bambulab]
A task's `cover` is a short-lived signed URL: download it right away, no auth header.

Read-only: nothing here starts, stops or changes anything on the account or the printers.
Uses only the standard library (blocking - call from asyncio.to_thread).
"""
from __future__ import annotations

import base64
import http.cookiejar
import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any, Optional

log = logging.getLogger(__name__)

MQTT_HOSTS = {"global": "us.mqtt.bambulab.com", "china": "cn.mqtt.bambulab.com"}
REGIONS = {
    "global": ("https://api.bambulab.com", "https://bambulab.com"),
    "china": ("https://api.bambulab.cn", "https://bambulab.cn"),
}
USER_AGENT = "bambu_network_agent/01.09.05.01"   # what Bambu's own network plugin sends


class CloudError(Exception):
    pass


class TokenExpired(CloudError):
    """Token rejected (401/403): the account must be linked again."""


class NeedsEmailCode(CloudError):
    """Login accepted the password but wants a 6-digit code sent to the account email."""


class NeedsTfa(CloudError):
    """Account uses an authenticator app: finish with login_tfa(tfa_key, code)."""

    def __init__(self, tfa_key: str):
        super().__init__("authenticator code required")
        self.tfa_key = tfa_key


def token_expiry(token: str) -> Optional[float]:
    """Unix time the token expires, if it is a JWT with an exp claim."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        exp = json.loads(base64.urlsafe_b64decode(payload)).get("exp")
        return float(exp) if exp else None
    except (IndexError, ValueError, TypeError):
        return None


class BambuCloud:
    def __init__(self, token: Optional[str] = None, region: str = "global",
                 api_base: Optional[str] = None, web_base: Optional[str] = None, timeout: float = 15):
        api, web = REGIONS.get(region, REGIONS["global"])
        self.api, self.web = (api_base or api).rstrip("/"), (web_base or web).rstrip("/")
        self.region, self.token, self.timeout = region, token, timeout
        self._jar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self._jar))

    # ------------------------------------------------------------------ http
    def _req(self, method: str, url: str, body: Optional[dict] = None, auth: bool = False,
             headers: Optional[dict] = None) -> Any:
        h = {"User-Agent": USER_AGENT, "Accept": "application/json", **(headers or {})}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        if auth:
            if not self.token:
                raise TokenExpired("not logged in")
            h["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(url, data=data, headers=h, method=method)
        try:
            with self._opener.open(req, timeout=self.timeout) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            detail = e.read()[:300].decode("utf-8", "replace")
            if auth and e.code in (401, 403):
                raise TokenExpired(f"HTTP {e.code}: {detail}") from e
            raise CloudError(f"HTTP {e.code} from {url}: {detail}") from e
        except (urllib.error.URLError, OSError) as e:
            raise CloudError(f"cannot reach {url}: {e}") from e
        try:
            return json.loads(raw) if raw else {}
        except ValueError as e:
            raise CloudError(f"unexpected response from {url}: {raw[:200]!r}") from e

    def _cookie(self, name: str) -> Optional[str]:
        return next((c.value for c in self._jar if c.name == name), None)

    def _take_token(self, resp: dict) -> str:
        tok = resp.get("accessToken") or resp.get("token") or self._cookie("token")
        if not tok:
            raise CloudError(f"login response had no token: {str(resp)[:200]}")
        self.token = tok
        return tok

    # ------------------------------------------------------------------ login
    def login(self, account: str, password: str) -> str:
        """Returns the token, or raises NeedsEmailCode / NeedsTfa for the second step."""
        r = self._req("POST", f"{self.api}/v1/user-service/user/login",
                      {"account": account, "password": password, "apiError": ""})
        kind = r.get("loginType")
        if kind == "verifyCode":
            self.send_email_code(account)
            raise NeedsEmailCode("a verification code was sent to the account email")
        if kind == "tfa":
            raise NeedsTfa(r.get("tfaKey", ""))
        if r.get("accessToken"):
            return self._take_token(r)
        raise CloudError(r.get("message") or r.get("error") or "login failed")

    def send_email_code(self, email: str) -> None:
        self._req("POST", f"{self.api}/v1/user-service/user/sendemail/code",
                  {"email": email, "type": "codeLogin"})

    def login_with_code(self, email: str, code: str) -> str:
        r = self._req("POST", f"{self.api}/v1/user-service/user/login", {"account": email, "code": code.strip()})
        if not r.get("accessToken"):
            raise CloudError(r.get("message") or "code rejected")
        return self._take_token(r)

    def login_tfa(self, tfa_key: str, code: str) -> str:
        """Authenticator-app second step (web sign-in)."""
        self._req("GET", f"{self.web}/api/csrf")
        csrf = self._cookie("bbl_csrf_token")
        if not csrf:
            raise CloudError("Bambu did not return a CSRF token (bbl_csrf_token cookie)")
        r = self._req("POST", f"{self.web}/api/sign-in/tfa", {"tfaKey": tfa_key, "tfaCode": code.strip()},
                      headers={"x-bbl-csrf-token": csrf})
        return self._take_token(r if isinstance(r, dict) else {})

    # ------------------------------------------------------------------ data
    def tasks(self, device_id: Optional[str] = None, limit: int = 20) -> list[dict]:
        q = f"limit={limit}" + (f"&deviceId={device_id}" if device_id else "")
        r = self._req("GET", f"{self.api}/v1/user-service/my/tasks?{q}", auth=True)
        hits = r.get("hits") if isinstance(r, dict) else None
        return hits if isinstance(hits, list) else []

    def devices(self) -> list[dict]:
        r = self._req("GET", f"{self.api}/v1/iot-service/api/user/bind", auth=True)
        devs = r.get("devices") if isinstance(r, dict) else None
        return devs if isinstance(devs, list) else []

    def user_id(self) -> str:
        """Account uid for the cloud MQTT username. JWT claim if the token carries it, else the preference call."""
        try:
            payload = (self.token or "").split(".")[1]
            payload += "=" * (-len(payload) % 4)
            claims = json.loads(base64.urlsafe_b64decode(payload))
            for k in ("uid", "user_id", "userId"):
                if str(claims.get(k) or "").isdigit():
                    return str(claims[k])
            if str(claims.get("username") or "").startswith("u_"):
                return str(claims["username"])[2:]
        except (IndexError, ValueError, TypeError, AttributeError):
            pass
        r = self._req("GET", f"{self.api}/v1/design-user-service/my/preference", auth=True)
        uid = r.get("uid") if isinstance(r, dict) else None
        if not uid:
            raise CloudError("the account did not return a user id (needed for the cloud connection)")
        return str(uid)

    def download(self, url: str, max_bytes: int = 10 * 1024 * 1024) -> bytes:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.read(max_bytes)
        except (urllib.error.URLError, OSError) as e:
            raise CloudError(f"cover download failed: {e}") from e


def find_task(tasks: list[dict], serial: Optional[str], task_id: Optional[str],
              subtask_name: Optional[str]) -> Optional[dict]:
    """The cloud task for the printer's current job: same task id, else newest same-name on that printer."""
    mine = [t for t in tasks if not serial or str(t.get("deviceId", "")) == serial]
    if task_id and str(task_id) not in ("", "0"):
        for t in mine:
            if str(t.get("id")) == str(task_id):
                return t
    if subtask_name:
        same = [t for t in mine if subtask_name in (t.get("title"), t.get("designTitle"))]
        if same:
            return max(same, key=lambda t: str(t.get("startTime") or ""))
    return None


def is_image(data: bytes) -> bool:
    return data[:8] == b"\x89PNG\r\n\x1a\n" or data[:3] == b"\xff\xd8\xff" or \
        (data[:4] == b"RIFF" and data[8:12] == b"WEBP")


# ---------------------------------------------------------------------- account (process-wide)
class Account:
    """The linked account the Bambu drivers use. state: 'linked' | 'needs_login'."""

    DEVICES_TTL_S = 60

    def __init__(self, cloud: BambuCloud, email: str):
        self.cloud, self.email = cloud, email
        self.state = "linked"
        self.last_error: Optional[str] = None
        self._uid: Optional[str] = None
        self._devices: tuple[Optional[float], list[dict]] = (None, [])     # (fetched at, list); None = never fetched
        self.mqtt_host_override: Optional[tuple[str, int]] = None     # tests
        self.on_rejected = None     # called (from any thread) once when Bambu stops accepting the token

    def _rejected(self, e: Exception) -> None:
        """Bambu refused the saved sign-in. Remember it (the manager saves it and tells the browsers), so every
        part of the app - and the next start - shows "sign in again" instead of quietly losing cloud features."""
        first = self.state != "needs_login"
        self.state, self.last_error = "needs_login", str(e)
        if first:
            log.warning("Bambu account sign-in rejected - sign in again (%s)", e)
            if self.on_rejected:
                try:
                    self.on_rejected()
                except Exception:  # noqa: BLE001
                    log.exception("saving the rejected Bambu sign-in failed")

    # ---- cloud connection for printers away from the LAN (blocking: call via asyncio.to_thread) ----
    def mqtt_login(self) -> tuple[str, int, str, str]:
        """(host, port, username, password) for Bambu's cloud MQTT."""
        if self.state != "linked" or not self.cloud.token:
            raise TokenExpired("Bambu account needs to be linked again")
        if not self._uid:
            try:
                self._uid = self.cloud.user_id()
            except TokenExpired as e:
                self._rejected(e)
                raise
        host, port = self.mqtt_host_override or (MQTT_HOSTS.get(self.cloud.region, MQTT_HOSTS["global"]), 8883)
        return host, port, f"u_{self._uid}", self.cloud.token

    def device(self, serial: Optional[str], fresh: bool = False) -> Optional[dict]:
        """This printer in the account's printer list (cached a minute; fresh=True asks again), or None if it isn't
        on the account."""
        if not serial or self.state != "linked":
            return None
        at, devs = self._devices
        if fresh or at is None or time.monotonic() - at > self.DEVICES_TTL_S:
            try:
                devs = self.cloud.devices()
            except TokenExpired as e:
                self._rejected(e)
                return None
            except CloudError:
                devs = []
            self._devices = (time.monotonic(), devs)
        return next((d for d in devs if str(d.get("dev_id")) == serial), None)

    def cover_for(self, serial: Optional[str], task_id: Optional[str],
                  subtask_name: Optional[str]) -> tuple[Optional[bytes], str]:
        if self.state != "linked":
            return None, "Bambu account needs to be linked again"
        try:
            task = find_task(self.cloud.tasks(serial, limit=10), serial, task_id, subtask_name)
            if not task:
                return None, f"no cloud task for task_id={task_id} yet"
            url = task.get("cover")
            if not url:
                return None, f"cloud task {task.get('id')} has no cover image"
            data = self.cloud.download(url)
            if not is_image(data):
                return None, f"cloud cover for task {task.get('id')} is not an image"
            return data, f"cloud task {task.get('id')} cover"
        except TokenExpired as e:
            self._rejected(e)
            return None, "Bambu account token expired"


_account: Optional[Account] = None


def set_account(acct: Optional[Account]) -> None:
    global _account
    _account = acct


def get_account() -> Optional[Account]:
    return _account


def account_from_store(store) -> Optional[Account]:
    """Build the Account from the saved link (app setting 'bambu_cloud'), or None if not linked."""
    saved = store.get_app_setting("bambu_cloud")
    if not saved or not saved.get("token"):
        return None
    acct = Account(BambuCloud(saved["token"], saved.get("region", "global")), saved.get("email", ""))
    exp = saved.get("expires")
    if (exp and exp < time.time()) or saved.get("rejected_at"):
        acct.state = "needs_login"
    return acct


def mark_rejected(store) -> None:
    """Remember that Bambu refused the saved sign-in (cleared by signing in again: save_link rewrites it)."""
    saved = dict(store.get_app_setting("bambu_cloud") or {})
    if saved:
        saved["rejected_at"] = time.time()
        store.set_app_setting("bambu_cloud", saved)


def save_link(store, email: str, cloud: BambuCloud) -> None:
    store.set_app_setting("bambu_cloud", {"email": email, "region": cloud.region, "token": cloud.token,
                                          "expires": token_expiry(cloud.token or ""),
                                          "linked_at": time.time()})
