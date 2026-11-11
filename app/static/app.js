"use strict";
/* PF PrintFloor - read-only status. No build step, no external scripts. */

const $ = (s, el = document) => el.querySelector(s);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const state = { printers: [], catalog: [], account: null, detailsId: null, view: "full", overlayId: null,
                filter: new Set(), locs: new Set(), groupLoc: false, collapsed: new Set(),
                layout: { dashboard: { name: "PF PrintFloor", banner: true }, locations: { items: [], default: null } } };
const VIEWS = ["full", "compact", "list"];

// Card size: Full or Compact for every card, remembered in this browser. In Compact, Expand opens
// one card full-size in an overlay; the grid itself stays compact.
function loadView() {
  try { state.view = VIEWS.includes(localStorage.getItem("view")) ? localStorage.getItem("view") : "full"; }
  catch (_) { /* storage blocked: default to Full */ }
}
function saveView() {
  try { localStorage.setItem("view", state.view); } catch (_) { /* ignore */ }
}

const JOB_LABEL = {
  idle: "Idle", preparing: "Preparing", printing: "Printing", paused: "Paused", finished: "Finished",
  cancelled: "Cancelled", failed: "Failed", error: "Error", unknown: "Unknown",
};
const CONN_LABEL = {
  connecting: "Connecting", offline: "Offline", auth_error: "Access code rejected",
  busy: "Connection slots full", error: "Driver error",
};
const ACTIVE = new Set(["printing", "preparing", "paused"]);

// ---------------------------------------------------------------- api
async function api(method, url, body) {
  const r = await fetch(url, {
    method, headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!r.ok) {
    let msg = `${r.status} ${r.statusText}`;
    try {
      const j = await r.json();
      if (typeof j.detail === "string") msg = j.detail;
      else if (Array.isArray(j.detail)) msg = j.detail.map((d) => `${d.loc?.slice(-1)[0]}: ${d.msg}`).join("; ");
    } catch (_) { /* not json */ }
    throw new Error(msg);
  }
  return r.status === 204 ? null : r.json();
}

// ---------------------------------------------------------------- formatting
function fmtDur(s) {
  if (s == null) return "";
  s = Math.max(0, Math.round(s));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  return h ? `${h}h ${String(m).padStart(2, "0")}m` : `${m}m`;
}
function fmtEta(iso) {
  if (!iso) return "";
  const d = new Date(iso), now = new Date();
  const t = d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  if (d.toDateString() === now.toDateString()) return t;
  return `${d.toLocaleDateString([], { weekday: "short" })} ${t}`;
}
function fmtTemp(t) {
  if (!t || t.current == null) return "";
  const cur = Math.round(t.current);
  return t.target ? `${cur}°<span class="tgt">/${Math.round(t.target)}°</span>` : `${cur}°`;
}

// Offline cards: when the printer itself last reported. Today: "9:14 AM"; this week: "Mon 9:14 AM"; older: "Oct 28".
function fmtSeen(iso) {
  if (!iso) return "";
  const d = new Date(iso), now = new Date();
  const t = d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  if (d.toDateString() === now.toDateString()) return t;
  if (now - d < 6 * 864e5) return `${d.toLocaleDateString([], { weekday: "short" })} ${t}`;
  return d.toLocaleDateString([], { month: "short", day: "numeric", ...(d.getFullYear() === now.getFullYear() ? {} : { year: "numeric" }) });
}
function fmtUp(iso) {                 // "3 d 4 h" / "5 h 12 min" since the printer's computer started
  const m = Math.max(0, Math.floor((Date.now() - new Date(iso)) / 60000));
  const d = Math.floor(m / 1440), h = Math.floor((m % 1440) / 60);
  return d ? `${d} d ${h} h` : h ? `${h} h ${m % 60} min` : `${m} min`;
}
function seenText(p) {
  const st = p.status;
  const off = p.power?.on === false || (st && st.connection !== "online" && st.connection !== "connecting");
  return off && st?.last_seen_at ? `Last seen ${fmtSeen(st.last_seen_at)}` : "";
}

// Automatic power cycle: one short line for the card's bottom slot (and the list's Time column).
const PAUSE_SVG = '<svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true" fill="currentColor"><rect x="6" y="5" width="4" height="14" rx="1"/><rect x="14" y="5" width="4" height="14" rx="1"/></svg>';
const PLAY_SVG = '<svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true" fill="currentColor"><path d="M7 5l12 7-12 7z"/></svg>';
const RESTART_SVG = '<svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 12a8 8 0 1 0 2.4-5.7"/><path d="M4 4v4h4"/></svg>';
function cycleInfo(p) {
  const a = p.autocycle;
  if (!a || !a.enabled || seenText(p)) return null;
  const mins = (iso) => Math.max(0, Math.ceil((new Date(iso) - Date.now()) / 60000));
  switch (a.phase) {
    case "scheduled": return { cls: "", text: `Reboot at ${fmtEta(a.at)}`, tip: "Automatic power cycle" };
    case "countdown": { const m = mins(a.at); return { cls: "soon", text: m ? `Reboot in ${m} min` : "Rebooting now", tip: "Automatic power cycle", btns: ["pause", "restart"] }; }
    case "paused": return { cls: "", text: "Reboot paused", btns: ["resume"], tip: `Automatic power cycle skipped until ${fmtEta(a.until)}` };
    case "cycling": return { cls: "busy", text: "Rebooting…", tip: "Automatic power cycle: plug off, 15 s, plug on" };
    case "failed": return { cls: "bad", text: "Reboot failed", tip: a.note || "" };
    case "waiting": return ["printing", "preparing", "receiving a file"].includes(a.why)
      ? { cls: "", text: "Reboot after this print", tip: "Automatic power cycle is due" }
      : { cls: "", text: "Reboot waiting", tip: `Automatic power cycle is due, waiting: ${a.why}` };
    default: return null;
  }
}
const inCycle = (p) => ["countdown", "paused", "cycling"].includes(p.autocycle?.enabled && p.autocycle.phase);
function cycleHTML(p) {
  const c = cycleInfo(p);
  if (!c) return "";
  const btn = { pause: ["data-cycle=\"pause\"", "Pause: skip today's power cycle", PAUSE_SVG],
                restart: ["data-cycle=\"restart\"", "Restart the countdown", RESTART_SVG],
                resume: ["data-cycle=\"resume\"", "Resume automatic power cycle", PLAY_SVG] };
  return `<span class="cycle ${c.cls}" title="${esc(c.tip || "")}">${esc(c.text)}</span>${(c.btns || []).map((b) =>
    `<button type="button" class="mini-btn" ${btn[b][0]} title="${btn[b][1]}" aria-label="${btn[b][1]} (${esc(p.name)})">${btn[b][2]}</button>`).join("")}`;
}
// Card's bottom slot, most important first: offline "Last seen", the power-cycle line, then uptime (U1).
function upText(p) {
  const st = p.status;
  return st?.booted_at && st.connection === "online" ? `Up ${fmtUp(st.booted_at)}` : "";
}
function slotHTML(p) {
  const seen = seenText(p);
  if (seen) return `<span class="muted">${esc(seen)}</span>`;
  const cyc = cycleHTML(p);
  if (cyc) return cyc;
  const up = upText(p);
  return up ? `<span class="muted" title="Since ${esc(fmtWhen(p.status.booted_at))}">${esc(up)}</span>` : "";
}

// ---------------------------------------------------------------- pieces
function activeSlot(st) {
  for (const u of st?.filament || []) for (const s of u.slots) if (s.active) return s;
  return null;
}
function slotStyle(s) {
  if (!s.present) return "";
  const cols = [s.color_hex, ...(s.extra_colors || [])].filter(Boolean);
  if (cols.length > 1) {
    const step = 360 / cols.length;
    const stops = cols.map((c, i) => `${c} ${i * step}deg ${(i + 1) * step}deg`).join(", ");
    return `--c: conic-gradient(${stops})`;
  }
  return cols.length ? `--c: ${cols[0]}` : "--c: var(--track)";
}
function slotName(s) {
  if (!s || !s.present) return "Empty";
  const mat = s.sub_type && s.material && s.sub_type.includes(s.material) ? s.sub_type
    : [s.material, s.sub_type].filter(Boolean).join(" ");
  return mat || "Unknown filament";
}
function spoolEl(s) {
  const cls = ["spool", s.present ? "" : "empty", s.active ? "active" : ""].join(" ").trim();
  const label = `${s.source === "external" ? "External spool" : `Slot ${s.index + 1}`}: ${slotName(s)}${s.brand ? `, ${s.brand}` : ""}`;
  return `<span class="${cls}" style="${slotStyle(s)}" title="${esc(label)}" role="img" aria-label="${esc(label)}"></span>`;
}

const PLATE_SVG = `<svg viewBox="0 0 48 48" aria-hidden="true"><path d="M8 34h32l-4 6H12z" fill="currentColor"/><rect x="18" y="14" width="12" height="16" rx="2" fill="none" stroke="currentColor" stroke-width="3"/><path d="M24 4v6" stroke="currentColor" stroke-width="3"/></svg>`;

function ringEl(st, color) {
  const job = st?.job || {};
  const pct = job.progress_pct;
  const r = 44, c = 2 * Math.PI * r;
  const off = pct == null ? c : c * (1 - Math.min(100, Math.max(0, pct)) / 100);
  const stroke = color || "var(--state)";
  const sub = st && ACTIVE.has(st.state) && job.remaining_s != null ? `${fmtDur(job.remaining_s)} left`
    : st && st.state === "finished" ? "Done" : "";
  return `<div class="ring" role="img" aria-label="${pct == null ? "No job progress" : `${Math.round(pct)} percent done`}">
    <svg viewBox="0 0 100 100" aria-hidden="true">
      <circle class="edge" cx="50" cy="50" r="${r}" fill="none" stroke-width="13"/>
      <circle class="track" cx="50" cy="50" r="${r}" fill="none" stroke-width="11"/>
      <circle class="arc" cx="50" cy="50" r="${r}" fill="none" stroke="${esc(stroke)}" stroke-width="11"
              stroke-dasharray="${c.toFixed(1)}" stroke-dashoffset="${off.toFixed(1)}" stroke-linecap="butt"/>
    </svg>
    <div class="ring-text"><b>${pct == null ? "" : Math.round(pct) + "%"}</b><small>${esc(sub)}</small></div>
  </div>`;
}

function thumbEl(p, big = false) {
  const url = p.status?.job?.thumbnail_url;
  const cls = big ? "thumb big-thumb" : "thumb";
  if (url) return `<div class="${cls}"><img src="${esc(url)}" alt="Preview of ${esc(p.status.job.name || "the current job")}" loading="lazy"></div>`;
  const hasJob = p.status?.job?.name && ACTIVE.has(p.status?.state);
  return `<div class="${cls}">${hasJob ? `<span>Preview<br>not available yet</span>` : PLATE_SVG}</div>`;
}

// "Paused: <why>" - unless the printer's phrase already starts with the state word ("Paused by user")
function withState(state, reason) {
  const word = JOB_LABEL[state] || "";
  return reason.toLowerCase().startsWith(word.toLowerCase()) ? reason : `${word}: ${reason}`;
}
// pill tooltip: why it's paused/failed when the printer says, else the connection detail
function pillTitle(st, p = null) {
  const c = p ? cycleInfo(p) : null;
  const extra = c ? ` - ${c.text}` : "";
  if (st?.connection === "online" && st.reason && ["paused", "failed", "error"].includes(st.state)) return withState(st.state, st.reason) + extra;
  return (st?.connection_detail || (c && st ? JOB_LABEL[st.state] || "" : "")) + extra;
}
function pillText(st, withStage = false, p = null) {
  if (p?.power?.on === false) return "Powered off";
  if (p?.power?.starting) return "Starting up";
  if (!st) return "Connecting";
  if (st.connection !== "online") return CONN_LABEL[st.connection] || st.connection;
  const job = st.job || {};
  if (job.transferring && st.state === "preparing")
    return job.transfer_pct != null ? `Downloading ${Math.round(job.transfer_pct)}%` : "Receiving file";
  if (withStage && st.reason && ["paused", "failed", "error"].includes(st.state)) return withState(st.state, st.reason);
  const stage = withStage && job.sub_state && st.state !== "idle" ? job.sub_state : "";
  // the printer's stage text can already start with the state word ("Paused: filament runout"): don't say it twice
  if (stage && stage.toLowerCase().startsWith(JOB_LABEL[st.state].toLowerCase())) return stage;
  return JOB_LABEL[st.state] + (stage ? `: ${stage}` : "");
}

function tempsFor(st) {
  const temps = st?.temps?.length ? st.temps : [
    { key: "nozzle", label: "Nozzle" }, { key: "bed", label: "Bed" }, { key: "chamber", label: "Chamber" }];
  return temps.slice(0, 3);
}

// ---------------------------------------------------------------- in-place updates
// Rebuilding a card with innerHTML on every status message (Bambus send ~1/s) recreated the
// thumbnail <img> and the ring each time, which made the page blink. morph() walks the old and
// new DOM together and only touches nodes, attributes and text that actually changed.
function morph(target, html) {
  const tpl = document.createElement("template");
  tpl.innerHTML = html;
  morphChildren(target, tpl.content);
}
function morphChildren(oldParent, newParent) {
  const olds = [...oldParent.childNodes], news = [...newParent.childNodes];
  for (let i = 0; i < news.length; i++) {
    const o = olds[i], n = news[i];
    if (!o) { oldParent.appendChild(n.cloneNode(true)); continue; }
    morphNode(o, n);
  }
  for (let i = olds.length - 1; i >= news.length; i--) olds[i].remove();
}
function morphNode(o, n) {
  if (o.nodeType !== n.nodeType || o.nodeName !== n.nodeName) { o.replaceWith(n.cloneNode(true)); return; }
  if (o.nodeType === Node.TEXT_NODE || o.nodeType === Node.COMMENT_NODE) {
    if (o.nodeValue !== n.nodeValue) o.nodeValue = n.nodeValue;
    return;
  }
  if (o.nodeType !== Node.ELEMENT_NODE) return;
  for (const a of [...o.attributes]) if (!n.hasAttribute(a.name)) o.removeAttribute(a.name);
  for (const a of n.attributes) if (o.getAttribute(a.name) !== a.value) o.setAttribute(a.name, a.value);
  morphChildren(o, n);
}

// ---------------------------------------------------------------- card
function cardHTML(p, inOverlay = false) {
  const st = p.status;
  const job = st?.job || {};
  const act = activeSlot(st);
  const color = act && act.color_hex && st && ACTIVE.has(st.state) ? act.color_hex : null;
  const showJob = st && st.state !== "idle" && job.name;
  // Not reachable: the reason goes in the job-name slot (2 lines reserved), so the card keeps its size.
  const connMsg = p.power?.on === false ? "Powered off at the plug."
    : st && st.connection !== "online" && st.connection !== "connecting" ? (st.connection_detail || CONN_LABEL[st.connection] || "")
    // paused / failed: why, in the job-name slot (the job name moves to the tooltip)
    : st?.reason && ["paused", "failed", "error"].includes(st.state) ? withState(st.state, st.reason) : "";
  const units = st?.filament || [];
  const spools = units.map((u) => `<span class="spool-group" title="${esc(u.kind)}">${u.slots.map(spoolEl).join("")}</span>`).join("");

  return `
    <div class="card-head">
      <button type="button" class="grip" data-grip aria-label="Drag to reorder ${esc(p.name)}" title="Drag to reorder">
        <svg width="14" height="22" viewBox="0 0 14 22" aria-hidden="true" fill="currentColor"><circle cx="4" cy="5" r="1.6"/><circle cx="10" cy="5" r="1.6"/><circle cx="4" cy="11" r="1.6"/><circle cx="10" cy="11" r="1.6"/><circle cx="4" cy="17" r="1.6"/><circle cx="10" cy="17" r="1.6"/></svg>
      </button>
      <div class="card-name"><h3>${esc(p.name)}${updDot(p)}</h3><p>${modelLine(p)}</p></div>
      <span class="pill" title="${esc(pillTitle(st, p))}">${esc(pillText(st, false, p))}</span>
    </div>
    <div class="media">${thumbEl(p)}${ringEl(st, color)}</div>
    <div class="card-body">
      ${connMsg ? `<p class="job-name conn-note" title="${esc(connMsg + (showJob ? ` (${job.name})` : ""))}">${esc(connMsg)}</p>`
        : `<p class="job-name ${showJob ? "" : "none"}">${showJob ? esc(job.name) : "No job running"}</p>`}
      <dl class="facts">
        <div class="fact"><dt>Done at</dt><dd>${showJob && ACTIVE.has(st.state) ? esc(fmtEta(job.eta)) : ""}</dd></div>
        <div class="fact"><dt>Layer</dt><dd>${showJob && job.total_layers ? `${job.layer ?? 0}<span class="tgt">/${job.total_layers}</span>` : ""}</dd></div>
        <div class="fact"><dt>Elapsed</dt><dd>${showJob ? esc(fmtDur(job.elapsed_s)) : ""}</dd></div>
        ${tempsFor(st).map((t) => `<div class="fact"><dt>${esc(t.label)}</dt><dd>${fmtTemp(t)}</dd></div>`).join("")}
      </dl>
      <div class="spools${units.length > 4 ? " dense denser" : units.length > 2 ? " dense" : ""}">${spools}</div>
    </div>
    <div class="card-foot">
      <span class="seen">${slotHTML(p)}</span>
      <span class="foot-actions">
        ${inOverlay ? `<button type="button" class="btn ghost" data-overlay-close>Close</button>` : ""}
        ${iconStack(p)}
        <button type="button" class="btn" data-details>Details</button>
      </span>
    </div>`;
}

const CAM_SVG = '<svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 8a2 2 0 0 1 2-2h2l1.5-2h7L17 6h2a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><circle cx="12" cy="12.5" r="3.5"/></svg>';
const POWER_SVG = '<svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 3v8"/><path d="M6.3 6.8a8 8 0 1 0 11.4 0"/></svg>';
function pwrBtn(p) {
  if (!p.power) return "";
  // green = plug on, red = plug off, yellow = plug not online / state unknown
  const s = p.power.online === false || p.power.on == null ? "unknown" : p.power.on ? "on" : "off";
  const tip = { on: "Plug is on", off: "Plug is off", unknown: p.power.online === false ? "Plug is not online" : "Plug state not known yet" }[s];
  return `<button type="button" class="icon-btn pwr-btn" data-power-open data-pstate="${s}" aria-label="Power for ${esc(p.name)}: ${tip}" title="${tip}">${POWER_SVG}</button>`;
}
// Always two slots (camera over power) so every card has the same size; a missing button leaves blank space.
function iconStack(p) {
  const ph = `<span class="icon-ph" aria-hidden="true"></span>`;
  return `<span class="icon-stack">${camBtn(p) || ph}${pwrBtn(p) || ph}</span>`;
}
// Bambu reached through Bambu's cloud (away from the LAN): no built-in camera that way.
const viaCloud = (p) => p.status?.link === "cloud" && p.status?.connection === "online";
const updDot = (p) => p.status?.update_available ? ` <span class="upd-dot" title="Firmware update available: ${esc(p.status.update_available)} (apply it on the printer)" role="img" aria-label="Firmware update available"></span>` : "";
const modelLine = (p) => `${esc(p.model)}${viaCloud(p) ? ` <span class="via-cloud" title="Not on this network: status comes through the Bambu cloud. No camera this way.">via cloud</span>` : ""}`;
function camBtn(p) {
  if (viaCloud(p) && p.camera?.kind === "builtin") return "";
  return p.camera?.kind ? `<button type="button" class="icon-btn cam-btn" data-camera aria-label="Camera: ${esc(p.name)}" title="Camera">${CAM_SVG}</button>` : "";
}

function compactHTML(p) {
  const st = p.status;
  const act = activeSlot(st);
  const color = act && act.color_hex && st && ACTIVE.has(st.state) ? act.color_hex : null;
  return `
    <div class="card-head">
      <button type="button" class="grip" data-grip aria-label="Drag to reorder ${esc(p.name)}" title="Drag to reorder">
        <svg width="14" height="22" viewBox="0 0 14 22" aria-hidden="true" fill="currentColor"><circle cx="4" cy="5" r="1.6"/><circle cx="10" cy="5" r="1.6"/><circle cx="4" cy="11" r="1.6"/><circle cx="10" cy="11" r="1.6"/><circle cx="4" cy="17" r="1.6"/><circle cx="10" cy="17" r="1.6"/></svg>
      </button>
      <div class="card-name"><h3>${esc(p.name)}${updDot(p)}</h3><p>${modelLine(p)}</p></div>
      ${iconStack(p)}
    </div>
    <span class="pill" title="${esc(pillTitle(st, p))}">${esc(pillText(st, false, p))}</span>
    ${ringEl(st, color)}
    <div class="card-foot">
      <span class="seen">${slotHTML(p)}</span>
      <span class="foot-actions">
        <button type="button" class="btn ghost" data-size="expand" aria-label="Expand ${esc(p.name)}">Expand</button>
        <button type="button" class="btn" data-details>Details</button>
      </span>
    </div>`;
}

// ---------------------------------------------------------------- list view (one row per printer)
const GRIP = `<button type="button" class="grip" data-grip aria-label="Drag to reorder" title="Drag to reorder">
        <svg width="14" height="22" viewBox="0 0 14 22" aria-hidden="true" fill="currentColor"><circle cx="4" cy="5" r="1.6"/><circle cx="10" cy="5" r="1.6"/><circle cx="4" cy="11" r="1.6"/><circle cx="10" cy="11" r="1.6"/><circle cx="4" cy="17" r="1.6"/><circle cx="10" cy="17" r="1.6"/></svg>
      </button>`;
// [column key, header label, sort it sets (none = not sortable)]
const LIST_COLS = [["grip", ""], ["thumb", ""], ["printer", "Printer", "name"], ["status", "Status", "status"],
  ["job", "Job", "job"], ["progress", "Progress", "progress"], ["time", "Time left / Done at", "left"],
  ["filament", "Filament"], ["location", "Location", "location"], ["actions", ""]];

function listHeadEl(grid) {
  let el = grid.querySelector(":scope > .lhead");
  if (!el) { el = document.createElement("div"); el.className = "lhead"; }
  morph(el, LIST_COLS.map(([k, label, sort]) => {
    if (!sort) return `<span class="lc lc-${k}">${esc(label)}</span>`;
    const on = state.sort === sort;
    const words = SORT_WORDS[sort];
    const tip = on ? `Sorted ${words[state.sortDir === "desc" ? 1 : 0]}. Click to reverse.` : `Sort by ${SORT_LABEL[sort]}`;
    return `<button type="button" class="lc lc-${k}" data-col-sort="${sort}" aria-pressed="${on}" title="${esc(tip)}">${esc(label)}<i class="arr" aria-hidden="true">${on ? (state.sortDir === "desc" ? "↓" : "↑") : ""}</i></button>`;
  }).join(""));
  return el;
}

function rowHTML(p) {
  const st = p.status;
  const job = st?.job || {};
  const act = activeSlot(st);
  const color = act && act.color_hex && st && ACTIVE.has(st.state) ? act.color_hex : null;
  const showJob = st && st.state !== "idle" && job.name;
  const live = st && ACTIVE.has(st.state);
  const pct = showJob ? job.progress_pct : null;
  const cyc = !seenText(p) && !live ? (cycleHTML(p) || (upText(p) ? `<span class="muted">${esc(upText(p))}</span>` : "")) : "";
  const time = seenText(p) || (live ? [job.remaining_s != null ? `${fmtDur(job.remaining_s)} left` : "", fmtEta(job.eta)].filter(Boolean).join(" · ")
    : st?.state === "finished" && showJob ? "Done" : "");
  const units = st?.filament || [];
  const spools = units.map((u) => `<span class="spool-group" title="${esc(u.kind)}">${u.slots.map(spoolEl).join("")}</span>`).join("");
  const url = job.thumbnail_url;
  const thumb = url ? `<img src="${esc(url)}" alt="" loading="lazy">` : PLATE_SVG;
  const ph = `<span class="icon-ph" aria-hidden="true"></span>`;
  return `
    <div class="lc lc-grip">${GRIP}</div>
    <div class="lc lc-thumb"><div class="thumb mini">${thumb}</div></div>
    <div class="lc lc-printer"><b>${esc(p.name)}${updDot(p)}</b><small>${modelLine(p)}</small></div>
    <div class="lc lc-status"><span class="pill" title="${esc(pillTitle(st, p))}">${esc(pillText(st, false, p))}</span></div>
    <div class="lc lc-job${showJob ? "" : " none"}" title="${esc(showJob ? job.name : "")}">${showJob ? esc(job.name) : "No job running"}</div>
    <div class="lc lc-progress">${pct == null ? "" : `<span class="bar" aria-hidden="true"><i style="width:${Math.min(100, Math.max(0, pct)).toFixed(1)}%;${color ? ` background:${esc(color)}` : ""}"></i></span><b>${Math.round(pct)}%</b>`}</div>
    <div class="lc lc-time">${cyc || esc(time)}</div>
    <div class="lc lc-filament"><div class="spools dense${units.length > 2 || units.reduce((n, u) => n + u.slots.length, 0) > 7 ? " denser" : ""}">${spools}</div></div>
    <div class="lc lc-location">${esc(locName(locOf(p)))}</div>
    <div class="lc lc-actions">${camBtn(p) || ph}${pwrBtn(p) || ph}<button type="button" class="btn" data-details>Details</button></div>`;
}

function renderCard(p, el) {
  el = el || document.createElement("article");
  const compact = state.view === "compact", row = state.view === "list";
  el.className = "card" + (compact ? " compact" : row ? " row" : "");
  el.dataset.id = p.id;
  el.dataset.state = p.status?.state || "unknown";
  el.dataset.conn = p.status?.connection || "connecting";
  el.dataset.power = p.power?.on === false ? "off" : p.power?.starting ? "starting" : "";
  el.setAttribute("aria-label", p.name);
  el.dataset.loc = locOf(p);
  const html = compact ? compactHTML(p) : row ? rowHTML(p) : cardHTML(p);
  if (el.childNodes.length) morph(el, html); else el.innerHTML = html;
  return el;
}

function renderAll() {
  const grid = $("#grid");
  const ids = state.printers.map((p) => p.id);
  for (const el of [...grid.querySelectorAll(":scope > .card")]) if (!ids.includes(el.dataset.id)) el.remove();
  for (const p of state.printers) {                  // applyOrder() puts everything (and section headers) in place
    const el = grid.querySelector(`:scope > [data-id="${CSS.escape(p.id)}"]`);
    const card = renderCard(p, el);
    if (!el) grid.appendChild(card);
  }
  $("#empty").hidden = state.printers.length > 0;
  applyOrder();
  renderSummary();
  if (state.overlayId) overlayCard();
  if (state.detailsId) renderDetails();
  if ($("#powerDlg").open) renderPowerDlg();
}

// Status filter: chips with counts; "All" or any combination (OR). Remembered in this browser.
const FILTERS = [
  ["printing", "Printing", "var(--printing)"], ["paused", "Paused", "var(--paused)"],
  ["finished", "Finished", "var(--done)"], ["failed", "Failed", "var(--fail)"],
  ["idle", "Idle", "var(--idle)"], ["offline", "Offline", "var(--idle)"]];

function category(p) {
  if (p.power?.on === false || p.power?.starting) return "offline";
  const st = p.status;
  if (!st || st.connection !== "online") return "offline";        // offline, connecting, busy, auth/driver error
  if (st.state === "paused") return "paused";
  if (ACTIVE.has(st.state)) return "printing";                     // printing, preparing
  if (st.state === "finished") return "finished";
  if (["failed", "error", "cancelled"].includes(st.state)) return "failed";
  return "idle";
}

// Groups: automatic (maker, model) + the user's tags. Within one kind any match counts; across kinds all must match.
function groupKeys(p) {
  return [`maker:${p.maker}`, `model:${p.model}`, ...(p.tags || []).map((t) => `tag:${t.toLowerCase()}`)];
}
function groupLabel(key) {
  const [kind, val] = [key.slice(0, key.indexOf(":")), key.slice(key.indexOf(":") + 1)];
  if (kind === "tag") {
    for (const p of state.printers) for (const t of p.tags || []) if (t.toLowerCase() === val) return t;
  }
  return val;
}
function groupMatch(p) {
  if (!state.groups.size) return true;
  const mine = new Set(groupKeys(p));
  const byKind = {};
  for (const k of state.groups) (byKind[k.split(":")[0]] ||= []).push(k);
  return Object.values(byKind).every((keys) => keys.some((k) => mine.has(k)));
}

function shown(p) {
  return (!state.filter.size || state.filter.has(category(p)) || (state.filter.has("cycle") && inCycle(p))) && groupMatch(p) && locMatch(p);
}

// ---------------------------------------------------------------- locations (filter + optional sections)
function locItems() { return state.layout.locations.items || []; }
function locOf(p) { return p.location || state.layout.locations.default; }
function locName(id) { return locItems().find((l) => l.id === id)?.name || ""; }
function locIndex(id) { const i = locItems().findIndex((l) => l.id === id); return i < 0 ? 999 : i; }
function locMatch(p) { return !state.locs.size || state.locs.has(locOf(p)); }
function folded(p) { return state.groupLoc && state.collapsed.has(locOf(p)); }
// [[location or null, printers]]: one unnamed section when not grouped; else per location in the user's order
function sections(list) {
  if (!state.groupLoc) return [[null, list]];
  return locItems().map((l) => [l, list.filter((p) => locOf(p) === l.id)]).filter(([, ps]) => ps.length);
}
function locHeadEl(box, l) {
  let el = box.querySelector(`:scope > [data-loc-head="${CSS.escape(l.id)}"]`);
  if (!el) { el = document.createElement("div"); el.className = "loc-head"; el.dataset.locHead = l.id; }
  const open = !state.collapsed.has(l.id);
  morph(el, `<button type="button" class="loc-toggle" data-loc-toggle aria-expanded="${open}" title="${open ? "Hide" : "Show"} the printers here"><i class="chev" aria-hidden="true"></i><span class="loc-name">${esc(l.name)}</span><span class="n"></span></button>`);
  return el;
}
function updateHeads(box) {                       // count of printers the filters let through; hide empty sections
  for (const h of box.querySelectorAll(":scope > .loc-head")) {
    const n = state.printers.filter((p) => locOf(p) === h.dataset.locHead && shown(p) && (box.id !== "wallGrid" || p.camera?.kind)).length;
    h.querySelector(".n").textContent = String(n);
    if (h.hidden !== !n) h.hidden = !n;
  }
}
function placeNodes(box, nodes) {
  for (const el of [...box.children]) if (!nodes.includes(el)) el.remove();
  nodes.forEach((n, i) => { if (box.children[i] !== n) box.insertBefore(n, box.children[i] || null); });
}

// Sort: My order (drag), Time left (soonest first), Name, Status.
const STATUS_RANK = { printing: 0, paused: 1, failed: 2, finished: 3, idle: 4, offline: 5 };
function sortedPrinters() {
  const list = [...state.printers];
  const left = (p) => {
    const c = category(p), r = p.status?.job?.remaining_s;
    return c === "printing" ? (r ?? 9e9) : c === "paused" ? 1e10 : 2e10 + STATUS_RANK[c];
  };
  const dir = state.sortDir === "desc" ? -1 : 1;
  const byName = (a, b) => a.name.localeCompare(b.name, undefined, { numeric: true });
  if (state.sort === "left") {
    // printing printers by time left (dir flips that), then paused, then everything else - those stay at the end
    const group = (p) => { const c = category(p); return c === "printing" ? 0 : c === "paused" ? 1 : 2; };
    list.sort((a, b) => group(a) - group(b)
      || (group(a) === 0 ? dir * ((a.status?.job?.remaining_s ?? 9e9) - (b.status?.job?.remaining_s ?? 9e9)) : 0)
      || (group(a) === 2 ? STATUS_RANK[category(a)] - STATUS_RANK[category(b)] : 0) || byName(a, b));
  } else if (state.sort === "name") list.sort((a, b) => dir * byName(a, b));
  else if (state.sort === "job") {
    const jn = (p) => (p.status && p.status.state !== "idle" && p.status.job?.name) || "";
    list.sort((a, b) => (!jn(a)) - (!jn(b)) || dir * jn(a).localeCompare(jn(b), undefined, { numeric: true }) || byName(a, b));
  } else if (state.sort === "progress") {
    const pr = (p) => (p.status && p.status.state !== "idle" && p.status.job?.name ? p.status.job.progress_pct : null);
    list.sort((a, b) => (pr(a) == null) - (pr(b) == null) || dir * ((pr(a) ?? 0) - (pr(b) ?? 0)) || byName(a, b));
  } else if (state.sort === "location") list.sort((a, b) => dir * (locIndex(locOf(a)) - locIndex(locOf(b))) || byName(a, b));
  else if (state.sort === "status") list.sort((a, b) => dir * (STATUS_RANK[category(a)] - STATUS_RANK[category(b)]) || byName(a, b));
  if (state.groupLoc) {                          // sections keep the sort inside them (stable sort)
    const order = new Map(list.map((p, i) => [p.id, i]));
    list.sort((a, b) => locIndex(locOf(a)) - locIndex(locOf(b)) || order.get(a.id) - order.get(b.id));
  }
  return list;
}
const SORT_WORDS = { left: ["least time left first", "most time left first"], name: ["A to Z", "Z to A"],
  status: ["printing first", "printing last"], job: ["A to Z", "Z to A"], progress: ["least done first", "most done first"],
  location: ["in your location order", "in reverse location order"] };
const SORT_LABEL = { manual: "my order", left: "time left", name: "name", status: "status", job: "job", progress: "progress",
  location: "location" };
const SORTS = Object.keys(SORT_LABEL);
// Click a sort: ascending. Click the same one again: reversed. Switching to another sort starts ascending again.
function setSort(s) {
  if (s === state.sort) { if (s === "manual") return; state.sortDir = state.sortDir === "asc" ? "desc" : "asc"; }
  else { state.sort = s; state.sortDir = "asc"; }
  saveFilter(); renderSortSeg(); applyOrder(); if (state.detailsId) renderDetails();
}
function renderSortSeg() {
  for (const b of document.querySelectorAll("#sortSeg [data-sort]")) {
    const s = b.dataset.sort, on = s === state.sort;
    b.setAttribute("aria-pressed", String(on));
    const arr = b.querySelector(".arr");
    if (arr) arr.textContent = on ? (state.sortDir === "desc" ? "↓" : "↑") : "";
    const words = SORT_WORDS[s];
    b.title = words ? (on ? `Sorted ${words[state.sortDir === "desc" ? 1 : 0]}. Click to reverse.` : `Sort by ${SORT_LABEL[s]}`) : "Your own order (drag cards to change it)";
  }
}
function applyOrder() {
  const grid = $("#grid");
  const nodes = state.view === "list" && state.printers.length ? [listHeadEl(grid)] : [];
  for (const [l, ps] of sections(sortedPrinters())) {
    if (l) nodes.push(locHeadEl(grid, l));
    for (const p of ps) { const el = grid.querySelector(`:scope > [data-id="${CSS.escape(p.id)}"]`); if (el) nodes.push(el); }
  }
  placeNodes(grid, nodes);
  updateHeads(grid);
  document.body.dataset.sort = state.sort;
  document.body.dataset.groupLoc = state.groupLoc ? "1" : "";
  if (wall && wall.on) renderWall();
}

function loadFilter() {
  try { state.filter = new Set(JSON.parse(localStorage.getItem("filter") || "[]").filter((k) => k === "cycle" || FILTERS.some((f) => f[0] === k))); }
  catch (_) { state.filter = new Set(); }
  try { state.groups = new Set(JSON.parse(localStorage.getItem("groups") || "[]")); } catch (_) { state.groups = new Set(); }
  try { state.sort = SORTS.includes(localStorage.getItem("sort")) ? localStorage.getItem("sort") : "manual"; }
  catch (_) { state.sort = "manual"; }
  try { state.sortDir = localStorage.getItem("sortDir") === "desc" ? "desc" : "asc"; } catch (_) { state.sortDir = "asc"; }
  try { state.locs = new Set(JSON.parse(localStorage.getItem("locs") || "[]")); } catch (_) { state.locs = new Set(); }
  try { state.collapsed = new Set(JSON.parse(localStorage.getItem("collapsedLocs") || "[]")); } catch (_) { state.collapsed = new Set(); }
  try { state.groupLoc = localStorage.getItem("groupLoc") === "1"; } catch (_) { state.groupLoc = false; }
}
function saveFilter() {
  try {
    localStorage.setItem("filter", JSON.stringify([...state.filter]));
    localStorage.setItem("groups", JSON.stringify([...state.groups]));
    localStorage.setItem("sort", state.sort);
    localStorage.setItem("sortDir", state.sortDir);
    localStorage.setItem("locs", JSON.stringify([...state.locs]));
    localStorage.setItem("collapsedLocs", JSON.stringify([...state.collapsed]));
    localStorage.setItem("groupLoc", state.groupLoc ? "1" : "0");
  } catch (_) { /* ignore */ }
}

function renderSummary() {                                          // filter chips + card visibility
  const counts = {};
  for (const p of state.printers) counts[category(p)] = (counts[category(p)] || 0) + 1;
  const chip = (key, label, color, n, on) =>
    `<button type="button" class="chip" data-filter="${key}" aria-pressed="${on}">${color ? `<i style="background:${color}"></i>` : ""}${esc(label)} <span class="n">${n}</span></button>`;
  const anyCycle = state.printers.some((p) => p.autocycle?.enabled);
  if (!anyCycle) state.filter.delete("cycle");
  const html = chip("all", "All", "", state.printers.length, !state.filter.size)
    + FILTERS.map(([k, label, c]) => chip(k, label, c, counts[k] || 0, state.filter.has(k))).join("")
    + (anyCycle ? chip("cycle", "Power cycle", "var(--paused)", state.printers.filter(inCycle).length, state.filter.has("cycle")) : "");
  const box = $("#summary");
  if (box.childNodes.length) morph(box, html); else box.innerHTML = html;
  let visible = 0;
  for (const p of state.printers) {
    const el = $(`#grid [data-id="${CSS.escape(p.id)}"]`);
    const show = shown(p);
    const hide = !show || folded(p);
    if (el && el.hidden !== hide) el.hidden = hide;
    if (show) visible++;
  }
  updateHeads($("#grid"));
  const none = $("#noMatch");
  none.hidden = !(state.printers.length && !visible);
  renderGroups();
  renderLocs();
  if (wall && wall.on) renderWall();
}

function renderGroups() {
  // drop selections whose group no longer exists (tag deleted, printer removed)
  const all = new Set(state.printers.flatMap(groupKeys));
  for (const k of [...state.groups]) if (!all.has(k)) state.groups.delete(k);
  const chips = [...state.groups].map((k) =>
    `<button type="button" class="chip" aria-pressed="true" data-ungroup="${esc(k)}" title="Remove this group filter">${esc(groupLabel(k))} <span class="n">✕</span></button>`).join("");
  const gc = $("#groupChips");
  if (gc.childNodes.length) morph(gc, chips); else gc.innerHTML = chips;
  $("#groupsBtn").textContent = state.groups.size ? `Groups (${state.groups.size})` : "Groups";
  if (!$("#groupsPop").hidden) renderGroupsPop();
}

function renderGroupsPop() {
  const counts = {};
  for (const p of state.printers) for (const k of new Set(groupKeys(p))) counts[k] = (counts[k] || 0) + 1;
  const section = (kind, title, empty) => {
    const keys = Object.keys(counts).filter((k) => k.startsWith(kind + ":"))
      .sort((a, b) => groupLabel(a).localeCompare(groupLabel(b), undefined, { numeric: true }));
    const items = keys.map((k) => `<label class="check"><input type="checkbox" data-group="${esc(k)}" ${state.groups.has(k) ? "checked" : ""}>
        <span>${esc(groupLabel(k))}</span><span class="n">${counts[k]}</span></label>`).join("");
    return `<div class="pop-sec"><h4>${title}</h4>${items || `<p class="muted pop-empty">${empty}</p>`}</div>`;
  };
  const html = section("tag", "Tags", "No tags yet. Add them in a printer's Details.")
    + section("maker", "Maker", "") + section("model", "Model", "")
    + (state.groups.size ? `<button type="button" class="link-btn" data-groups-clear>Clear groups</button>` : "");
  const pop = $("#groupsPop");
  if (pop.childNodes.length) morph(pop, html); else pop.innerHTML = html;
}

function setGroupsPop(open) {
  $("#groupsPop").hidden = !open;
  $("#groupsBtn").setAttribute("aria-expanded", String(open));
  if (open) renderGroupsPop();
}

function setupGroups() {
  $("#groupsBtn").addEventListener("click", () => setGroupsPop($("#groupsPop").hidden));
  $("#groupsPop").addEventListener("change", (e) => {
    const c = e.target.closest("[data-group]");
    if (!c) return;
    if (c.checked) state.groups.add(c.dataset.group); else state.groups.delete(c.dataset.group);
    saveFilter(); renderSummary();
  });
  $("#groupsPop").addEventListener("click", (e) => {
    if (e.target.closest("[data-groups-clear]")) { state.groups.clear(); saveFilter(); renderSummary(); }
  });
  $("#groupsPop").addEventListener("keydown", (e) => { if (e.key === "Escape") { setGroupsPop(false); $("#groupsBtn").focus(); } });
  $("#groupChips").addEventListener("click", (e) => {
    const b = e.target.closest("[data-ungroup]");
    if (b) { state.groups.delete(b.dataset.ungroup); saveFilter(); renderSummary(); }
  });
  document.addEventListener("click", (e) => { if (!e.target.closest("#groupsPop, #groupsBtn")) setGroupsPop(false); });
  // Click a sort: ascending. Click the same one again: reversed. Switching to another sort starts ascending again.
  $("#sortSeg").addEventListener("click", (e) => { const b = e.target.closest("[data-sort]"); if (b) setSort(b.dataset.sort); });
  $("#grid").addEventListener("click", (e) => { const b = e.target.closest("[data-col-sort]"); if (b) setSort(b.dataset.colSort); });
  renderSortSeg();
  document.body.dataset.sort = state.sort;
}

// Locations button: same dropdown as Groups - pick locations to show (none = all), plus Group by location.
function renderLocs() {
  const ids = new Set(locItems().map((l) => l.id));
  for (const k of [...state.locs]) if (!ids.has(k)) state.locs.delete(k);      // location deleted
  const chips = [...state.locs].map((k) =>
    `<button type="button" class="chip" aria-pressed="true" data-unloc="${esc(k)}" title="Remove this location filter">${esc(locName(k))} <span class="n">✕</span></button>`).join("");
  const lc = $("#locChips");
  if (lc.childNodes.length) morph(lc, chips); else lc.innerHTML = chips;
  $("#locBtn").textContent = state.locs.size ? `Locations (${state.locs.size})` : "Locations";
  if (!$("#locPop").hidden) renderLocPop();
}
function renderLocPop() {
  const counts = {};
  for (const p of state.printers) counts[locOf(p)] = (counts[locOf(p)] || 0) + 1;
  const items = locItems().map((l) => `<label class="check"><input type="checkbox" data-loc="${esc(l.id)}" ${state.locs.has(l.id) ? "checked" : ""}>
      <span>${esc(l.name)}</span><span class="n">${counts[l.id] || 0}</span></label>`).join("");
  const html = `<div class="pop-sec"><label class="check toggle-row"><input type="checkbox" class="toggle" data-loc-group ${state.groupLoc ? "checked" : ""}>
      <span>Group by location</span></label></div>
    <div class="pop-sec"><h4>Show</h4>${items}</div>
    ${state.locs.size ? `<button type="button" class="link-btn" data-locs-clear>Show all locations</button>` : ""}
    <button type="button" class="link-btn" data-manage-locs>Manage locations…</button>`;
  const pop = $("#locPop");
  if (pop.childNodes.length) morph(pop, html); else pop.innerHTML = html;
}
function setLocPop(open) {
  $("#locPop").hidden = !open;
  $("#locBtn").setAttribute("aria-expanded", String(open));
  if (open) renderLocPop();
}
function regroup() { saveFilter(); applyOrder(); renderSummary(); if (state.detailsId) renderDetails(); }
function setupLocs() {
  $("#locBtn").addEventListener("click", () => setLocPop($("#locPop").hidden));
  $("#locPop").addEventListener("change", (e) => {
    if (e.target.closest("[data-loc-group]")) { state.groupLoc = e.target.checked; regroup(); return; }
    const c = e.target.closest("[data-loc]");
    if (!c) return;
    if (c.checked) state.locs.add(c.dataset.loc); else state.locs.delete(c.dataset.loc);
    if (state.locs.size === locItems().length) state.locs.clear();               // everything picked = all
    saveFilter(); renderSummary();
  });
  $("#locPop").addEventListener("click", (e) => {
    if (e.target.closest("[data-locs-clear]")) { state.locs.clear(); saveFilter(); renderSummary(); }
    if (e.target.closest("[data-manage-locs]")) { setLocPop(false); openLayoutDlg(); }
  });
  $("#locPop").addEventListener("keydown", (e) => { if (e.key === "Escape") { setLocPop(false); $("#locBtn").focus(); } });
  $("#locChips").addEventListener("click", (e) => {
    const b = e.target.closest("[data-unloc]");
    if (b) { state.locs.delete(b.dataset.unloc); saveFilter(); renderSummary(); }
  });
  document.addEventListener("click", (e) => { if (!e.target.closest("#locPop, #locBtn")) setLocPop(false); });
  for (const box of [$("#grid"), $("#wallGrid")]) box.addEventListener("click", (e) => {      // fold a section
    const t = e.target.closest("[data-loc-toggle]");
    if (!t) return;
    const id = t.closest(".loc-head").dataset.locHead;
    if (state.collapsed.has(id)) state.collapsed.delete(id); else state.collapsed.add(id);
    regroup();
  });
}

// Dashboard name banner (Settings) + browser tab title
function applyLayout() {
  const d = state.layout.dashboard;
  $("#dashName").textContent = d.name;
  $("#dashName").hidden = !d.banner;
  document.title = d.name;
}

// ---------------------------------------------------------------- Settings: dashboard name + locations
const lay = { draft: [], def: null, armed: null, n: 0 };
function openLayoutDlg() {
  const L = state.layout.locations;
  lay.draft = locItems().map((l) => ({ id: l.id, key: l.id, name: l.name }));
  lay.def = L.default; lay.armed = null;
  $("#layName").value = state.layout.dashboard.name;
  $("#layBanner").checked = state.layout.dashboard.banner;
  $("#layError").textContent = "";
  renderLayoutDlg();
  $("#layoutDlg").showModal();
}
function renderLayoutDlg() {
  const counts = {};
  for (const p of state.printers) counts[locOf(p)] = (counts[locOf(p)] || 0) + 1;
  const defName = lay.draft.find((x) => x.key === lay.def)?.name || "the default";
  const n = lay.draft.length;
  $("#layLocs").innerHTML = lay.draft.map((x, i) => {
    const c = x.id ? counts[x.id] || 0 : 0;
    const armed = lay.armed === x.key;
    const isDef = lay.def === x.key;
    return `<li data-key="${esc(x.key)}" draggable="false">
      <button type="button" class="grip" data-loc-grip aria-label="Drag to reorder ${esc(x.name)}" title="Drag to reorder">${GRIP.match(/<svg[\s\S]*<\/svg>/)[0]}</button>
      <input value="${esc(x.name)}" maxlength="40" aria-label="Location name" placeholder="e.g. Garage">
      <label class="def" title="New printers go here"><input type="radio" name="layDef" ${isDef ? "checked" : ""}> Default</label>
      <span class="muted">${c} printer${c === 1 ? "" : "s"}</span>
      <span class="mv"><button type="button" class="icon-btn" data-up ${i === 0 ? "disabled" : ""} aria-label="Move up">↑</button><button type="button" class="icon-btn" data-down ${i === n - 1 ? "disabled" : ""} aria-label="Move down">↓</button></span>
      <button type="button" class="btn danger${armed ? " armed" : ""}" data-del ${n === 1 ? "disabled title=\"Keep at least one location\"" : ""}>${armed ? "Click again" : "Delete"}</button>
      ${armed && c ? `<small class="del-note">${c} printer${c === 1 ? "" : "s"} will move to ${esc(isDef ? lay.draft.find((y) => y.key !== x.key)?.name : defName)}.</small>` : ""}
    </li>`;
  }).join("");
}
function setupLayoutDlg() {
  $("#layoutBtn").addEventListener("click", openLayoutDlg);
  const list = $("#layLocs");
  const at = (e) => lay.draft.findIndex((x) => x.key === e.target.closest("li")?.dataset.key);
  list.addEventListener("input", (e) => { const i = at(e); if (i >= 0 && e.target.matches("input:not([type])")) lay.draft[i].name = e.target.value; });
  list.addEventListener("change", (e) => { const i = at(e); if (i >= 0 && e.target.type === "radio") lay.def = lay.draft[i].key; });
  list.addEventListener("click", (e) => {
    const i = at(e);
    if (i < 0) return;
    const swap = (j) => { [lay.draft[i], lay.draft[j]] = [lay.draft[j], lay.draft[i]]; renderLayoutDlg(); list.querySelector(`li[data-key="${CSS.escape(lay.draft[j].key)}"] ${e.target.closest("[data-up]") ? "[data-up]" : "[data-down]"}`)?.focus(); };
    if (e.target.closest("[data-up]") && i > 0) swap(i - 1);
    else if (e.target.closest("[data-down]") && i < lay.draft.length - 1) swap(i + 1);
    else if (e.target.closest("[data-del]")) {
      const x = lay.draft[i];
      if (lay.armed !== x.key) { lay.armed = x.key; renderLayoutDlg(); return; }
      lay.draft.splice(i, 1); lay.armed = null;
      if (lay.def === x.key) lay.def = lay.draft[0].key;
      renderLayoutDlg();
    }
  });
  // drag to reorder (grip)
  let dragKey = null;
  list.addEventListener("pointerdown", (e) => { const li = e.target.closest("li"); if (li) li.draggable = !!e.target.closest("[data-loc-grip]"); });
  list.addEventListener("dragstart", (e) => { dragKey = e.target.closest("li")?.dataset.key; e.dataTransfer.effectAllowed = "move"; e.dataTransfer.setData("text/plain", dragKey || ""); });
  list.addEventListener("dragover", (e) => { if (dragKey && e.target.closest("li")) e.preventDefault(); });
  list.addEventListener("drop", (e) => {
    const li = e.target.closest("li");
    if (!dragKey || !li) return;
    e.preventDefault();
    const from = lay.draft.findIndex((x) => x.key === dragKey);
    const [item] = lay.draft.splice(from, 1);
    let to = lay.draft.findIndex((x) => x.key === li.dataset.key);
    const r = li.getBoundingClientRect();
    if (e.clientY > r.top + r.height / 2) to += 1;
    lay.draft.splice(to, 0, item);
    dragKey = null; renderLayoutDlg();
  });
  list.addEventListener("dragend", () => { dragKey = null; });
  $("#layAdd").addEventListener("click", () => {
    const key = `new${++lay.n}`;
    lay.draft.push({ id: null, key, name: "" });
    renderLayoutDlg();
    list.querySelector(`li[data-key="${key}"] input`).focus();
  });
  $("#layoutForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const err = $("#layError");
    if (lay.draft.some((x) => !x.name.trim())) { err.textContent = "Every location needs a name (or delete the empty one)."; return; }
    try {
      // apply what the server saved right away; don't wait for the live update stream
      state.layout.dashboard = await api("PUT", "/api/dashboard", { name: $("#layName").value.trim() || null, banner: $("#layBanner").checked });
      state.layout.locations = await api("PUT", "/api/locations", { items: lay.draft.map((x) => (x.id ? { id: x.id, name: x.name } : { ref: x.key, name: x.name })), default: lay.def });
      applyLayout(); renderAll();
      $("#layoutDlg").close();
    } catch (ex) { err.textContent = ex.message; }
  });
}

function setupFilter() {
  $("#summary").addEventListener("click", (e) => {
    const b = e.target.closest("[data-filter]");
    if (!b) return;
    const k = b.dataset.filter;
    if (k === "all") state.filter.clear();
    else if (state.filter.has(k)) state.filter.delete(k);
    else state.filter.add(k);
    if (FILTERS.every((f) => state.filter.has(f[0]))) state.filter.clear();   // every status picked = All
    saveFilter();
    renderSummary();
  });
  $("#noMatch").addEventListener("click", (e) => {
    if (!e.target.closest("[data-filter-all]")) return;
    state.filter.clear(); state.groups.clear(); state.locs.clear(); saveFilter(); renderSummary();
  });
}

state.tagList = [];   // from /api/tags: [{name, count, printers: [ids]}], includes tags no printer has yet
state.tagOpen = new Set();

async function loadTags() {
  try { state.tagList = await api("GET", "/api/tags"); } catch { /* keep what we had */ }
  const typing = document.activeElement?.matches?.("#tagsBody input:not([type=checkbox])");
  if ($("#tagsDlg").open && !typing) renderTagsDlg();
}

function allTags() {
  const seen = new Map();
  for (const t of state.tagList) seen.set(t.name.toLowerCase(), t.name);
  for (const p of state.printers) for (const t of p.tags || []) if (!seen.has(t.toLowerCase())) seen.set(t.toLowerCase(), t);
  return [...seen.values()].sort((a, b) => a.localeCompare(b, undefined, { numeric: true }));
}

// ---------------------------------------------------------------- manage tags (add / rename / delete / which printers)
function renderTagsDlg(msg = "") {
  const has = (p, t) => (p.tags || []).some((x) => x.toLowerCase() === t.toLowerCase());
  const tags = allTags();
  const printers = [...state.printers].sort((a, b) => (a.name || "").localeCompare(b.name || "", undefined, { numeric: true }));
  $("#tagsBody").innerHTML = `
    <form class="tag-new" data-newtag>
      <input name="tag" maxlength="30" placeholder='New tag, e.g. "0.6 nozzle"' aria-label="New tag">
      <button type="submit" class="btn primary">Add tag</button>
    </form>
    ${tags.length ? `
    <p class="note">Click <b>Printers</b> to choose which printers have a tag. Renaming or deleting changes it on every
      printer. You can also add tags to one printer in its Details.</p>
    <ul class="tag-admin">${tags.map((t) => {
      const n = printers.filter((p) => has(p, t)).length;
      const open = state.tagOpen.has(t.toLowerCase());
      return `
      <li data-tag="${esc(t)}">
        <input value="${esc(t)}" maxlength="30" aria-label="Rename ${esc(t)}">
        <button type="button" class="btn" data-tagprinters aria-expanded="${open}">${n} printer${n === 1 ? "" : "s"} ${open ? "▴" : "▾"}</button>
        <button type="button" class="btn" data-rename>Rename</button>
        <button type="button" class="btn danger${state.tagArmed === t ? " armed" : ""}" data-deltag>${state.tagArmed === t ? "Click again" : "Delete"}</button>
        ${open ? `<div class="tag-printers">${printers.length ? printers.map((p) => `
          <label class="check"><input type="checkbox" data-tagpid="${esc(p.id)}"${has(p, t) ? " checked" : ""}> ${esc(p.name || p.model || p.host)}</label>`).join("")
          : `<span class="muted">No printers yet.</span>`}</div>` : ""}
      </li>`;
    }).join("")}</ul>` : `<p class="note">No tags yet. Add one above, then choose which printers have it.</p>`}
    <p class="form-error" role="alert">${esc(msg)}</p>`;
}

async function setTagPrinters(name, ids) {
  state.tagList = state.tagList.filter((t) => t.name.toLowerCase() !== name.toLowerCase());
  state.tagList.push(await api("PUT", "/api/tags", { name, printers: ids }));
}

function setupTags() {
  $("#tagsBtn").addEventListener("click", () => {
    state.tagArmed = null; state.tagOpen.clear(); renderTagsDlg(); $("#tagsDlg").showModal(); loadTags();
  });
  $("#tagsBody").addEventListener("submit", async (e) => {
    if (!e.target.closest("[data-newtag]")) return;
    e.preventDefault();
    const name = e.target.tag.value.trim().replace(/\s+/g, " ");
    if (!name) return;
    const known = allTags().find((t) => t.toLowerCase() === name.toLowerCase());
    if (known) { state.tagOpen.add(known.toLowerCase()); renderTagsDlg(`"${known}" already exists.`); return; }
    try {
      await setTagPrinters(name, []);
      state.tagOpen.add(name.toLowerCase());
      renderTagsDlg();
    } catch (err) { renderTagsDlg(err.message); }
  });
  $("#tagsBody").addEventListener("change", async (e) => {
    const box = e.target.closest("[data-tagpid]");
    if (!box) return;
    const t = box.closest("li[data-tag]").dataset.tag;
    const ids = [...box.closest(".tag-printers").querySelectorAll("[data-tagpid]:checked")].map((x) => x.dataset.tagpid);
    try { await setTagPrinters(t, ids); renderTagsDlg(); } catch (err) { renderTagsDlg(err.message); }
    $(`#tagsBody li[data-tag="${CSS.escape(t)}"] [data-tagpid="${CSS.escape(box.dataset.tagpid)}"]`)?.focus();
  });
  $("#tagsBody").addEventListener("click", async (e) => {
    const li = e.target.closest("li[data-tag]");
    if (!li) return;
    const old = li.dataset.tag;
    if (e.target.closest("[data-tagprinters]")) {
      const k = old.toLowerCase();
      state.tagOpen.has(k) ? state.tagOpen.delete(k) : state.tagOpen.add(k);
      renderTagsDlg();
    }
    if (e.target.closest("[data-rename]")) {
      const nw = li.querySelector("input").value.trim();
      if (!nw || nw === old) return;
      try {
        await api("POST", "/api/tags/retag", { old, new: nw });
        if (state.tagOpen.delete(old.toLowerCase())) state.tagOpen.add(nw.toLowerCase());
        await loadTags(); renderTagsDlg();
      } catch (err) { renderTagsDlg(err.message); }
    }
    if (e.target.closest("[data-deltag]")) {
      if (state.tagArmed !== old) { state.tagArmed = old; renderTagsDlg(); return; }
      state.tagArmed = null;
      try { await api("POST", "/api/tags/retag", { old, new: null }); await loadTags(); renderTagsDlg(); }
      catch (err) { renderTagsDlg(err.message); }
    }
  });
  $("#tagsBody").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && e.target.closest("li[data-tag] > input")) { e.preventDefault(); e.target.closest("li").querySelector("[data-rename]").click(); }
  });
}

// ---------------------------------------------------------------- usage (Details) + job history
state.usage = {};
const fmtH = (s) => s == null ? "" : `${(s / 3600).toLocaleString(undefined, { minimumFractionDigits: 1, maximumFractionDigits: 1 })} h`;
const fmtAvg = (s) => s < 3600 ? `${Math.round(s / 60)} min` : fmtH(s);   // short jobs read better in minutes
const fmtN = (n) => n == null ? "" : n.toLocaleString();
const jobsN = (n) => `${fmtN(n)} job${n === 1 ? "" : "s"}`;
const NO_RATE = `<span class="sub">No ended jobs yet</span>`;
const fmtDate = (iso) => iso ? new Date(iso).toLocaleDateString([], { month: "short", day: "numeric", year: "numeric" }) : "";
const fmtWhen = (iso) => iso ? new Date(iso).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) : "";
const RESULT = { finished: ["Finished", "var(--done)"], failed: ["Failed", "var(--fail)"], cancelled: ["Cancelled", "var(--fail)"],
  unknown: ["Ended (result unknown)", "var(--idle)"], running: ["In progress", "var(--printing)"] };

async function loadUsage(id) {
  try { state.usage[id] = await api("GET", `/api/printers/${id}/usage`); } catch (_) { return; }
  if (state.detailsId === id) {
    const sec = $("#usageSec");
    if (sec && !sec.contains(document.activeElement)) morph(sec, usageHTML(state.printers.find((x) => x.id === id)));
  }
}

function jobRow(j, withPrinter = false) {
  const [label, color] = RESULT[j.result] || RESULT.unknown;
  return `<li class="job-row"><i style="background:${color}" title="${esc(label)}"></i>
    <span><b>${esc(j.job || "Unnamed job")}</b><br><span class="sub">${esc([withPrinter ? j.printer_name : "", fmtWhen(j.started_at), label,
      j.source === "printer" ? "from the printer's history" : ""].filter(Boolean).join(", "))}</span></span>
    <span class="sub">${esc(fmtH(j.print_seconds))}</span></li>`;
}

function usageHTML(p) {
  const u = p && state.usage[p.id];
  if (!u) return `<h3>Usage</h3><p class="muted">Loading…</p>`;
  const src = { printer: "from the printer", manual: "entered by you" }[u.baseline_source] || "not set";
  const a = u.audit;
  const auditHTML = !a ? "" : `
    ${a.last_reset_at ? `<p class="note">The printer's own counter was reset on ${esc(fmtDate(a.last_reset_at))}. Totals were kept, and the check below adds back what the reset cleared.</p>` : ""}
    ${a.basis === "finished" ? `<p class="note">This printer's counter only adds successfully finished prints (Bambu), so the check leaves out failed and cancelled jobs.</p>` : ""}
    ${a.flagged ? `<div class="note warn">Doesn't match the printer: it says <b>${esc(fmtH(a.printer_seconds))}</b>${a.printer_jobs != null ? ` and ${esc(fmtN(a.printer_jobs))} jobs` : ""}, the dashboard says <b>${esc(fmtH(a.dashboard_seconds))}</b>${a.printer_jobs != null ? ` and ${esc(fmtN(a.dashboard_jobs))} jobs` : ""}.
       <div class="row-actions" style="margin-top:8px"><button type="button" class="btn" data-usage="accept">Use the printer's numbers</button></div></div>`
      : `<p class="note">Matches the printer's own counter (${esc(fmtH(a.printer_seconds))}), checked ${esc(fmtWhen(a.checked_at))}.</p>`}`;
  const edit = state.usageEdit ? `
    <form class="usage-form" data-usage-form>
      <label class="field"><span>Hours before tracking</span><input name="hours" type="number" min="0" step="0.1" value="${(u.baseline_seconds / 3600).toFixed(1)}"></label>
      <label class="field"><span>Jobs before tracking</span><input name="jobs" type="number" min="0" step="1" value="${u.baseline_jobs}"></label>
      <div class="row-actions"><button class="btn primary" type="submit">Save</button><button class="btn ghost" type="button" data-usage="cancel">Cancel</button></div>
      <p class="note">Changes are logged with the date and the old values.</p>
    </form>` : "";
  const link = u.linkable.length ? `
    <details class="link-hist"><summary>Link to previous history</summary>
      <p class="note">For a replaced mainboard or a rebuilt printer: move an old printer's hours and jobs onto this one.</p>
      <div class="row-actions"><select data-link-key>${u.linkable.map((o) => `<option value="${esc(o.key)}">${esc(o.last_name || o.key)}: ${esc(fmtH(o.total_seconds))}, ${esc(jobsN(o.jobs))}</option>`).join("")}</select>
      <button type="button" class="btn" data-usage="link">Link</button></div>
    </details>` : "";
  return `<h3>Usage</h3>
    ${kv([
      ["Total print time", `<b>${esc(fmtH(u.total_seconds))}</b>`],
      ["Total jobs", esc(fmtN(u.total_jobs))],
      ["Success rate", u.success_rate == null ? NO_RATE : `${u.success_rate}% <span class="sub">(${u.finished} finished, ${u.failed} failed since tracking began)</span>`],
      ["Before tracking", `${esc(fmtH(u.baseline_seconds))}, ${esc(jobsN(u.baseline_jobs))} <span class="sub">(${src})</span> ${state.usageEdit ? "" : `<button type="button" class="link-btn" data-usage="edit">Edit</button>`}`],
      ["Tracked here", `${esc(fmtH(u.tracked_seconds))}, ${esc(jobsN(u.tracked_jobs))} <span class="sub">since ${esc(fmtDate([u.first_job_at, u.tracking_since].filter(Boolean).sort()[0]))}</span>`],
      ["Average per job", u.avg_job_seconds == null ? `<span class="sub">No ended jobs yet</span>`
        : `${esc(fmtAvg(u.avg_job_seconds))} <span class="sub">(${esc(jobsN(u.avg_jobs))} tracked here${u.avg_jobs < u.tracked_jobs ? ", not counting the one printing" : ""})</span>`],
    ])}
    ${edit}${auditHTML}
    ${u.recent.length ? `<h4 class="minor">Recent jobs</h4><ul class="job-list">${u.recent.map((j) => jobRow(j)).join("")}</ul>` : `<p class="muted">No jobs recorded yet.</p>`}
    <div class="row-actions"><button type="button" class="btn" data-usage="history">All jobs for this printer</button></div>
    ${link}`;
}

function setupUsage() {
  const body = $("#detailsBody");
  body.addEventListener("click", async (e) => {
    const b = e.target.closest("[data-usage]");
    if (!b) return;
    const id = state.detailsId;
    const act = b.dataset.usage;
    try {
      if (act === "edit") { state.usageEdit = true; }
      else if (act === "cancel") { state.usageEdit = false; }
      else if (act === "accept") { state.usage[id] = await api("POST", `/api/printers/${id}/usage/accept-reported`); }
      else if (act === "link") {
        const key = body.querySelector("[data-link-key]").value;
        state.usage[id] = await api("POST", `/api/printers/${id}/usage/link`, { key });
      } else if (act === "history") { $("#detailsDlg").close(); openHistory(id); return; }
    } catch (err) { alertInline(err.message); }
    morph($("#usageSec"), usageHTML(state.printers.find((x) => x.id === id)));
  });
  body.addEventListener("submit", async (e) => {
    if (!e.target.matches("[data-usage-form]")) return;
    e.preventDefault();
    const id = state.detailsId, fd = new FormData(e.target);
    try {
      state.usage[id] = await api("PUT", `/api/printers/${id}/usage`, { hours: Number(fd.get("hours")), jobs: Number(fd.get("jobs")) });
      state.usageEdit = false;
    } catch (err) { alertInline(err.message); return; }
    $("#usageSec").innerHTML = usageHTML(state.printers.find((x) => x.id === id));
  });
}
function alertInline(msg) { const el = $("#editError"); if (el) el.textContent = msg; }

// Job history (Settings -> Job history)
const hist = { printer: "", result: "", rows: [], more: false };
async function loadHistory(reset = false) {
  if (reset) hist.rows = [];
  const before = hist.rows.length ? hist.rows[hist.rows.length - 1].id : "";
  const q = new URLSearchParams({ limit: "50", ...(hist.printer && { printer_id: hist.printer }), ...(hist.result && { result: hist.result }), ...(before && { before }) });
  const [rows, sum] = await Promise.all([api("GET", `/api/history?${q}`), reset ? api("GET", "/api/history/summary") : null]);
  hist.rows = hist.rows.concat(rows);
  hist.more = rows.length === 50;
  if (sum) hist.summary = sum;
  renderHistory();
}
function renderHistory() {
  const s = hist.summary;
  const f = s?.fleet;
  $("#historyBody").innerHTML = `
    ${s ? `<section class="sec"><h3>All printers</h3>${kv([["Print time", `<b>${esc(fmtH(f.total_seconds))}</b>`], ["Jobs", esc(fmtN(f.total_jobs))],
      ["Success rate", f.success_rate == null ? NO_RATE : `${f.success_rate}% <span class="sub">(tracked jobs)</span>`]])}
      <table class="usage-table"><thead><tr><th>Printer</th><th>Hours</th><th>Jobs</th><th>Success</th></tr></thead><tbody>
      ${s.printers.map((r) => `<tr><td>${esc(r.name)}${r.audit_flagged ? ` <span class="flag" title="Doesn't match the printer's own counter. See the printer's Details">check</span>` : ""}</td>
        <td>${esc(fmtH(r.total_seconds))}</td><td>${esc(fmtN(r.total_jobs))}</td><td>${r.success_rate == null ? "" : r.success_rate + "%"}</td></tr>`).join("")}
      </tbody></table></section>` : ""}
    <section class="sec"><h3>Jobs</h3>
      <div class="row-actions">
        <select data-hist="printer" aria-label="Printer"><option value="">All printers</option>${state.printers.map((p) => `<option value="${esc(p.id)}" ${hist.printer === p.id ? "selected" : ""}>${esc(p.name)}</option>`).join("")}</select>
        <select data-hist="result" aria-label="Result"><option value="">Any result</option>${["finished", "failed", "cancelled", "unknown", "running"].map((r) => `<option value="${r}" ${hist.result === r ? "selected" : ""}>${esc(RESULT[r][0])}</option>`).join("")}</select>
        <a class="btn ghost" href="/api/history.csv" download>Export CSV</a>
      </div>
      ${hist.rows.length ? `<ul class="job-list">${hist.rows.map((j) => jobRow(j, true)).join("")}</ul>` : `<p class="muted">No jobs recorded yet. Jobs are recorded from now on as printers run.</p>`}
      ${hist.more ? `<button type="button" class="btn" data-hist-more>Show more</button>` : ""}
    </section>`;
}
function openHistory(printerId = "") {
  hist.printer = printerId; hist.result = "";
  $("#historyBody").innerHTML = `<p class="muted">Loading…</p>`;
  $("#historyDlg").showModal();
  loadHistory(true);
}
function setupHistory() {
  $("#historyBtn").addEventListener("click", () => openHistory(""));
  $("#historyBody").addEventListener("change", (e) => {
    const sel = e.target.closest("[data-hist]");
    if (!sel) return;
    hist[sel.dataset.hist] = sel.value;
    loadHistory(true);
  });
  $("#historyBody").addEventListener("click", (e) => { if (e.target.closest("[data-hist-more]")) loadHistory(false); });
}

// ---------------------------------------------------------------- Govee plugs (power)
state.govee = null;            // {linked, error, plugs:[{sku, device, name, on, online, printer_id}], suggestions}
state.powerLog = {};
state.powerConfirm = null;     // {id, reason, type} when turning off needs the typed name
state.powerArm = null;         // printer id armed for a plain (safe) turn-off
async function loadGovee() {
  try { state.govee = await api("GET", "/api/govee"); } catch (_) { state.govee = null; }
  if ($("#plugsDlg").open) renderPlugsDlg();
  if (state.detailsId) renderDetails();
}
async function loadPowerLog(id) {
  try { state.powerLog[id] = (await api("GET", `/api/printers/${id}/power`)).log; } catch (_) { return; }
  rerenderPower();
}
function powerHTML(p) {
  const g = state.govee;
  if (!g?.linked) return "";
  const pw = p.power;
  const opts = (sel) => `<option value="">No plug</option>` + g.plugs.map((x) => {
    const other = x.printer_id && x.printer_id !== p.id ? state.printers.find((q) => q.id === x.printer_id)?.name : null;
    return `<option value="${esc(x.device)}" ${sel === x.device ? "selected" : ""} ${other ? "disabled" : ""}>${esc(x.name)}${other ? ` (used by ${esc(other)})` : ""}</option>`;
  }).join("");
  const log = (state.powerLog[p.id] || []).map((l) => `<li>${esc(fmtWhen(l.at))}: turned <b>${esc(l.action)}</b>${l.ok ? "" : " - <span class=\"fail-text\">failed</span>"}${l.note ? ` <span class="sub">(${esc(l.note)})</span>` : ""}</li>`).join("");
  const c = state.powerConfirm && state.powerConfirm.id === p.id ? state.powerConfirm : null;
  if (!pw) {
    const sug = g.suggestions?.[p.id] || "";
    return `<section class="sec" id="powerSec"><h3>Power</h3>
      <p class="note">Pick the Govee plug this printer is plugged into${sug ? " (suggested from the plug names)" : ""}.</p>
      <div class="row-actions"><select data-plug-pick aria-label="Plug">${opts(sug)}</select><button class="btn" type="button" data-plug-save>Save</button></div>
      <p class="form-error" id="powerError" role="alert"></p></section>`;
  }
  const stateTxt = pw.on === true ? "On" : pw.on === false ? "Off" : "Unknown";
  const checked = pw.from_printer ? "the printer is online" : pw.checked_at ? `checked ${fmtWhen(pw.checked_at)}` : "";
  return `<section class="sec" id="powerSec"><h3>Power</h3>
    ${kv([["Plug", esc(pw.name)], ["Plug power", `<b>${stateTxt}</b>${pw.online === false ? ` <span class="sub">(plug offline)</span>` : ""}${checked ? ` <span class="sub">${esc(checked)}</span>` : ""}`]])}
    ${c ? `<div class="power-warn" role="alertdialog" aria-labelledby="pwTitle">
        <p id="pwTitle"><b>Emergency stop: cut power to ${esc(p.name)}?</b></p>
        <p>${esc(c.reason)}</p>
        <label class="field"><span>Type <b>${esc(c.type)}</b> to confirm</span><input data-power-type autocomplete="off" spellcheck="false"></label>
        <div class="row-actions"><button class="btn danger" type="button" data-power-agree disabled>${state.powerBusy?.id === p.id ? "Turning off…" : "I understand, turn it off"}</button><button class="btn ghost" type="button" data-power-cancel>Cancel</button></div>
      </div>` : `<div class="row-actions">
        ${(() => { const busy = state.powerBusy && state.powerBusy.id === p.id ? state.powerBusy : null; return `
        <button class="btn" type="button" data-power="on" ${busy || pw.on === true || pw.on_wait_s > 0 ? "disabled" : ""}>${busy?.on === true ? "Turning on…" : pw.on_wait_s > 0 ? `Turn on in ${pw.on_wait_s} s` : "Turn on"}</button>
        <button class="btn danger${state.powerArm === p.id ? " armed" : ""}" type="button" data-power="off" ${busy || pw.on === false ? "disabled" : ""}>${busy?.on === false ? "Turning off…" : state.powerArm === p.id ? "Click again to turn off" : "Turn off"}</button>`; })()}
      </div>`}
    <p class="form-error" id="powerError" role="alert"></p>
    ${log ? `<h4 class="minor">Recent</h4><ul class="power-log">${log}</ul>` : ""}
    ${cycleSettingsHTML(p)}
    <details class="link-hist"><summary>Change plug</summary><div class="row-actions"><select data-plug-pick aria-label="Plug">${opts(p.power?.device || "")}</select><button class="btn" type="button" data-plug-save>Save</button></div></details>
  </section>`;
}
function cycleSettingsHTML(p) {
  const a = p.autocycle || { enabled: false, time: "06:00", idle_min: 30 };
  const c = cycleInfo(p);
  return `<form class="cycle-form" data-cycle-form>
    <h4 class="minor">Automatic power cycle</h4>
    <label class="check toggle-row"><input type="checkbox" class="toggle" name="enabled" ${a.enabled ? "checked" : ""}><span>Power cycle once a day</span></label>
    <div class="cycle-fields">
      <label class="field"><span>Daily at</span><input type="time" name="time" value="${esc(a.time)}" required></label>
      <label class="field"><span>After idle for (minutes)</span><input type="number" name="idle_min" min="1" max="1440" step="1" value="${esc(a.idle_min)}" required></label>
    </div>
    <p class="note">Plug off, 15 seconds, plug on. Only when the printer is Finished or Idle with a print since the last power cycle, the nozzle is cool and nothing else is running. Skipped that day if it was power cycled from the dashboard less than 12 hours before; more than 24 hours since the last one and it runs once the current print is done.${a.last_cycle ? ` Last power cycle: ${esc(fmtWhen(a.last_cycle))}.` : ""}${c ? ` Now: ${esc(c.text)}${c.tip ? ` (${esc(c.tip)})` : ""}.` : ""} <a href="https://github.com/pomeroyfamily/pf-printfloor/blob/main/docs/govee.md#5-optional-daily-power-cycle" target="_blank" rel="noopener">How it works</a></p>
    <div class="row-actions"><button class="btn" type="submit">Save</button></div>
    <p class="form-error" data-cycle-error role="alert"></p>
  </form>`;
}
document.addEventListener("submit", async (e) => {
  const f = e.target.closest("[data-cycle-form]");
  if (!f) return;
  e.preventDefault();
  const id = state.detailsId || state.powerDlgId;
  const fd = new FormData(f);
  let tz = null;
  try { tz = Intl.DateTimeFormat().resolvedOptions().timeZone; } catch (_) { /* old browser */ }
  try {
    await api("PUT", `/api/printers/${id}/autocycle`, { enabled: fd.get("enabled") === "on", time: fd.get("time"), idle_min: Number(fd.get("idle_min")), tz });
    f.querySelector("[data-cycle-error]").textContent = "Saved.";
  } catch (err) { f.querySelector("[data-cycle-error]").textContent = err.message; }
});
async function powerAction(id, on, confirm) {
  const err = () => powerRoot().querySelector("#powerError");
  state.powerBusy = { id, on }; rerenderPower();
  try {
    const r = await api("POST", `/api/printers/${id}/power`, { on, ...(confirm ? { confirm } : {}) }).finally(() => { state.powerBusy = null; });
    if (r.wait_s) { if (err()) err().textContent = `Wait ${r.wait_s} s before turning it back on.`; return; }
    if (r.needs_confirm) { state.powerConfirm = { id, reason: r.reason, type: r.type }; state.powerArm = null; rerenderPower(); powerRoot().querySelector("[data-power-type]")?.focus(); return; }
    state.powerConfirm = null; state.powerArm = null;
    if (!on) startOnCountdown(id);
    if (r.unconfirmed && powerRoot().querySelector("#powerError")) setTimeout(() => {
      const el = powerRoot().querySelector("#powerError");
      if (el) el.textContent = "Sent, but Govee wouldn't let the dashboard read the plug back to confirm. Check the plug or the Govee app.";
    }, 300);
    await loadPowerLog(id);
  } catch (e) { state.powerArm = null; rerenderPower(); if (err()) err().textContent = e.message; loadPowerLog(id); }
}
// After turning off, count the "Turn on in N s" button down locally (the server enforces the same wait).
function startOnCountdown(id) {
  const t = setInterval(() => {
    const p = state.printers.find((x) => x.id === id);
    if (!p?.power || !(p.power.on_wait_s > 0)) { clearInterval(t); rerenderPower(); return; }
    p.power.on_wait_s -= 1;
    rerenderPower();
  }, 1000);
}
function setupPower() {
  for (const root of [$("#detailsBody"), $("#powerBody")]) {
    const idOf = () => (root.id === "powerBody" ? state.powerDlgId : state.detailsId);
    root.addEventListener("click", async (e) => {
      const id = idOf();
      const b = e.target.closest("[data-power]");
      if (b) {
        if (b.dataset.power === "on") { powerAction(id, true); return; }
        if (state.powerArm !== id) { state.powerArm = id; rerenderPower(); setTimeout(() => { if (state.powerArm === id) { state.powerArm = null; rerenderPower(); } }, 6000); return; }
        powerAction(id, false); return;
      }
      if (e.target.closest("[data-power-cancel]")) { state.powerConfirm = null; rerenderPower(); return; }
      if (e.target.closest("[data-power-agree]")) { powerAction(id, false, root.querySelector("[data-power-type]").value); return; }
      if (e.target.closest("[data-plug-save]")) {
        const dev = e.target.closest(".row-actions").querySelector("[data-plug-pick]").value || null;
        try { await api("PUT", `/api/printers/${id}/plug`, { device: dev }); await loadGovee(); }
        catch (err) { root.querySelector("#powerError").textContent = err.message; }
      }
    });
    root.addEventListener("input", (e) => {
      if (!e.target.matches("[data-power-type]") || !state.powerConfirm) return;
      root.querySelector("[data-power-agree]").disabled = e.target.value.trim().toLowerCase() !== state.powerConfirm.type.trim().toLowerCase();
    });
  }
}
// Power window (card power button): same controls as Details > Power.
state.powerDlgId = null;
function powerRoot() { return $("#powerDlg").open ? $("#powerBody") : $("#detailsBody"); }
function rerenderPower() { if ($("#powerDlg").open) renderPowerDlg(); if (state.detailsId) renderDetails(); }
function renderPowerDlg() {
  const p = state.printers.find((x) => x.id === state.powerDlgId);
  if (!p || !p.power) { $("#powerDlg").close(); return; }
  $("#powerTitle").textContent = `Power: ${p.name}`;
  const body = $("#powerBody");
  const html = powerHTML(p);
  if (body.dataset.for === p.id && body.childNodes.length) morph(body, html); else { body.innerHTML = html; body.dataset.for = p.id; }
}
function openPower(id) {
  state.powerDlgId = id; state.powerConfirm = null; state.powerArm = null;
  $("#powerBody").innerHTML = ""; $("#powerBody").dataset.for = "";
  renderPowerDlg();
  if (!$("#powerDlg").open) $("#powerDlg").showModal();
  loadPowerLog(id);
}
function renderPlugsDlg() {
  const g = state.govee;
  const box = $("#plugsBody");
  if (!g?.linked) {
    box.innerHTML = `<p class="note">Lets you turn printers on and off from the dashboard, including an emergency stop. Get an API key in the Govee Home app (person tab, gear icon, Apply for API Key); Govee emails it to you. <a href="https://github.com/pomeroyfamily/pf-printfloor/blob/main/docs/govee.md" target="_blank" rel="noopener">Step-by-step guide</a></p>
      <form id="goveeForm" novalidate><label class="field"><span>Govee API key</span><input name="key" type="password" autocomplete="off" required></label>
      <p class="form-error" id="goveeError" role="alert"></p>
      <div class="row-actions"><button class="btn primary" type="submit">Link Govee account</button></div></form>
      <p class="note">The key is stored on the dashboard server only and never shown again.</p>`;
    return;
  }
  const u = g.usage || {};
  // Pick the printer for each plug right here. Unassigned plugs are pre-filled when the plug's name matches a
  // printer (suggestion); nothing is saved until "Save".
  const sugByDev = Object.fromEntries(Object.entries(g.suggestions || {}).map(([pid, dev]) => [dev, pid]));
  const draft = state.plugDraft || (state.plugDraft = {});
  const cyc = state.cycleDraft || (state.cycleDraft = {});
  const chosen = (x) => x.device in draft ? draft[x.device] : (x.printer_id || sugByDev[x.device] || "");
  const cycleNow = (pid) => !!state.printers.find((q) => q.id === pid)?.autocycle?.enabled;
  const cycleOn = (x) => { const pid = chosen(x); return !!pid && (x.device in cyc ? cyc[x.device] : cycleNow(pid)); };
  const changed = g.plugs.filter((x) => chosen(x) !== (x.printer_id || "") || (chosen(x) && cycleOn(x) !== cycleNow(chosen(x))));
  const picks = g.plugs.map(chosen).filter(Boolean);
  const dup = picks.find((pid, i) => picks.indexOf(pid) !== i);
  const printers = [...state.printers].sort((a, b) => a.name.localeCompare(b.name));
  const sel = (x) => `<select data-assign-plug="${esc(x.device)}" aria-label="Printer on plug ${esc(x.name)}">
      <option value="">Not used</option>${printers.map((q) => `<option value="${esc(q.id)}" ${chosen(x) === q.id ? "selected" : ""}>${esc(q.name)}</option>`).join("")}</select>`;
  box.innerHTML = `<p class="note">Linked. ${g.plugs.length} plug${g.plugs.length === 1 ? "" : "s"} on the account. Pick the printer each plug powers, then <b>Save</b>. Plugs named like a printer are filled in for you. <a href="https://github.com/pomeroyfamily/pf-printfloor/blob/main/docs/govee.md" target="_blank" rel="noopener">How Govee plugs work</a></p>
    ${g.error ? `<p class="note warn">${esc(g.error)}</p>` : ""}
    <table class="usage-table plug-assign"><thead><tr><th>Plug</th><th>Power</th><th>Printer</th><th title="Optional: power cycle the printer once a day">Daily power cycle</th></tr></thead><tbody>
    ${g.plugs.map((x) => {
      const isSug = !x.printer_id && !(x.device in draft) && sugByDev[x.device];
      const dirty = chosen(x) !== (x.printer_id || "");
      const pid = chosen(x);
      return `<tr class="${dirty || (pid && cycleOn(x) !== cycleNow(pid)) ? "dirty" : ""}"><td>${esc(x.name)}</td><td>${x.on === true ? "On" : x.on === false ? "Off" : ""}</td>
        <td>${sel(x)}${isSug ? ` <span class="sub">suggested</span>` : ""}</td>
        <td><input type="checkbox" class="toggle" data-cycle-plug="${esc(x.device)}" ${cycleOn(x) ? "checked" : ""} ${pid ? "" : "disabled"} aria-label="Daily power cycle for the printer on ${esc(x.name)}"></td></tr>`;
    }).join("")}
    </tbody></table>
    <p class="note">Daily power cycle (optional): the plug turns off for 15 seconds, then back on, once a day. New ones start at <b>6:00 AM</b>, only after the printer has been finished or idle for <b>30 minutes</b>, is cool and has printed since the last cycle. Change the time and the idle minutes for each printer in its <b>Details → Power</b>. <a href="https://github.com/pomeroyfamily/pf-printfloor/blob/main/docs/govee.md#5-optional-daily-power-cycle" target="_blank" rel="noopener">How it works</a></p>
    <p class="form-error" id="plugAssignError" role="alert">${dup ? `${esc(state.printers.find((q) => q.id === dup)?.name || "A printer")} is picked for two plugs.` : ""}</p>
    <div class="row-actions"><button class="btn primary" type="button" data-assign-save ${changed.length && !dup ? "" : "disabled"}>${changed.length ? `Save ${changed.length} change${changed.length === 1 ? "" : "s"}` : "Saved"}</button></div>
    <p class="note">Govee requests today: ${u.sent_today ?? 0}${u.remaining != null ? ` (Govee says ${u.remaining.toLocaleString()} left of ${u.daily_limit.toLocaleString()})` : ` of about ${(u.daily_limit || 10000).toLocaleString()} allowed per day`}. A plug's state is checked every 5 minutes while its printer is offline${u.polling ? "" : ". <b>Checking is paused for today to keep requests free for turning plugs on and off.</b>"}</p>
    <div class="row-actions" style="margin-top:12px"><button class="btn danger${state.govArm ? " armed" : ""}" type="button" data-govee-unlink>${state.govArm ? "Click again to unlink" : "Unlink Govee"}</button></div>`;
}
async function savePlugAssignments() {
  const g = state.govee, draft = state.plugDraft || {};
  const sugByDev = Object.fromEntries(Object.entries(g.suggestions || {}).map(([pid, dev]) => [dev, pid]));
  const want = Object.fromEntries(g.plugs.map((x) => [x.device, x.device in draft ? draft[x.device] : (x.printer_id || sugByDev[x.device] || "")]));
  const err = $("#plugAssignError");
  try {
    // 1) clear printers that lose their plug or get a different one (so no "already set for" conflicts), 2) set
    const clear = new Set(), set = [];
    for (const x of g.plugs) {
      const to = want[x.device];
      if (to === (x.printer_id || "")) continue;
      if (x.printer_id) clear.add(x.printer_id);
      if (to) { clear.add(to); set.push([to, x.device]); }
    }
    for (const pid of clear) await api("PUT", `/api/printers/${pid}/plug`, { device: null });
    for (const [pid, dev] of set) await api("PUT", `/api/printers/${pid}/plug`, { device: dev });
    // daily power cycle toggles: keep each printer's own time / idle minutes (defaults 6:00 AM, 30 min)
    let tz = null;
    try { tz = Intl.DateTimeFormat().resolvedOptions().timeZone; } catch (_) { /* old browser */ }
    const cyc = state.cycleDraft || {};
    for (const x of g.plugs) {
      const pid = want[x.device];
      if (!pid || !(x.device in cyc)) continue;
      const a = state.printers.find((q) => q.id === pid)?.autocycle || {};
      if (!!a.enabled === cyc[x.device]) continue;
      await api("PUT", `/api/printers/${pid}/autocycle`, { enabled: cyc[x.device], time: a.time || "06:00", idle_min: a.idle_min || 30, tz });
    }
    state.plugDraft = {}; state.cycleDraft = {};
    await loadGovee();
    renderPlugsDlg();
  } catch (e) { if (err) err.textContent = e.message; await loadGovee(); }
}
function setupPlugsDlg() {
  $("#plugsBtn").addEventListener("click", async () => { state.govArm = false; state.plugDraft = {}; state.cycleDraft = {}; renderPlugsDlg(); $("#plugsDlg").showModal(); await loadGovee(); });
  $("#plugsBody").addEventListener("change", (e) => {
    const cdev = e.target.dataset?.cyclePlug;
    if (cdev != null) {
      state.cycleDraft = { ...(state.cycleDraft || {}), [cdev]: e.target.checked };
      renderPlugsDlg();
      $(`[data-cycle-plug="${CSS.escape(cdev)}"]`)?.focus();
      return;
    }
    const dev = e.target.dataset?.assignPlug;
    if (dev == null) return;
    state.plugDraft = { ...(state.plugDraft || {}), [dev]: e.target.value };
    renderPlugsDlg();
    $(`[data-assign-plug="${CSS.escape(dev)}"]`)?.focus();
  });
  $("#plugsBody").addEventListener("submit", async (e) => {
    if (e.target.id !== "goveeForm") return;
    e.preventDefault();
    const key = new FormData(e.target).get("key").trim();
    try { state.govee = await api("PUT", "/api/govee", { key }); renderPlugsDlg(); }
    catch (err) { $("#goveeError").textContent = err.message; }
  });
  $("#plugsBody").addEventListener("click", async (e) => {
    const save = e.target.closest("[data-assign-save]");
    if (save) { save.disabled = true; save.textContent = "Saving…"; await savePlugAssignments(); return; }
    if (!e.target.closest("[data-govee-unlink]")) return;
    if (!state.govArm) { state.govArm = true; renderPlugsDlg(); return; }
    state.govArm = false;
    await api("DELETE", "/api/govee"); await loadGovee();
  });
}

// ---------------------------------------------------------------- cameras
// Popup: one live MJPEG stream from the server. Wall: each tile asks for the next frame (short requests, so many
// tiles don't run into the browser's ~6-connections-per-site limit). Nothing streams unless one of these is open.
function openCamera(id) {
  const p = state.printers.find((x) => x.id === id);
  if (!p) return;
  const dlg = $("#camDlg"), img = $("#camImg"), msg = $("#camMsg");
  $("#camTitle").textContent = p.name;
  $("#camSub").textContent = p.camera?.kind === "external" ? "External camera" : "Built-in camera";
  msg.textContent = "Connecting to the camera…"; msg.hidden = false; img.hidden = true;
  img.onload = () => { msg.hidden = true; img.hidden = false; };
  img.onerror = async () => {
    if (!dlg.open) return;
    let why = "";
    try { why = (await api("GET", `/api/printers/${id}/camera`)).error || ""; } catch (_) { /* ignore */ }
    msg.textContent = `The camera isn't sending a picture.${why ? " " + why : ""} Retrying…`; msg.hidden = false; img.hidden = true;
    setTimeout(() => { if (dlg.open && dlg.dataset.for === id) img.src = `/api/printers/${id}/camera.mjpg?r=${Date.now()}`; }, 4000);
  };
  dlg.dataset.for = id;
  $("#camFrame").hidden = true; $("#camFrame").removeAttribute("src");
  $("#camSmooth").hidden = true; $("#camSmooth").setAttribute("aria-pressed", "false");
  img.src = `/api/printers/${id}/camera.mjpg?r=${Date.now()}`;
  if (!dlg.open) dlg.showModal();
  setTimeout(async () => {                      // Paxx U1: offer its own WebRTC player (smooth, home network only)
    if (!dlg.open || dlg.dataset.for !== id) return;
    try {
      const info = await api("GET", `/api/printers/${id}/camera`);
      if (info.player_url) { $("#camSmooth").hidden = false; $("#camSmooth").dataset.url = info.player_url; }
    } catch (_) { /* ignore */ }
  }, 2500);
}
function setSmooth(on) {
  const id = $("#camDlg").dataset.for, img = $("#camImg"), fr = $("#camFrame");
  $("#camSmooth").setAttribute("aria-pressed", String(on));
  $("#camSmooth").textContent = on ? "Back to normal view" : "Smooth video (home network only)";
  if (on) {
    img.onerror = null; img.removeAttribute("src"); img.hidden = true;         // free the server stream
    fr.src = $("#camSmooth").dataset.url; fr.hidden = false; $("#camMsg").hidden = true;
  } else {
    fr.removeAttribute("src"); fr.hidden = true;
    openCamera(id);
  }
}
function closeCameraStream() {
  const img = $("#camImg"); img.onerror = null; img.removeAttribute("src");
  $("#camFrame").removeAttribute("src"); $("#camDlg").dataset.for = "";
}

var wall = { on: false, loops: new Map() };   // var: renderSummary may run before this line
function camPrinters() { return sortedPrinters().filter((p) => shown(p)); }
function renderWall() {
  const box = $("#wall");
  const ps = camPrinters();
  const withCam = ps.filter((p) => p.camera?.kind), without = ps.filter((p) => !p.camera?.kind);
  const ids = withCam.map((p) => p.id);
  for (const el of [...box.querySelectorAll(".cam-tile")]) if (!ids.includes(el.dataset.id)) stopTile(el.dataset.id);
  const grid = $("#wallGrid");
  const nodes = [];
  for (const [l, list] of sections(withCam)) {
    if (l) nodes.push(locHeadEl(grid, l));
    for (const p of list) {
      let el = grid.querySelector(`:scope > [data-id="${CSS.escape(p.id)}"]`);
      if (!el) {
        el = document.createElement("figure");
        el.className = "cam-tile"; el.dataset.id = p.id;
        el.innerHTML = `<button type="button" class="cam-open" data-cam-open aria-label="Open ${esc(p.name)} camera"><img alt="${esc(p.name)} camera" hidden><span class="cam-msg">Connecting…</span></button><figcaption></figcaption>`;
      }
      nodes.push(el);
    }
  }
  placeNodes(grid, nodes);
  updateHeads(grid);
  withCam.forEach((p) => {
    const el = grid.querySelector(`:scope > [data-id="${CSS.escape(p.id)}"]`);
    if (folded(p)) { el.hidden = true; stopTile(p.id); return; }       // folded section: no frames pulled
    el.hidden = false;
    const st = p.status || {}, job = st.job || {};
    const live = ACTIVE.has(st.state);
    const extra = [live && job.progress_pct != null ? `${Math.round(job.progress_pct)}%` : "",
      live && job.remaining_s ? `${fmtDur(job.remaining_s)} left` : ""].filter(Boolean).join(" · ");
    morph(el.querySelector("figcaption"), `<b>${esc(p.name)}</b><span class="pill" title="${esc(pillTitle(st, p))}">${esc(pillText(st, false, p))}</span><span class="muted">${esc(extra)}</span>`);
    if (wall.on && !wall.loops.has(p.id)) startTile(p.id);
  });
  box.querySelector(".cam-none").innerHTML = without.length ? `No camera yet: ${without.map((p) => esc(p.name)).join(", ")}. Add an external camera in a printer's Details.` : "";
  box.querySelector(".cam-empty").hidden = withCam.length > 0;
}
function startTile(id) {
  const loop = { seq: 0, stop: false, url: null };
  wall.loops.set(id, loop);
  (async () => {
    while (!loop.stop) {
      const el = $(`#wall [data-id="${CSS.escape(id)}"]`);
      if (!el) break;
      try {
        const r = await fetch(`/api/printers/${id}/camera.jpg?after=${loop.seq}&wait=1.5`, { cache: "no-store" });
        if (loop.stop) break;
        if (r.status === 200) {
          loop.seq = Number(r.headers.get("X-Frame-Seq")) || loop.seq + 1;
          const url = URL.createObjectURL(await r.blob());
          const img = el.querySelector("img");
          img.onload = () => { if (loop.url && loop.url !== url) URL.revokeObjectURL(loop.url); loop.url = url; };
          img.src = url; img.hidden = false; el.querySelector(".cam-msg").hidden = true;
        } else if (r.status !== 204) {
          const why = await r.json().then((j) => j.detail).catch(() => "");
          el.querySelector(".cam-msg").textContent = why || "Camera not available";
          el.querySelector(".cam-msg").hidden = false;
          await new Promise((res) => setTimeout(res, 5000));
        }
      } catch (_) { await new Promise((res) => setTimeout(res, 3000)); }
    }
  })();
}
function stopTile(id) { const l = wall.loops.get(id); if (l) { l.stop = true; if (l.url) URL.revokeObjectURL(l.url); } wall.loops.delete(id); }
function stopWall() { for (const id of [...wall.loops.keys()]) stopTile(id); }
function setWall(on) {
  wall.on = on;
  // On the wall, Full/Compact turns into one "Dashboard" button (same box, same width, so Cameras doesn't move).
  const seg = $(".top-actions .seg:not(.mini)");
  if (on) seg.style.width = `${seg.getBoundingClientRect().width}px`; else seg.style.width = "";
  seg.querySelectorAll("[data-view]").forEach((b) => { b.hidden = on; });
  $("#dashBtn").hidden = !on;
  document.body.dataset.wall = on ? "1" : "";
  $("#wall").hidden = !on;
  $("#grid").hidden = on;
  $("#wallBtn").setAttribute("aria-pressed", String(on));
  if (on) { renderWall(); } else stopWall();
}
document.addEventListener("visibilitychange", () => {          // background tab: stop pulling camera frames
  if (!wall.on) return;
  if (document.hidden) stopWall(); else renderWall();
});

// ---------------------------------------------------------------- details sheet
function kv(rows) {
  return `<dl class="kv">${rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${v ?? ""}</dd>`).join("")}</dl>`;
}

function renderDetails() {
  const p = state.printers.find((x) => x.id === state.detailsId);
  const dlg = $("#detailsDlg");
  if (!p) { if (dlg.open) dlg.close(); return; }
  const st = p.status || {};
  const job = st.job || {};
  $("#detailsTitle").textContent = p.name;
  $("#detailsSub").textContent = `${p.maker} ${p.model}`;
  const active = ACTIVE.has(st.state);
  const account = state.account;
  const bambuThumbNote = p.family === "bambu" && !(account && account.linked && account.state === "linked")
    ? `<p class="note">Without a linked Bambu account the preview is read from the printer itself. Best effort, no promises.</p>` : "";

  const units = (st.filament || []).map((u) => `
    <div class="unit">
      <div class="unit-head">${esc(u.kind)}<span>${[u.humidity_pct != null ? `Humidity ${u.humidity_pct}%` : "", u.temperature != null ? `${Math.round(u.temperature)}°` : ""].filter(Boolean).join(", ")}</span></div>
      <ul class="slot-list">${u.slots.map((s) => `
        <li>${spoolEl(s)}
          <span>${esc(slotName(s))}${s.active ? " (in use)" : ""}<br><span class="sub">${esc([s.brand, s.color_hex].filter(Boolean).join(", ") || (s.present ? "" : "Nothing loaded"))}</span></span>
          <span class="sub">${s.remaining_pct != null ? `${s.remaining_pct}% left` : ""}</span>
        </li>`).join("")}</ul>
    </div>`).join("");

  const peers = state.groupLoc ? state.printers.filter((x) => locOf(x) === locOf(p)) : state.printers;   // Move earlier/later stays inside the location
  const idx = peers.indexOf(p);
  const secretFields = (p.fields || []).filter((f) => f.kind === "secret");

  const armed = state.removeArmed === p.id;
  const html = `
    <section class="sec">
      <h3>Job</h3>
      <div style="display:flex; gap:14px; align-items:flex-start; flex-wrap:wrap">
        ${thumbEl(p, true)}
      </div>
      ${bambuThumbNote}
      ${kv([
        ["Status", esc(pillText(st, true, p))],
        ["Job", esc(st.state !== "idle" ? job.name : "")],
        ["File", esc(job.file_name)],
        ["Progress", job.progress_pct != null ? `${Math.round(job.progress_pct)}%` : ""],
        ["Time left", active ? esc(fmtDur(job.remaining_s)) : ""],
        ["Done at", active ? esc(fmtEta(job.eta)) : ""],
        ["Elapsed", esc(fmtDur(job.elapsed_s))],
        ["Layer", job.total_layers ? `${job.layer ?? 0} of ${job.total_layers}` : ""],
        ["Plate", job.plate ? `${job.plate}${job.plate_count ? ` of ${job.plate_count}` : ""}` : ""],
        ["Preview from", esc(job.thumbnail_source === "cloud" ? "Bambu cloud" : job.thumbnail_source === "printer" ? "The printer" : "")],
      ])}
    </section>
    <section class="sec" id="usageSec">${usageHTML(p)}</section>
    <section class="sec">
      <h3>Temperatures</h3>
      ${kv(tempsFor(st).concat((st.temps || []).slice(3)).map((t) => [t.label, fmtTemp(t)]))}
    </section>
    <section class="sec">
      <h3>Fans</h3>
      ${kv((st.fans || []).map((f) => [f.label, f.percent != null ? `${f.percent}%` : ""]))}
    </section>
    <section class="sec">
      <h3>Filament</h3>
      ${units || `<p class="muted">${p.family === "sdcp" ? "Canvas filament is read while the printer is idle, then kept until the next idle moment." : "No filament data."}</p>`}
    </section>
    <section class="sec">
      <h3>Errors</h3>
      ${(st.errors || []).length ? `<ul class="err-list">${st.errors.map((e) => `<li><b>${esc(e.message || "No description for this code")}</b><br><span class="sub">${esc(e.code)}</span></li>`).join("")}</ul>` : `<p class="muted">None reported.</p>`}
    </section>
    <section class="sec">
      <h3>Connection</h3>
      ${kv([
        ["State", esc(st.connection ? (CONN_LABEL[st.connection] || "Online") : "")],
        ["Detail", esc(st.connection_detail)],
        ["Address", `${esc(p.host)}${p.settings?.address_changed ? ` <small class="sub">(changed from ${esc(p.settings.address_changed.from)} on ${esc(fmtWhen(p.settings.address_changed.at))}: the printer announced its new address)</small>` : ""}`],
        ["Serial", esc(p.serial)],
        ["Firmware", `${esc(st.firmware)}${st.update_available ? ` <span class="upd-note">Update available: ${esc(st.update_available)}</span>` : ""}`],
        ["Last update", esc(st.updated_at ? new Date(st.updated_at).toLocaleString() : "")],
        ["Up", st.booted_at && st.connection === "online" ? `${esc(fmtUp(st.booted_at))} <span class="sub">(since ${esc(fmtWhen(st.booted_at))})</span>` : ""],
      ])}
    </section>
    <section class="sec">
      <h3>Edit</h3>
      <form id="editForm" class="modal-body" style="padding:0" novalidate>
        <label class="field"><span>Name on the dashboard</span><input name="name" value="${esc(p.name)}" maxlength="60" required ${p.follow_name ? "readonly" : ""}></label>
        <label class="follow-check"><input type="checkbox" id="followName" ${p.follow_name ? "checked" : ""}> Use the printer's name
          <small class="sub">${p.reported_name ? `(it reports “${esc(p.reported_name)}”)` : "(it hasn't reported one yet)"}. Follows renames made on the printer.</small></label>
        <label class="field"><span>IP address</span><input name="host" value="${esc(p.host)}" required></label>
        ${secretFields.map((f) => `<label class="field"><span>${esc(f.label)}</span><input name="secret:${esc(f.key)}" type="password" autocomplete="off" placeholder="${p.secrets_set?.[f.key] ? "Saved. Type a new one to replace it" : "Not set"}"></label>`).join("")}
        <p class="form-error" id="editError" role="alert"></p>
        <div class="row-actions"><button class="btn" type="submit">Save changes</button></div>
      </form>
    </section>
    ${powerHTML(p)}
    <section class="sec">
      <h3>Camera</h3>
      <p class="note">${p.camera?.kind === "external" ? "Using the external camera below." : p.camera?.kind === "builtin" ? "Using the printer's built-in camera." : "No built-in camera the dashboard can show yet. Add an external camera below."}</p>
      ${p.camera?.kind ? `<div class="row-actions" style="margin:8px 0"><button class="btn" type="button" data-camera-open>Open camera</button></div>` : ""}
      <form id="camForm" class="modal-body" style="padding:0" novalidate>
        <label class="field"><span>External camera URL (optional)</span>
          <input name="camera_url" value="${esc(p.camera?.url || "")}" placeholder="http://10.0.0.50:8080/stream.mjpg" inputmode="url" autocomplete="off">
          <small>An MJPEG stream or a JPEG snapshot address, e.g. from a webcam on a Raspberry Pi. Replaces the built-in camera. Leave blank to use the built-in one.</small></label>
        <label class="field"><span>URL type</span>
          <select name="camera_kind">${[["auto", "Detect automatically"], ["mjpeg", "MJPEG stream"], ["snapshot", "Snapshot (refreshed)"]].map(([v, l]) => `<option value="${v}" ${(p.camera?.url_kind || "auto") === v ? "selected" : ""}>${l}</option>`).join("")}</select></label>
        <p class="form-error" id="camError" role="alert"></p>
        <div class="row-actions"><button class="btn" type="submit">Save camera</button></div>
      </form>
    </section>
    ${p.family === "bambu" ? `<section class="sec">
      <h3>Connection</h3>
      <label class="field"><span>How the dashboard reaches this printer</span>
        <select id="connSelect">${[["auto", "Automatic: this network, Bambu cloud when away"], ["local", "This network only"], ["cloud", "Bambu cloud only"]]
          .map(([v, l]) => `<option value="${v}" ${(p.connection || "auto") === v ? "selected" : ""}>${l}</option>`).join("")}</select></label>
      <p class="note">${st.connection === "online" ? (st.link === "cloud" ? "Now: through the Bambu cloud (no camera or file previews from the printer this way)." : "Now: on this network.") : ""}
        Cloud needs the printer on the linked Bambu account (Settings → Bambu account).</p>
    </section>` : ""}
    <section class="sec">
      <h3>Location</h3>
      <label class="field"><span>Where this printer is</span>
        <select id="locSelect">${locItems().map((l) => `<option value="${esc(l.id)}" ${l.id === locOf(p) ? "selected" : ""}>${esc(l.name)}</option>`).join("")}</select></label>
      <p class="note">Changes right away. Add or rename locations under Settings → Dashboard &amp; locations.</p>
    </section>
    <section class="sec">
      <h3>Tags</h3>
      <div class="tag-edit">
        ${(p.tags || []).map((t) => `<span class="tag-pill">${esc(t)}<button type="button" data-untag="${esc(t)}" aria-label="Remove tag ${esc(t)}">✕</button></span>`).join("")}
        <input id="tagInput" list="tagList" maxlength="30" placeholder="${(p.tags || []).length ? "Add another tag" : "Add a tag, e.g. hardened steel"}" aria-label="Add a tag">
        <datalist id="tagList">${allTags().filter((t) => !(p.tags || []).some((x) => x.toLowerCase() === t.toLowerCase())).map((t) => `<option value="${esc(t)}">`).join("")}</datalist>
      </div>
      <p class="note">Press Enter to add. Tags show up under Groups at the top.</p>
    </section>
    <section class="sec">
      <h3>Card position</h3>
      ${state.sort !== "manual" ? `<p class="note">Cards are sorted by ${esc(SORT_LABEL[state.sort])}. Switch Sort to My order to move cards.</p>` : ""}
      <div class="row-actions">
        <button class="btn" type="button" data-move="-1" ${idx <= 0 || state.sort !== "manual" ? "disabled" : ""}>Move earlier</button>
        <button class="btn" type="button" data-move="1" ${idx >= peers.length - 1 || state.sort !== "manual" ? "disabled" : ""}>Move later</button>
      </div>
    </section>
    <section class="sec">
      <h3>Remove</h3>
      <p class="note" style="margin-bottom:8px">Stops watching this printer and removes its card. Nothing on the printer changes.</p>
      <button class="btn danger${armed ? " armed" : ""}" type="button" id="removeBtn">${armed ? `Click again to remove ${esc(p.name)}` : `Remove ${esc(p.name)}`}</button>
    </section>`;
  const body = $("#detailsBody");
  if (body.dataset.for === p.id && body.childNodes.length) morph(body, html);
  else { body.innerHTML = html; body.dataset.for = p.id; body.scrollTop = 0; }
}

function setupDetailsActions() {
  const body = $("#detailsBody");
  const current = () => state.printers.find((x) => x.id === state.detailsId);
  body.addEventListener("submit", async (e) => {
    if (e.target.id !== "editForm") return;
    e.preventDefault();
    const p = current();
    if (!p) return;
    const fd = new FormData(e.target);
    const req = { host: fd.get("host").trim(), secrets: {} };
    if (!p.follow_name) req.name = fd.get("name").trim();      // following: the printer decides the name
    for (const [k, v] of fd.entries()) if (k.startsWith("secret:") && v) req.secrets[k.slice(7)] = v;
    try {
      await api("PATCH", `/api/printers/${p.id}`, req);
      $("#editError").textContent = "";
      e.target.querySelectorAll("input[type=password]").forEach((i) => { i.value = ""; });
    } catch (err) { $("#editError").textContent = err.message; }
  });
  body.addEventListener("submit", async (e) => {
    if (e.target.id !== "camForm") return;
    e.preventDefault();
    const p = current();
    if (!p) return;
    const fd = new FormData(e.target);
    try {
      await api("PATCH", `/api/printers/${p.id}`, { camera_url: fd.get("camera_url").trim(), camera_kind: fd.get("camera_kind") });
      $("#camError").textContent = "";
    } catch (err) { $("#camError").textContent = err.message; }
  });
  body.addEventListener("click", (e) => { if (e.target.closest("[data-camera-open]")) openCamera(state.detailsId); });
  body.addEventListener("change", async (e) => {
    if (e.target.id !== "followName") return;
    const p = current();
    if (!p) return;
    try { await api("PATCH", `/api/printers/${p.id}`, { follow_name: e.target.checked }); $("#editError").textContent = ""; }
    catch (err) { e.target.checked = !e.target.checked; $("#editError").textContent = err.message; }
  });
  body.addEventListener("change", async (e) => {
    if (e.target.id !== "connSelect") return;
    const p = current();
    if (!p) return;
    try { await api("PATCH", `/api/printers/${p.id}`, { connection: e.target.value }); }
    catch (err) { $("#editError").textContent = err.message; }
  });
  body.addEventListener("change", async (e) => {
    if (e.target.id !== "locSelect") return;
    const p = current();
    if (!p) return;
    try { await api("PATCH", `/api/printers/${p.id}`, { location: e.target.value }); }
    catch (err) { $("#editError").textContent = err.message; }
  });
  const saveTags = async (p, tags) => {
    try { await api("PATCH", `/api/printers/${p.id}`, { tags }); }
    catch (err) { $("#editError").textContent = err.message; }
  };
  body.addEventListener("keydown", async (e) => {
    if (e.target.id !== "tagInput" || (e.key !== "Enter" && e.key !== ",")) return;
    e.preventDefault();
    const p = current();
    const v = e.target.value.replace(/,/g, " ").trim();
    if (!p || !v) return;
    e.target.value = "";
    const known = allTags().find((t) => t.toLowerCase() === v.toLowerCase());   // reuse existing spelling
    await saveTags(p, [...(p.tags || []), known || v]);
    $("#tagInput")?.focus();
  });
  body.addEventListener("click", async (e) => {
    const p = current();
    if (!p) return;
    const un = e.target.closest("[data-untag]");
    if (un) { await saveTags(p, (p.tags || []).filter((t) => t !== un.dataset.untag)); return; }
    const mv = e.target.closest("[data-move]");
    if (mv) {
      const ids = state.printers.map((x) => x.id);
      const peers = (state.groupLoc ? state.printers.filter((x) => locOf(x) === locOf(p)) : state.printers).map((x) => x.id);
      const k = peers.indexOf(p.id), other = peers[k + Number(mv.dataset.move)];
      if (!other) return;
      const i = ids.indexOf(p.id), j = ids.indexOf(other);
      [ids[i], ids[j]] = [ids[j], ids[i]];
      saveOrder(ids);
      return;
    }
    const rm = e.target.closest("#removeBtn");
    if (!rm) return;
    if (state.removeArmed !== p.id) {
      state.removeArmed = p.id;
      renderDetails();
      setTimeout(() => { if (state.removeArmed === p.id) { state.removeArmed = null; renderDetails(); } }, 4000);
      return;
    }
    rm.disabled = true;
    try { await api("DELETE", `/api/printers/${p.id}`); state.removeArmed = null; state.detailsId = null; $("#detailsDlg").close(); }
    catch (err) { rm.disabled = false; rm.textContent = err.message; }
  });
}

function openDetails(id) {
  state.detailsId = id;
  state.usageEdit = false;
  state.powerConfirm = null; state.powerArm = null;
  renderDetails();
  loadUsage(id);
  if (state.govee?.linked) loadPowerLog(id);
  const dlg = $("#detailsDlg");
  if (!dlg.open) dlg.showModal();
}

// ---------------------------------------------------------------- reorder (drag + buttons)
async function saveOrder(ids) {
  const byId = Object.fromEntries(state.printers.map((p) => [p.id, p]));
  state.printers = ids.map((i) => byId[i]);
  renderAll();
  try { await api("PUT", "/api/printers/order", { ids }); } catch (err) { console.warn(err); }
}

function setupDrag() {
  const grid = $("#grid");
  let dragId = null;
  grid.addEventListener("pointerdown", (e) => {
    const grip = state.sort === "manual" && e.target.closest("[data-grip]");
    const card = e.target.closest(".card");
    if (card) card.draggable = !!grip;
  });
  grid.addEventListener("dragstart", (e) => {
    const card = e.target.closest(".card");
    if (!card) return;
    dragId = card.dataset.id;
    card.classList.add("dragging");
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData("text/plain", dragId);
  });
  const clear = () => grid.querySelectorAll(".drop-before,.drop-after").forEach((c) => c.classList.remove("drop-before", "drop-after"));
  const sameSection = (card) => !state.groupLoc || card.dataset.loc === grid.querySelector(`:scope > [data-id="${CSS.escape(dragId)}"]`)?.dataset.loc;
  grid.addEventListener("dragover", (e) => {
    const card = e.target.closest(".card");
    if (!dragId || !card || card.dataset.id === dragId || !sameSection(card)) return;   // moving between locations: Details only
    e.preventDefault();
    const r = card.getBoundingClientRect();
    const after = state.view === "list" ? e.clientY > r.top + r.height / 2 : e.clientX > r.left + r.width / 2;
    clear();
    card.classList.add(after ? "drop-after" : "drop-before");
  });
  grid.addEventListener("dragleave", (e) => { if (!grid.contains(e.relatedTarget)) clear(); });
  grid.addEventListener("drop", (e) => {
    const card = e.target.closest(".card");
    if (!dragId || !card || !sameSection(card)) return;
    e.preventDefault();
    const after = card.classList.contains("drop-after");
    clear();
    const ids = state.printers.map((p) => p.id).filter((i) => i !== dragId);
    let at = ids.indexOf(card.dataset.id);
    if (at < 0) return;
    if (after) at += 1;
    ids.splice(at, 0, dragId);
    saveOrder(ids);
  });
  grid.addEventListener("dragend", () => {
    grid.querySelectorAll(".dragging").forEach((c) => { c.classList.remove("dragging"); c.draggable = false; });
    clear();
    dragId = null;
  });
}

// ---------------------------------------------------------------- add printer
const HOW = {
  "Bambu Lab": [
    ["account", "From my Bambu account", "Sign in and pick from your printers. Names and access codes are filled in."],
    ["manual", "Enter it myself", "Type the printer's IP address and LAN access code."]],
  "Snapmaker": [
    ["network", "Find on my network", "Lists the U1s on your network with their names. No sign-in needed."],
    ["manual", "Enter it myself", "Type the printer's IP address."]],
  "Elegoo": [
    ["network", "Find on my network", "Lists the Elegoo printers on your network. No sign-in needed."],
    ["manual", "Enter it myself", "Type the printer's IP address."]],
  "Klipper (generic)": [
    ["network", "Find on my network", "Lists Klipper printers (Moonraker) on your network."],
    ["manual", "Enter it myself", "Type the printer's IP address."]],
};

function renderHow() {
  const opts = HOW[$("#addMaker").value];
  const box = $("#addHow");
  box.hidden = !opts;
  if (!opts) { box.innerHTML = ""; return; }
  box.innerHTML = `<legend>How do you want to add it?</legend>` + opts.map(([v, title, help], i) =>
    `<label><input type="radio" name="how" value="${v}" ${i === 0 ? "checked" : ""}><span><b>${esc(title)}</b><small>${esc(help)}</small></span></label>`).join("");
}

function addMode() {
  return $("#addHow").querySelector("input:checked")?.value || "manual";
}

function applyAddMode() {
  const mode = addMode();
  $("#addManual").hidden = mode !== "manual";
  $("#addAccount").hidden = mode !== "account";
  $("#addNetwork").hidden = mode !== "network";
  $("#addError").textContent = "";
  if (mode === "account") renderAccountPicker();
  else if (mode === "network") renderNetworkPicker();
  else { $("#addSubmit").textContent = "Add printer"; $("#addSubmit").disabled = !currentModel()?.available; }
}

// Network search (U1 / Klipper): starts only when the user clicks, shows real progress from the
// server (addresses checked), lists printers as they are found, and can be stopped.
const net = { phase: "idle", snap: null, timer: null, unchecked: new Set() };

const NET = {
  "Snapmaker": { what: "U1s", match: (p) => p.driver === "snapmaker.u1" },
  "Elegoo": { what: "Elegoo printers", match: (p) => (p.driver || "").startsWith("elegoo.") },
  "Klipper (generic)": { what: "Klipper printers", match: (p) => p.driver === "klipper.generic" },
};
function netWhat() { return (NET[$("#addMaker").value] || NET["Klipper (generic)"]).what; }
function netMatch(p) { return (NET[$("#addMaker").value] || NET["Klipper (generic)"]).match(p); }

function renderNetworkPicker() {
  const box = $("#addNetwork");
  const sub = $("#addSubmit");
  const snap = net.snap;
  const list = (snap?.found || []).filter(netMatch);
  const what = netWhat();
  let head;
  if (net.phase === "idle") {
    head = `<p class="note">Looks for ${esc(what)} on your network: it checks each address once and reads the printer's name. Takes about 10 seconds.</p>
      <div class="row-actions"><button type="button" class="btn" data-net="start">Search my network</button></div>`;
  } else if (net.phase === "running") {
    const pct = snap && snap.total ? Math.round(100 * snap.checked / snap.total) : 0;
    head = `<div class="scan">
        <div class="scan-bar" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${pct}"><span style="width:${pct}%"></span></div>
        <p class="note">Searching… checked ${snap ? snap.checked : 0} of ${snap ? snap.total : 254} addresses${list.length ? `, found ${list.length}` : ""}. <button type="button" class="link-btn" data-net="stop">Stop</button></p>
      </div>`;
  } else {
    const how = snap?.cancelled ? "Search stopped" : "Search finished";
    head = snap?.error ? `<p class="form-error">Search failed: ${esc(snap.error)}</p>`
      : `<p class="note">${how}: ${list.length ? `found ${list.length} ${esc(what)}` : `no ${esc(what)} found. Check the printer is on and on the same network, or enter it yourself`}.
         <button type="button" class="link-btn" data-net="start">Search again</button></p>`;
  }
  const rows = list.map((p) => {
    const supported = p.driver !== "elegoo.unsupported";
    const can = !p.added_id && supported;
    const name = p.name || `${p.model || "Printer"} at ${p.host}`;
    const checked = can && !net.unchecked.has(p.host);
    return `<li class="${can ? "" : "off"}" data-host="${esc(p.host)}">
      <input type="checkbox" data-pick="${esc(p.host)}" ${checked ? "checked" : ""} ${can ? "" : "disabled"} aria-label="Add ${esc(name)}">
      <span><b>${esc(name)}</b><br><span class="meta">${esc([p.model, p.host, p.firmware ? `firmware ${p.firmware}` : ""].filter(Boolean).join(", "))}</span></span>
      <span class="tag ${p.added_id ? "added" : ""}">${p.added_id ? "Added" : supported ? "Not added" : "Not supported yet"}</span>
    </li>`;
  }).join("");
  const html = head + (rows ? `<ul class="pick-list">${rows}</ul>` : "");
  if (box.childNodes.length) morph(box, html); else box.innerHTML = html;
  const n = box.querySelectorAll("[data-pick]:checked").length;
  sub.disabled = n === 0;
  sub.textContent = n === 0 ? "Add selected printers" : n === 1 ? "Add 1 printer" : `Add ${n} printers`;
}

async function netPoll() {
  clearTimeout(net.timer);
  if (net.phase !== "running") return;
  try { net.snap = await api("GET", "/api/discover/network"); }
  catch (err) { net.phase = "done"; net.snap = { error: err.message, found: [] }; }
  if (net.snap?.done) net.phase = "done";
  if (addMode() === "network") renderNetworkPicker();
  if (net.phase === "running") net.timer = setTimeout(netPoll, 400);
}

async function netStart() {
  net.phase = "running"; net.snap = null; net.unchecked.clear();
  renderNetworkPicker();
  try { await api("POST", "/api/discover/network"); }
  catch (err) { net.phase = "done"; net.snap = { error: err.message, found: [] }; renderNetworkPicker(); return; }
  netPoll();
}

function netReset() {
  clearTimeout(net.timer);
  if (net.phase === "running") api("DELETE", "/api/discover/network").catch(() => {});
  net.phase = "idle"; net.snap = null; net.unchecked.clear();
  $("#addNetwork").innerHTML = "";
}

function setupNetworkPicker() {
  const box = $("#addNetwork");
  box.addEventListener("click", (e) => {
    const b = e.target.closest("[data-net]");
    if (!b) return;
    if (b.dataset.net === "start") netStart();
    if (b.dataset.net === "stop") api("DELETE", "/api/discover/network").catch(() => {});
  });
  box.addEventListener("change", (e) => {
    const c = e.target.closest("[data-pick]");
    if (!c) return;
    if (c.checked) net.unchecked.delete(c.dataset.pick); else net.unchecked.add(c.dataset.pick);
    renderNetworkPicker();
  });
}

async function submitNetworkAdd() {
  const box = $("#addNetwork");
  const byHost = Object.fromEntries((net.snap?.found || []).map((p) => [p.host, p]));
  const picks = [...box.querySelectorAll("[data-pick]:checked")].map((c) => byHost[c.dataset.pick]).filter(Boolean);
  if (!picks.length) return;
  $("#addSubmit").disabled = true;
  const failed = [];
  for (const p of picks) {
    try {
      await api("POST", "/api/printers", { driver: p.driver, name: p.name || null, host: p.host, serial: p.serial || null,
        settings: p.port && p.port !== 80 ? { port: p.port } : {}, secrets: {}, location: $("#addLoc").value || null });
    } catch (err) { failed.push([p, err.message]); }
  }
  if (!failed.length) { $("#addDlg").close(); return; }
  try { const snap = await api("GET", "/api/discover/network"); if (snap.started) net.snap = snap; } catch (_) { /* keep old */ }
  renderNetworkPicker();
  for (const [p, msg] of failed) {
    const li = box.querySelector(`li[data-host="${CSS.escape(p.host)}"]`);
    if (li) li.insertAdjacentHTML("beforeend", `<span class="err">${esc(msg)}</span>`);
  }
  $("#addError").textContent = `${picks.length - failed.length} added, ${failed.length} not added`;
}

async function renderAccountPicker() {
  const box = $("#addAccount");
  const sub = $("#addSubmit");
  sub.textContent = "Add selected printers";
  sub.disabled = true;
  const a = state.account;
  if (!a || !a.linked || a.state !== "linked") { renderAccount(undefined, undefined, box, renderAccountPicker); return; }
  box.innerHTML = `<p class="note">Loading the printers on ${esc(a.email)}…</p>`;
  let list;
  try { list = await api("GET", "/api/bambu-account/printers"); }
  catch (err) {
    box.innerHTML = `<p class="form-error">${esc(err.message)}</p>`;
    await loadAccount();
    if (state.account?.state !== "linked") renderAccount(undefined, err.message, box, renderAccountPicker);
    return;
  }
  state.accountPrinters = list;
  const rows = list.map((p) => {
    const can = p.supported && !p.added_id;
    const tag = p.added_id ? `<span class="tag added">Added</span>` : !p.supported ? `<span class="tag">Not supported yet</span>` : `<span class="tag">Not added</span>`;
    const ip = p.host ? esc(p.host) : can ? `<input data-ip="${esc(p.serial)}" placeholder="IP address" aria-label="IP address for ${esc(p.name)}">` : "";
    return `<li class="${can ? "" : "off"}" data-serial="${esc(p.serial)}">
      <input type="checkbox" data-pick="${esc(p.serial)}" ${can ? "checked" : "disabled"} aria-label="Add ${esc(p.name)}">
      <span><b>${esc(p.name)}</b><br><span class="meta">${[esc(p.model || ""), can && !p.host ? `IP not heard on the network yet ${ip}` : ip, p.online === false ? "offline" : ""].filter(Boolean).join(", ")}</span></span>
      ${tag}
    </li>`;
  }).join("");
  box.innerHTML = list.length ? `
    <p class="note">Printers on ${esc(a.email)}. Names and access codes come from the account; IP addresses from the printers' own network announcements.</p>
    <ul class="pick-list">${rows}</ul>
    <button type="button" class="link-btn" data-refresh>Look again</button>`
    : `<p class="note">No printers are bound to ${esc(a.email)}.</p>`;
  box.querySelector("[data-refresh]")?.addEventListener("click", renderAccountPicker);
  const update = () => {
    const n = box.querySelectorAll("[data-pick]:checked").length;
    sub.disabled = n === 0;
    sub.textContent = n === 1 ? "Add 1 printer" : `Add ${n} printers`;
  };
  box.querySelectorAll("[data-pick]").forEach((c) => c.addEventListener("change", update));
  update();
}

async function submitAccountImport() {
  const box = $("#addAccount");
  const picks = [...box.querySelectorAll("[data-pick]:checked")].map((c) => {
    const ip = box.querySelector(`[data-ip="${CSS.escape(c.dataset.pick)}"]`);
    return { serial: c.dataset.pick, host: ip ? ip.value.trim() || null : null };
  });
  if (!picks.length) return;
  $("#addSubmit").disabled = true;
  try {
    const res = await api("POST", "/api/bambu-account/printers", { printers: picks, location: $("#addLoc").value || null });
    const failed = res.filter((r) => r.error);
    if (!failed.length) { $("#addDlg").close(); return; }
    await renderAccountPicker();
    for (const f of failed) {
      const li = box.querySelector(`li[data-serial="${CSS.escape(f.serial)}"]`);
      if (li) li.insertAdjacentHTML("beforeend", `<span class="err">${esc(f.error)}</span>`);
    }
    $("#addError").textContent = `${res.length - failed.length} added, ${failed.length} not added`;
  } catch (err) {
    $("#addError").textContent = err.message;
    $("#addSubmit").disabled = false;
  }
}

function fillModels() {
  const maker = state.catalog.find((m) => m.maker === $("#addMaker").value);
  $("#addModel").innerHTML = (maker?.models || []).map((m) =>
    `<option value="${esc(m.driver)}" ${m.available ? "" : "disabled"}>${esc(m.model)}${m.available ? "" : " (coming soon)"}</option>`).join("");
  const first = maker?.models.find((m) => m.available);
  if (first) $("#addModel").value = first.driver;
  fillFields();
  renderHow();
  applyAddMode();
}

function currentModel() {
  for (const m of state.catalog) for (const mo of m.models) if (mo.driver === $("#addModel").value) return mo;
  return null;
}

function fillFields() {
  const mo = currentModel();
  $("#addFields").innerHTML = (mo?.fields || []).map((f) => `
    <label class="field">
      <span>${esc(f.label)}${f.required ? "" : " (optional)"}</span>
      <input name="${esc(f.key)}" data-kind="${esc(f.kind)}" ${f.kind === "secret" ? 'type="password" autocomplete="off"' : f.kind === "int" ? 'type="number"' : ""}
             ${f.required ? "required" : ""} value="${f.default != null ? esc(f.default) : ""}">
      ${f.help ? `<small>${esc(f.help)}</small>` : ""}
    </label>`).join("");
  if (addMode() === "manual") $("#addSubmit").disabled = !mo || !mo.available;
}

function fillLocSelect() {
  const sel = $("#addLoc");
  sel.innerHTML = locItems().map((l) => `<option value="${esc(l.id)}">${esc(l.name)}</option>`).join("");
  sel.value = state.layout.locations.default || "";
  $("#addLocField").hidden = !locItems().length;
}
async function openAdd() {
  fillLocSelect();
  if (!state.catalog.length) state.catalog = await api("GET", "/api/catalog");
  const makers = state.catalog.filter((m) => m.models.some((x) => x.available)).concat(state.catalog.filter((m) => !m.models.some((x) => x.available)));
  $("#addMaker").innerHTML = makers.map((m) => `<option ${m.models.some((x) => x.available) ? "" : "disabled"}>${esc(m.maker)}</option>`).join("");
  $("#addMaker").value = makers[0]?.maker;
  fillModels();
  $("#addName").value = "";
  $("#addError").textContent = "";
  $("#addDlg").showModal();
}

async function submitAdd(e) {
  e.preventDefault();
  if (addMode() === "account") { submitAccountImport(); return; }
  if (addMode() === "network") { submitNetworkAdd(); return; }
  const mo = currentModel();
  const body = { driver: mo.driver, name: $("#addName").value.trim() || null, host: "", serial: null, settings: {}, secrets: {},
    location: $("#addLoc").value || null };
  const missing = [];
  for (const inp of $("#addFields").querySelectorAll("input")) {
    const v = inp.value.trim();
    const f = mo.fields.find((x) => x.key === inp.name);
    if (f.required && !v) missing.push(f.label);
    if (!v) continue;
    if (f.key === "host") body.host = v;
    else if (f.key === "serial") body.serial = v;
    else if (f.kind === "secret") body.secrets[f.key] = v;
    else body.settings[f.key] = f.kind === "int" ? Number(v) : v;
  }
  if (missing.length) { $("#addError").textContent = `Fill in: ${missing.join(", ")}`; return; }
  $("#addSubmit").disabled = true;
  try {
    await api("POST", "/api/printers", body);
    $("#addDlg").close();
  } catch (err) {
    $("#addError").textContent = err.message;
  } finally {
    $("#addSubmit").disabled = false;
  }
}

// ---------------------------------------------------------------- Bambu account
async function loadAccount() {
  try { state.account = await api("GET", "/api/bambu-account"); } catch (_) { state.account = null; }
  const a = state.account;
  const btn = $("#accountBtn");
  const color = !a?.linked ? "var(--idle)" : a.state === "linked" ? "var(--done)" : "var(--paused)";
  const note = !a?.linked ? "Not linked" : a.state === "linked" ? "Linked" : "Sign in again";
  btn.innerHTML = `<span class="dot" style="background:${color}"></span>Bambu account<small>${esc(note)}</small>`;
  $("#settingsDot").hidden = !(a?.linked && a.state !== "linked");      // needs attention
  $("#settingsBtn").title = $("#settingsDot").hidden ? "Settings" : "Settings: Bambu account needs you to sign in again";
  if ($("#accountDlg").open && !$("#accountBody").querySelector("[data-codeform]")) renderAccount();
  if (state.detailsId) renderDetails();
}

function renderAccount(step, msg, box = $("#accountBody"), onDone = null) {
  const a = state.account || {};
  const intro = box.id === "accountBody"
    ? `<p class="note">Optional. With your Bambu account linked, each card shows the job's cover image for prints sent through Bambu cloud, and you can add printers from the account. Without it, the preview is read from the printer. Best effort, no promises.</p>`
    : `<p class="note">Sign in to list the printers on your Bambu account. Your password is used once and not saved.</p>`;
  const done = async () => { await loadAccount(); if (onDone) onDone(); else renderAccount(undefined, undefined, box); };
  if (step === "email_code" || step === "tfa") {
    box.innerHTML = `
      <p>${step === "tfa" ? "Enter the 6-digit code from your authenticator app." : "Bambu emailed you a 6-digit code. Enter it here."}</p>
      <div class="modal-body" style="padding:0" data-codeform>
        <label class="field"><span>Code</span><input name="code" inputmode="numeric" autocomplete="one-time-code" maxlength="8"></label>
        <p class="form-error" role="alert">${esc(msg || "")}</p>
        <div class="row-actions"><button class="btn primary" type="button" data-go>Sign in</button></div>
      </div>`;
    const inp = box.querySelector("input[name=code]");
    inp.focus();
    const go = async () => {
      try { await api("POST", "/api/bambu-account/code", { code: inp.value.trim() }); await done(); }
      catch (err) { renderAccount(step, err.message, box, onDone); }
    };
    box.querySelector("[data-go]").addEventListener("click", go);
    inp.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); go(); } });
    return;
  }
  if (box.id === "accountBody" && a.linked && a.state === "linked") {
    box.innerHTML = `${intro}${kv([["Linked account", esc(a.email)]])}
      <div class="modal-foot" style="padding:4px 0 16px"><button class="btn danger" type="button" id="unlinkBtn">Unlink account</button></div>`;
    $("#unlinkBtn").addEventListener("click", async () => { await api("DELETE", "/api/bambu-account"); await loadAccount(); renderAccount(); });
    return;
  }
  box.innerHTML = `${intro}
    ${a.linked && a.state !== "linked" ? `<p class="note warn">Bambu stopped accepting the saved sign-in for ${esc(a.email)}. Sign in again.</p>` : ""}
    <div class="modal-body" style="padding:0">
      <label class="field"><span>Email</span><input name="email" type="email" autocomplete="username" value="${esc(a.email || "")}"></label>
      <label class="field"><span>Password</span><input name="password" type="password" autocomplete="current-password"><small>Used once to sign in. Not saved.</small></label>
      <label class="field"><span>Region</span><select name="region"><option value="global">Global</option><option value="china">China</option></select></label>
      <p class="form-error" role="alert">${esc(msg || "")}</p>
      <div class="row-actions"><button class="btn primary" type="button" data-go>Sign in</button></div>
    </div>`;
  const q = (n) => box.querySelector(`[name=${n}]`);
  const btn = box.querySelector("[data-go]");
  const go = async () => {
    if (!q("email").value.trim() || !q("password").value) { renderAccount(undefined, "Enter your email and password", box, onDone); return; }
    btn.disabled = true;
    try {
      const r = await api("POST", "/api/bambu-account/login", { email: q("email").value.trim(), password: q("password").value, region: q("region").value });
      if (r.step === "done") await done(); else renderAccount(r.step, undefined, box, onDone);
    } catch (err) { renderAccount(undefined, err.message, box, onDone); }
  };
  btn.addEventListener("click", go);
  for (const n of ["email", "password"]) q(n).addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); go(); } });
}

// ---------------------------------------------------------------- live updates
const LIVE_SILENT_MS = 45000;   // the server sends at least a ping every 15 s
let liveES = null, liveLast = 0;
setInterval(() => {               // a stream that went quiet (sleep, Wi-Fi drop, dropped by the server): start over
  if (liveES && Date.now() - liveLast > LIVE_SILENT_MS) { liveES.close(); connect(); }
}, 10000);
function connect() {
  const es = new EventSource("/api/events");
  liveES = es; liveLast = Date.now();
  es.onopen = () => { liveLast = Date.now(); $("#liveBanner").hidden = true; };
  es.onerror = () => { $("#liveBanner").hidden = false; };
  es.onmessage = (m) => {
    liveLast = Date.now();
    const ev = JSON.parse(m.data);
    if (ev.type === "ping") return;
    if (ev.type === "printers") {
      if (ev.layout) { state.layout = ev.layout; applyLayout(); }
      state.printers = ev.printers; renderAll();
      if (!$("#addDlg").open) fillLocSelect();
      loadTags();
    }
    else if (ev.type === "status") {
      const p = state.printers.find((x) => x.id === ev.id);
      if (!p) return;
      p.status = ev.status;
      const el = $(`#grid [data-id="${CSS.escape(p.id)}"]`);
      if (el && !el.classList.contains("dragging")) renderCard(p, el);
      if (state.sort !== "manual") applyOrder();
      renderSummary();
      if (state.overlayId === p.id) overlayCard();
      if (state.detailsId === p.id && !$("#detailsBody").contains(document.activeElement)) renderDetails();
    } else if (ev.type === "account") loadAccount();
    else if (ev.type === "job") {
      if (state.detailsId === ev.printer_id) loadUsage(ev.printer_id);
      if ($("#historyDlg").open) loadHistory(true);
    }
  };
}

// ---------------------------------------------------------------- wiring
document.addEventListener("click", (e) => {
  if (e.target.closest("[data-close]")) e.target.closest("dialog")?.close();
  const d = e.target.closest("[data-details]");
  if (d) openDetails(d.closest(".card").dataset.id);
  const cb = e.target.closest("[data-camera]");
  if (cb) openCamera(cb.closest(".card").dataset.id);
  const pb = e.target.closest("[data-power-open]");
  if (pb) openPower(pb.closest(".card").dataset.id);
  if (e.target.closest("[data-open-add]")) openAdd();
});
for (const dlg of document.querySelectorAll("dialog")) {
  dlg.addEventListener("click", (e) => { if (e.target === dlg) dlg.close(); });   // click on backdrop
}
$("#detailsDlg").addEventListener("close", () => { state.detailsId = null; });
// Settings menu (gear): Add printer, Bambu account
function setMenu(open, focusFirst = false) {
  $("#settingsMenu").hidden = !open;
  $("#settingsBtn").setAttribute("aria-expanded", String(open));
  if (open && focusFirst) $("#settingsMenu [role=menuitem]").focus();
}
$("#settingsBtn").addEventListener("click", (e) => setMenu($("#settingsMenu").hidden, e.detail === 0));
$("#settingsMenu").addEventListener("click", (e) => { if (e.target.closest("[role=menuitem]")) setMenu(false); });
$("#settingsMenu").addEventListener("keydown", (e) => {
  const items = [...$("#settingsMenu").querySelectorAll("[role=menuitem], [role=menuitemradio]")];
  const i = items.indexOf(document.activeElement);
  if (e.key === "ArrowDown") { e.preventDefault(); items[(i + 1) % items.length].focus(); }
  if (e.key === "ArrowUp") { e.preventDefault(); items[(i - 1 + items.length) % items.length].focus(); }
  if (e.key === "Escape") { setMenu(false); $("#settingsBtn").focus(); }
  if (e.key === "Tab") setMenu(false);
});
document.addEventListener("click", (e) => { if (!e.target.closest(".menu-wrap")) setMenu(false); });
$("#addBtn").addEventListener("click", openAdd);
$("#addMaker").addEventListener("change", fillModels);
$("#addModel").addEventListener("change", fillFields);
$("#addHow").addEventListener("change", applyAddMode);
$("#addForm").addEventListener("submit", submitAdd);
$("#accountBtn").addEventListener("click", () => { renderAccount(); $("#accountDlg").showModal(); });
$("#addDlg").addEventListener("close", () => { $("#addAccount").innerHTML = ""; netReset(); });
setInterval(() => { if (state.printers.some((p) => p.autocycle?.enabled || seenText(p) || p.status?.booted_at)) renderAll(); }, 30000);   // countdowns, "today" -> weekday

function overlayCard() {
  const p = state.printers.find((x) => x.id === state.overlayId);
  const dlg = $("#cardDlg");
  if (!p) { if (dlg.open) dlg.close(); return; }
  const el = $("#overlayCard");
  el.className = "card in-overlay";
  el.dataset.id = p.id;
  el.dataset.state = p.status?.state || "unknown";
  el.dataset.conn = p.status?.connection || "connecting";
  el.setAttribute("aria-label", p.name);
  const html = cardHTML(p, true);
  if (el.childNodes.length && el.dataset.for === p.id) morph(el, html); else { el.innerHTML = html; el.dataset.for = p.id; }
}

function applyView() {
  document.body.dataset.view = state.view;
  document.querySelectorAll(".seg [data-view]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.view === state.view)));
}
document.querySelector(".seg").addEventListener("click", (e) => {
  const b = e.target.closest("[data-view]");
  if (!b || b.dataset.view === state.view) return;
  state.view = b.dataset.view;
  saveView(); applyView(); renderAll();
});
$("#grid").addEventListener("click", async (e) => {
  const cb = e.target.closest("[data-cycle]");
  if (!cb) return;
  try { await api("POST", `/api/printers/${cb.closest(".card").dataset.id}/autocycle/${cb.dataset.cycle}`); }
  catch (err) { console.warn(err); }
});
$("#grid").addEventListener("click", (e) => {
  const b = e.target.closest("[data-size=expand]");
  if (!b) return;
  state.overlayId = b.closest(".card").dataset.id;
  $("#overlayCard").innerHTML = "";
  overlayCard();
  $("#cardDlg").showModal();
});
$("#cardDlg").addEventListener("click", (e) => {
  if (e.target.closest("[data-overlay-close]")) $("#cardDlg").close();   // Details: global handler opens it on top
});
$("#cardDlg").addEventListener("close", () => { state.overlayId = null; });
$("#camDlg").addEventListener("close", closeCameraStream);
$("#powerDlg").addEventListener("close", () => { state.powerDlgId = null; state.powerConfirm = null; state.powerArm = null; });
$("#camSmooth").addEventListener("click", () => setSmooth($("#camSmooth").getAttribute("aria-pressed") !== "true"));
$("#wallBtn").addEventListener("click", () => setWall(!wall.on));
$("#dashBtn").addEventListener("click", () => setWall(false));
$("#wall").addEventListener("click", (e) => { const t = e.target.closest("[data-cam-open]"); if (t) openCamera(t.closest(".cam-tile").dataset.id); });
// Appearance: Auto (follows the system), Light or Dark. Remembered in this browser; applied early in <head>.
function applyTheme(t) {
  if (t === "light" || t === "dark") document.documentElement.dataset.theme = t;
  else delete document.documentElement.dataset.theme;
  document.querySelectorAll("[data-theme-pick]").forEach((b) => {
    const on = b.dataset.themePick === (t || "auto");
    b.setAttribute("aria-checked", String(on)); b.setAttribute("aria-pressed", String(on));
  });
}
let savedTheme = "auto";
try { savedTheme = localStorage.getItem("theme") || "auto"; } catch (_) { /* private mode */ }
applyTheme(savedTheme);
$("#themeSeg").addEventListener("click", (e) => {
  const b = e.target.closest("[data-theme-pick]");
  if (!b) return;
  try { localStorage.setItem("theme", b.dataset.themePick); } catch (_) { /* ignore */ }
  applyTheme(b.dataset.themePick);
});
loadView();
applyView();
loadFilter();
setupFilter();
setupGroups();
setupLocs();
setupLayoutDlg();
setupTags();
loadTags();
setupUsage();
setupHistory();
setupPower();
setupPlugsDlg();
setupDrag();
setupDetailsActions();
setupNetworkPicker();
loadAccount();
loadGovee();
connect();
