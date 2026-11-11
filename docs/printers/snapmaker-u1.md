# Snapmaker U1

Driver `snapmaker.u1`. Connects to Moonraker over HTTP (port 80). Local network only.

## Camera: stock firmware vs. extended firmware

| | Stock firmware | Extended firmware (paxx12) |
|---|---|---|
| Card and camera wall | Still pictures, about 1–3 per second | Still pictures, about 3 per second, 1080p |
| Popup | Same pictures | Same, plus **Smooth video** |
| Setup | None | Install the firmware on the printer |

- **Stock:** the U1 only makes camera pictures while it's asked for them every few seconds. The dashboard does
  that only while a camera is open.
- **Extended firmware:** the community
  [Snapmaker U1 Extended Firmware](https://github.com/paxx12-snapmaker-u1/SnapmakerU1-Extended-Firmware)
  by paxx12 adds a real camera stream. The dashboard finds it by itself (no setting), and the camera popup gets a
  **Smooth video** button: full frame-rate WebRTC video straight from the printer, **home network only** (it
  doesn't go through the dashboard, so it doesn't work over a remote connection to the dashboard).
- The extended firmware is not made by Snapmaker or by this project. Read its instructions and keep the stock
  firmware file so you can go back.

**If extended-firmware pictures come back empty** (black or no picture) after installing it: in the printer's
camera settings set the camera to **Internal**, save, then set it back to **Paxx12** and save.

## Status

- Polled every 3 seconds. Shows Offline about 10 seconds after it disappears.
- **Pause reasons** come from the printer (for example "Paused: filament tangled (Toolhead 2)") and are only shown
  while it's paused or in error, because the U1 can keep the message after resuming.
- **Name:** "Use the printer's name" follows Moonraker's device name, or the host name if there is none.
- **Uptime:** real uptime from Moonraker, read every 5 minutes.
