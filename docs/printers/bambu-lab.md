# Bambu Lab (X1 Carbon, P1P, A1, A1 Mini)

Drivers `bambu.x1c`, `bambu.p1p`, `bambu.a1`, `bambu.a1mini`. Connects to the printer over local MQTT (needs the
LAN access code from the printer's screen), or through the Bambu cloud with a linked Bambu account.

## Camera

- **P1P / A1 / A1 Mini:** still pictures, about 0.5–2 per second. These models have no video stream; smooth video
  needs an external camera (Details → Camera → camera URL).
- **X1 Carbon:** sends real video that browsers can't play directly. Showing it needs a relay in the dashboard,
  which is planned. It will need **LAN Mode Liveview** turned on in the printer's network settings (LAN Only Mode
  is not needed).
- While a printer is connected **via cloud**, the camera button is hidden.

## Access code and address

- The access code changes when the printer is set up again (new Wi-Fi, removed and re-added in Bambu Handy). The
  card then shows **Access code rejected**. With a linked Bambu account the dashboard picks up the new code by
  itself; otherwise enter it in Details → Edit.
- A new IP address is followed automatically when it's safe to (same serial number, the old address no longer
  answers, and the new one proves it's that printer). See the README:
  [Bambu printer on a new address](../../README.md#bambu-printer-on-a-new-address).

## Cloud connection

Each Bambu has **Connection: Automatic / This network only / Bambu cloud only** in Details. Automatic switches to the
cloud after 2 minutes without a local connection (when an account is linked) and back when the printer answers
locally again. The printer must stay in normal cloud mode, not LAN Only. Details:
[Bambu cloud connection](../../README.md#bambu-cloud-connection-remote-printers).

## Status

- **P1P / A1** send only changes; a full refresh is asked for at most every 5 minutes (the P1P lags when asked more
  often). The X1C sends full reports.
- **Filament remaining %** is hidden: the local connection reports 0 even when the screen shows a value.
- **Pause reasons** come from the printer's stage or error code. An unknown code shows no text rather than a
  guess.
- **Firmware update available** is shown (confirmed on the X1C). Updates are applied on the printer.
- **Uptime:** not reported. A reboot is detected when the printer's report counter starts over, usually within a
  minute. No uptime is shown until the dashboard has seen the first reboot.
