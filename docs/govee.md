# Govee smart plugs

PF PrintFloor can switch a printer's Govee Wi-Fi smart plug: on and off from the dashboard, an **emergency stop**,
and an optional **daily power cycle**. Everything else on the dashboard stays read-only.

Tested with the Govee **H5086** plug. Other Govee plugs that the Govee cloud API can switch should work too.

## 1. Get a Govee API key

Govee has no "sign in with Govee" for other apps. Each Govee account requests its own key:

1. In the **Govee Home** app on your phone: the person tab (bottom right) → gear icon (top) →
   **Apply for API Key**.
2. Enter your name and a reason (for example "local printer dashboard") and submit.
3. Govee emails you the key. It can take a while to arrive.

> **One key per account.** Since May 2026, getting a new key turns off your older keys right away. If something
> else already uses your Govee key (Home Assistant, a script), reuse that key here, or update those apps after
> you get a new one.

## 2. Link it to the dashboard

1. Gear menu (top right) → **Govee plugs**.
2. Paste the key → **Link Govee account**.
3. The window now lists every plug on your Govee account. If a plug is missing, check it is set up in the Govee
   Home app and online.

The key is stored on the dashboard server only (in its data folder), never sent to the browser and never shown
again.

## 3. Tell the dashboard which plug powers which printer

Right in the same **Govee plugs** window:

1. Each plug has a **Printer** list. Pick the printer it powers, or leave it **Not used** (lamps, other devices).
2. Plugs named like a printer are filled in for you and marked **suggested** (plug `X1` → printer `X1C`,
   plug `A1` → printer `A1-1`). Check them; naming your plugs like your printers in the Govee Home app saves time.
3. Click **Save**. Nothing changes until you do. One printer can only have one plug; picking it twice is flagged.

You can also set or change a single printer's plug in its **Details → Power** (**Change plug**).

## 4. Using it

- **Power icon on the card**: green = plug on, red = off, yellow = plug offline (unplugged or off Wi-Fi) or not
  known yet. The Govee plugs window shows **Offline** next to such a plug. Click it for the
  power window (same controls as Details → Power).
- **Turn off** asks for a second click. While the printer is printing, paused, receiving a file, updating
  firmware, drying filament or has a hot nozzle (> 50 °C), it becomes an **emergency stop**: you have to type the
  printer's name, and the dashboard tells you why it's risky.
- A plug that Govee reports **offline** (unplugged or off Wi-Fi) isn't switched: the dashboard checks first and
  says so.
- **Turn on** waits 15 seconds after a turn off, so the printer's power supply can discharge.
- After every switch the dashboard **reads the plug back** to confirm it, and re-sends once if needed. If Govee
  won't confirm, it says "unconfirmed": check the plug or the Govee app.
- Every on/off is logged in Details → Power → Recent.

## 5. Optional: daily power cycle

Some printers run better after a daily restart. The dashboard can do it for you: plug **off for 15 seconds, then
on again**, once a day.

**Turn it on** for a printer either:
- in the **Govee plugs** window: the **Daily power cycle** switch next to the plug, then **Save**, or
- in the printer's **Details → Power → Power cycle once a day**.

**Defaults:** at **6:00 AM**, once the printer has been Finished or Idle for **30 minutes**. Change both for each
printer in **Details → Power** (*Daily at* and *After idle for (minutes)*) and click **Save** there.

It only power cycles when it's safe and useful:
- the printer is **Finished or Idle**, with **a print since the last power cycle** (an unused printer is left
  alone),
- the nozzle is cool and nothing else is running (no file transfer, firmware update or filament drying),
- the plug is on (a plug that's off is never switched on),
- the printer hasn't restarted in the 12 hours before the daily time (a dashboard power cycle, or its own reboot
  where the printer reports it),
- if a print runs past the set time, it waits until the print is done (after more than 24 hours since the last
  restart, it runs as soon as the printer is finished and idle long enough).

Turning it on doesn't cycle right away: the first one is at the first daily time at least 12 hours later.

While a power cycle is coming up, the card shows a countdown, with **Pause** and **Restart countdown**. The full
rules are in the README under [Automatic power cycle](../README.md#automatic-power-cycle).

## How often it talks to Govee

Govee allows about 10,000 requests per account per day.

- A plug's state is read every 5 minutes while its printer is **not** online. While the printer is online its
  plug is taken to be on, and the plug is read every 30 minutes to catch one that's unplugged or offline.
- Plugs are also read when you open the **Govee plugs** window (all of them), assign a plug, open a printer's
  power window or Details, and right before turning a plug on or off (at most once a minute per plug, except
  the read before a switch).
- The plug list is refreshed a few times a day.
- Background checks stop for the day after 3,000 of the dashboard's own requests, or when Govee says fewer than
  2,000 are left, so turning plugs on and off always has room. The **Govee plugs** window shows today's count.

## Troubleshooting

| What you see | What to do |
|---|---|
| "Govee rejected that API key" when linking | Copy the key again from Govee's email. If you requested a newer key since, only the newest works. |
| It worked before, now every plug fails | A new key was generated for the account (for example by another app) and turned this one off. Unlink, then link the newest key. |
| A plug isn't in the list | Set it up in the Govee Home app and make sure it's online. Unlink and link again to reload the list right away. |
| Power icon stays yellow, or the plug says **Offline** | Govee reports the plug offline: it's unplugged or off Wi-Fi (the Govee Home app shows the same). If the printer is online anyway, it's running from another outlet. A plug that hasn't been read yet is also yellow for up to 5 minutes. |
| "Unconfirmed" after on/off | Govee took the command but wouldn't report the plug's state. Check the plug or the Govee app. |

To remove the key: Govee plugs → **Unlink Govee**. Each printer keeps its plug choice, so linking the same Govee
account again brings everything back. To clear a printer's plug: Details → Power → Change plug → **No plug**.
