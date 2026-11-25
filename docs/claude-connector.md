# Claude connector

Lets [Claude](https://claude.ai) look at your dashboard, from any chat on your phone or computer, or from a
scheduled task. Ask things like *"what's printing and when will it be done?"*, *"did anything fail overnight?"* or
*"show me the U1 camera"*.

**Read-only.** Claude can see printer status, job history, print stats and camera snapshots. It can't switch plugs,
change settings, pause, cancel or control a printer in any way.

What Claude can use:

| Tool | What it gives |
|---|---|
| `list_printers` | Every printer: connection, state, reason (when the printer gave one), job, progress, time left, finish time, plug power, location, tags |
| `get_printer` | One printer in full: temperatures, fans, loaded filament, error codes, firmware |
| `recent_jobs` | Recent prints, newest first, with result and print time (all printers or one) |
| `print_stats` | Print hours, job counts, finished vs failed, success rate |
| `camera_snapshot` | A current picture from a printer's camera |

Printer addresses, serial numbers, access codes, API keys and camera URLs are never included in answers.

## 1. Turn it on

**Settings (gear) → Claude connector → Turn on.** You get a **Connector URL**, which looks like
`https://printfloor.example.com/mcp/pfp_…`. Copy it straight away: the token at the end isn't shown again. If
you lose it, press **New token** (the old one stops working at once).

Anyone with the URL can see your printers, so treat it like a password. **Turn off** deletes the token.

## 2. Make it reachable from the internet

Claude connects to your dashboard **from Anthropic's servers**, not from your phone or computer, so the dashboard
must be reachable on the internet by an `https://` address. Claude can't reach a home address such as
`http://192.168.1.50:8765`, or an address with a port number in it.

### Behind Cloudflare Tunnel + Access

Access asks for a sign-in that Claude can't do, so let the connector's path through. Only that path skips
Access; the dashboard's own token protects it, and every other page still asks you to sign in.

**a) An Access application that bypasses `/mcp`**

1. Cloudflare **Zero Trust → Access controls → Applications → Add an application → Self-hosted**.
2. Name it, e.g. *PrintFloor connector*.
3. Add **two** public hostnames, both with your dashboard's **subdomain and domain** filled in (e.g. `printfloor`
   and `example.com`). Path `mcp` on the first, `mcp/*` on the second (a path with `/*` doesn't cover the path
   itself). Leave off the leading `/`; the form shows it.
4. Add a policy: **Action: Bypass**, **Include: Everyone**. Make sure it's listed under this application's
   **Access policies** before saving.
5. Save. Cloudflare uses the more specific application for those paths, and your existing application for
   everything else.

**b) A tunnel route for `/mcp` (if the dashboard's route has "Protect with Access")**

If your tunnel route for the dashboard validates the Access token (**Additional application settings → Access**
shows *customized*, or *Protect with Access* is on), the tunnel itself turns away anything that skipped Access,
and `/mcp` shows **HTTP ERROR 403 - Access denied**. Give `/mcp` its own route without that check:

1. **Networking → Tunnels** → your tunnel → **Routes** → **Add route → Published application**.
2. **Subdomain** and **Domain**: the same as the dashboard's route. **Path**: `^/mcp(/.*)?$`.
   **Service URL**: the same as the dashboard's route (e.g. `http://192.168.1.50:8765`).
3. Leave **Additional application settings → Access** on *Using defaults*. Save.
4. In the routes list, this route must come **before** the dashboard's own route (the tunnel uses the first match).

The dashboard's route keeps its Access check for every other page.

**Check**, in a private browser window:

| Address | Should show |
|---|---|
| `https://<your-dashboard>/` | the Cloudflare sign-in page |
| `https://<your-dashboard>/mcp` | **HTTP ERROR 405** ("This page isn't working"). That's right: `/mcp` only answers Claude, not a browser. |

Sign-in page on `/mcp`: step (a) isn't in effect (blank subdomain, policy not attached, or wait a minute).
HTTP ERROR 403: step (b). `{"detail":"Not Found"}`: it got through, but the dashboard is older than 0.3.0.

`PRINTER_DASH_ALLOWED_HOSTS` must already contain your domain (see the README, Configuration).

## 3. Add it to Claude

1. In Claude: **Plugins → Add → Add custom connector**. (Claude's menus move around: if it isn't there, look for
   *Connectors* under *Customize* or *Settings*.)
2. Name: e.g. *PrintFloor*. URL: paste the Connector URL.
3. Authentication: leave the defaults; the token is already in the URL.
4. **Add**, then start a new chat and ask *"what's printing?"*.

**Request headers instead of the token in the URL.** If your Claude account shows a **Request headers** section
when adding the connector, you can keep the token out of the URL: use `https://<your-dashboard>/mcp` as the URL
and add the header `Authorization` with the value `Bearer <token>` (the token is under *Using a request header
instead* when you turn the connector on or make a new token). `X-API-Key: <token>` works too.

## Good to know

- **Last used** in the connector window shows when Claude last connected (updated at most every 5 minutes).
- After 20 wrong tokens in a minute the connector refuses everyone for a minute.
- Times are given in UTC; the dashboard's own time zone comes along with them, so Claude can convert.
- A camera snapshot wakes the camera the same way the camera wall does.
- Nothing is sent anywhere until Claude asks; PrintFloor doesn't push anything to Claude.
