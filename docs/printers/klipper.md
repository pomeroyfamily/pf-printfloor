# Generic Klipper (Moonraker)

Driver `klipper.generic`. For any Klipper printer with Moonraker (Fluidd, Mainsail). Connects over HTTP on the
local network.

- **Camera:** none built in. Add an external camera URL in Details → Camera: an MJPEG stream, a snapshot URL, or
  auto. The webcam your Fluidd / Mainsail already shows usually works (its stream or snapshot address).
- **Pause reasons:** shown when the printer reports one.
- **Name:** "Use the printer's name" follows Moonraker's device name, or the host name.
- **Uptime:** real uptime from Moonraker, read every 5 minutes.

The Snapmaker U1 has its own driver: [snapmaker-u1.md](snapmaker-u1.md).
