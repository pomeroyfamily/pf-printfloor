# Notifications

PF PrintFloor can tell people when a printer **finishes**, **pauses** (needs attention, e.g. filament ran out),
**fails** (failed, error or cancelled), goes **offline mid-print**, or **starts** a print.

Alerts go to each person's own app or chat. **Each person sets up their own service**, for free in most cases, and
pastes one address into the dashboard. Nothing extra to install on the dashboard's computer.

Behind the scenes the dashboard uses [Apprise](https://github.com/caronc/apprise), so dozens of services work. The
common ones are below.

## 1. Pick a service and get an address

### ntfy (free, no account) - the easiest

1. Install the **ntfy** app ([iPhone](https://apps.apple.com/us/app/ntfy/id1625396347),
   [Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy)).
2. Make up a **long, random topic name**, e.g. `pf-danny-8f3k2q9xa1`. Anyone who knows the name can read the alerts,
   so treat it like a password.
3. In the app: **+** → enter the topic → **Subscribe**.
4. Your address is `ntfy://` + the topic: `ntfy://pf-danny-8f3k2q9xa1`

Using your own ntfy server instead: `ntfys://ntfy.example.com/your-topic` (add `token@` before the host if your
server needs an access token: `ntfys://tk_xxxxxxxx@ntfy.example.com/your-topic`).

### Telegram (free)

1. In Telegram, message **@BotFather** → `/newbot` → follow the steps. It gives you a **bot token** like
   `123456789:AAH...`.
2. Send your new bot any message (so it's allowed to message you).
3. Get your **chat ID**: message **@userinfobot**; it replies with your ID.
4. Your address: `tgram://<bot token>/<chat id>`, e.g. `tgram://123456789:AAHxxxx/987654321`

### Discord (free)

1. In your Discord server: channel **Edit Channel** → **Integrations** → **Webhooks** → **New Webhook** → **Copy
   Webhook URL**. It looks like `https://discord.com/api/webhooks/<id>/<token>`.
2. Your address: `discord://<id>/<token>`

### Email

`mailtos://you:app-password@gmail.com` (Gmail needs an [app password](https://myaccount.google.com/apppasswords),
not your normal one). Other providers: see the [Apprise email page](https://github.com/caronc/apprise/wiki/Notify_email).

### Pushover (one-time purchase after a 30-day trial)

From your [Pushover dashboard](https://pushover.net/): your **user key**, and an **application token** (Create an
Application). Address: `pover://<user key>@<app token>`

### Bark (free, iPhone only)

Install **Bark**, copy the key it shows. Address: `barks://api.day.app/<key>`

### Others

Slack, Microsoft Teams, Matrix, Signal, Gotify, Pushbullet, Home Assistant, a generic JSON webhook and more: find the
service on the [Apprise wiki](https://github.com/caronc/apprise/wiki) and use the address format shown there.
Plain `http://` / `https://` addresses aren't accepted; use the service's own form (e.g. `json://` / `jsons://` for a
webhook).

## 2. Add people in the dashboard

Gear menu → **Notifications**.

1. **Defaults** (top): which events a new person gets. Out of the box: Finished, Paused, Failed and Offline
   mid-print on; Started off.
2. **Add person** → type a name → **Add person**.
3. Paste their address → **Add address** → **Send test**. A test message should arrive within seconds. A person can
   have several addresses (e.g. ntfy and email); each gets every alert for that person.
4. Choose what they get in their grid (below). Changes save right away.

Addresses are stored on the dashboard server only. The screen shows just the service and the last few characters
(e.g. `ntfy (…9xa1)`); the full address is never sent back to the browser. To change an address, remove it and add
the new one.

## 3. Who gets what: the grid

Each person has a grid: **printers down the side, events across the top**, with an **All printers** row on top.

```
                     Finished  Paused  Failed  Offline  Started
All printers            ☑        ☑       ☑       ☑        ☐      <- this person's starting point
  A1 mini-2             ☐        ☐       ☐       ☐        ☐      <- changed: nothing from this printer
  X1C                   ·        ·       ·       ·        ☑      <- follows the row above, plus Started
```

- **Faint** boxes follow the layer above: a printer row follows the person's **All printers** row, which follows the
  **Defaults**. Changing a default later reaches everyone who hasn't changed that box.
- **Click** a box to change it for that row (it turns solid). Click it back and it follows again.
- **All** / **None** set a whole row; **Reset** makes a changed row follow again.

Examples:
- *Bethany only wants to know when the A1 mini-2 is done or paused:* her **All printers** row → **None**; A1 mini-2 row →
  tick Finished and Paused.
- *You don't want to hear about the A1 mini-2 at all:* your A1 mini-2 row → **None**.
- *"Started" only for the X1C:* leave Started off in your All printers row, tick it in the X1C row.

## What the alerts say

The title is the printer and what happened (`X1C: Finished`, `P1-2: Paused`, `U1-3: Cancelled`); the message has the
job name, the reason when the printer gives one (e.g. "Filament ran out"), the print time for finished / failed jobs,
and the dashboard's name.

## When alerts are sent

- **One alert per change.** A printer that stays paused doesn't alert again; it alerts again only if it resumes and
  pauses again.
- **Finished / Failed** are sent once when the job ends. An error during a print is reported once, as Failed.
  **Cancelled** jobs are sent as Failed ("Cancelled"), so you know someone stopped it.
- **Offline mid-print** waits **2 minutes**, so a short Wi-Fi blip isn't reported. An idle printer turning off is
  never an alert.
- **Restarting the dashboard** doesn't re-send anything.
- At most 30 alerts a minute are sent in all, so a misbehaving printer can't flood everyone.
- The last few alerts (sent or failed) are listed at the bottom of the Notifications window.

## Troubleshooting

| What you see | What to do |
|---|---|
| "that address isn't complete for this service" | Compare it with the format above; ntfy needs a topic after `ntfy://`. |
| "isn't a supported service" | Use the service's own form (e.g. `ntfy://`, `tgram://`, `jsons://`), not a plain `https://` link. |
| "Not sent: the service didn't accept it" | The token / key / topic is wrong or expired. Get a fresh one and add it again. |
| Test works, but no alerts | Check that person's grid for that printer and event. Alerts are only sent on a change. |
| Nothing arrives on an iPhone with ntfy | In the ntfy app, make sure you're subscribed to exactly the same topic and notifications are allowed in iOS Settings. |
