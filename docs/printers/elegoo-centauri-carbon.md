# Elegoo Centauri Carbon

Driver `elegoo.cc1`. Connects over the printer's local SDCP WebSocket (port 3030). Local network only: Elegoo has
no cloud connection the dashboard can use, so a Centauri Carbon at another location needs a VPN (for example
Tailscale) to that network.

## Camera

- **Built in, about 10 frames per second at 640×360** (MJPEG). Nothing extra to set up.
- The camera stream is **switched on when someone opens it** and switched off again 10 seconds after the last
  viewer leaves. The dashboard holds **one** stream per printer and shares it with every viewer (cards, popup,
  camera wall), so it uses one of the printer's slots no matter how many people watch.
- **The printer only allows a few viewers at a time** (4 on the printer we tested). Elegoo's slicer and the
  Elegoo app each take a slot while their camera view is open.
- When all slots are taken, the dashboard says **"the printer's viewer limit is reached"**. Close the camera view
  in the Elegoo slicer or app, then open the camera in the dashboard again.

## Status

- Polled every 15 seconds. Shows Offline about 20 seconds after it disappears.
- **Pause reasons aren't available.** The printer doesn't say why it paused over the network (checked during a
  real "filament or spool may be jammed" pause), so the card just says "Paused".
- **Canvas filament** is read every 5 minutes while idle and every 2 minutes while printing (turn the printing
  reads off with the `canvas_while_printing` setting). Between reads the last values are shown.
- **Name:** "Use the printer's name" follows the name the printer reports.

## Uptime and reboots

The printer reports no uptime. After power-on its position reads exactly `0,0,0` until it moves; homing gives a
real position. Moved or homed → `0,0,0` is treated as a reboot, dated when the dashboard noticed. A reboot
followed by a move before the next poll (15 s) is missed.
