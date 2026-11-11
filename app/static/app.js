"use strict";
const $ = (s, el = document) => el.querySelector(s);
const view = $("#view");
let statusCache = null;
let currentRoute = "";

// ---------------------------------------------------------------- helpers
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
async function api(path, opts = {}) {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts,
    body: opts.method === "POST" ? JSON.stringify(opts.body || {}) : undefined });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || data.message || `HTTP ${r.status}`);
  return data;
}
function toast(msg) {
  const d = document.createElement("div"); d.textContent = msg; $("#toast").appendChild(d);
  setTimeout(() => d.remove(), 4500);
}
const fmtBytes = (n) => { if (!n) return "—"; const u = ["B", "KB", "MB", "GB", "TB", "PB"]; let i = 0; while (n >= 1000 && i < u.length - 1) { n /= 1000; i++; } return `${n.toFixed(i > 2 ? 1 : 0)} ${u[i]}`; };
const fmtNum = (n) => n == null ? "—" : Number(n).toLocaleString();
function fmtHours(h) {
  if (h == null) return "—";
  const y = Math.floor(h / 8766), d = Math.floor((h % 8766) / 24);
  return `${y ? y + "y " : ""}${d}d`;
}
function fmtAgo(ts) {
  if (!ts) return "never";
  const s = Math.round(Date.now() / 1000 - ts);
  if (s < 60) return "just now"; if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`; return `${Math.round(s / 86400)} d ago`;
}
const fmtDate = (ts) => ts ? new Date(ts * 1000).toLocaleString() : "—";
function fmtDays(d) {
  if (d == null) return "—"; if (d < 1) return "< 1 day"; if (d < 60) return `~${Math.round(d)} days`;
  if (d < 730) return `~${Math.round(d / 30.4)} months`; return `~${(d / 365).toFixed(1)} years`;
}
const stClass = (c) => `st-${c.state === "missing" ? "missing" : c.state === "standby" && !c.status ? "standby" : (c.status || "unknown")}`;
function statusLabel(c) {
  if (c.state === "missing") return "Missing";
  return { good: "Healthy", warning: "Warning", critical: "Critical", failing: "Failing" }[c.status] || "Unknown";
}
function driveType(s) {
  if (s.protocol === "NVMe") return "NVMe SSD";
  if (s.is_ssd) return "SATA SSD";
  if (s.protocol === "SCSI") return "SAS/SCSI";
  return s.rotation_rate ? `HDD · ${s.rotation_rate} rpm` : "HDD";
}

function ring(value, cls, size = 64) {
  const r = size / 2 - 6, c = 2 * Math.PI * r, v = value == null ? 0 : Math.max(0, Math.min(100, value));
  return `<svg class="ring ${cls}" width="${size}" height="${size}" viewBox="0 0 ${size} ${size}" role="img" aria-label="Health ${v}%">
    <circle class="track" cx="${size / 2}" cy="${size / 2}" r="${r}" fill="none" stroke-width="6"/>
    <circle class="bar" cx="${size / 2}" cy="${size / 2}" r="${r}" fill="none" stroke-width="6" stroke-linecap="round"
      stroke-dasharray="${c}" stroke-dashoffset="${c * (1 - v / 100)}" transform="rotate(-90 ${size / 2} ${size / 2})"/>
    <text x="50%" y="50%" text-anchor="middle" dominant-baseline="central" font-size="${size / 4.2}">${value == null ? "?" : v + "%"}</text>
  </svg>`;
}

// ---------------------------------------------------------------- chart
function niceTicks(lo, hi, n = 4) {
  const span = hi - lo || 1, raw = span / n, mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => span / s <= n) || mag * 10;
  const out = []; for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) out.push(+v.toFixed(6));
  return out;
}
function downsample(ts, series, max = 600) {
  if (ts.length <= max) return { ts, series };
  const k = Math.ceil(ts.length / max), nts = [];
  const ns = series.map((s) => ({ ...s, data: [] }));
  for (let i = 0; i < ts.length; i += k) {
    nts.push(ts[Math.min(i + k - 1, ts.length - 1)]);
    series.forEach((s, j) => {
      const chunk = s.data.slice(i, i + k).filter((v) => v != null);
      ns[j].data.push(chunk.length ? (s.agg === "max" ? Math.max(...chunk) : chunk.reduce((a, b) => a + b, 0) / chunk.length) : null);
    });
  }
  return { ts: nts, series: ns };
}
function lineChart(el, tsIn, seriesIn, opt = {}) {
  const { ts, series } = downsample(tsIn, seriesIn.filter((s) => s.data.some((v) => v != null)));
  if (!ts.length || !series.length) { el.innerHTML = `<div class="empty small">No data yet</div>`; return; }
  const W = el.clientWidth || 600, H = el.clientHeight || 190, P = { l: 42, r: 10, t: 8, b: 22 };
  const vals = series.flatMap((s) => s.data.filter((v) => v != null));
  let lo = opt.min ?? Math.min(...vals), hi = opt.max ?? Math.max(...vals);
  if (opt.min == null && opt.max == null) { const pad = (hi - lo) * 0.1 || 1; lo -= pad; hi += pad; }
  if (opt.floor != null) lo = Math.max(opt.floor, lo);
  if (hi === lo) hi = lo + 1;
  const t0 = ts[0], t1 = ts[ts.length - 1] === t0 ? t0 + 1 : ts[ts.length - 1];
  const x = (t) => P.l + ((t - t0) / (t1 - t0)) * (W - P.l - P.r);
  const y = (v) => P.t + (1 - (v - lo) / (hi - lo)) * (H - P.t - P.b);
  const ticks = niceTicks(lo, hi);
  const span = t1 - t0, xt = [0, 1 / 3, 2 / 3, 1].map((f) => t0 + f * span);
  const xl = (t) => { const d = new Date(t * 1000); return span > 2 * 86400 ? d.toLocaleDateString(undefined, { month: "short", day: "numeric" }) : d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" }); };
  let svg = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none"><g class="grid">`;
  ticks.forEach((v) => { svg += `<line x1="${P.l}" x2="${W - P.r}" y1="${y(v)}" y2="${y(v)}"/>`; });
  svg += `</g><g class="axis">`;
  ticks.forEach((v) => { svg += `<text x="${P.l - 6}" y="${y(v) + 4}" text-anchor="end">${v}${opt.unit || ""}</text>`; });
  xt.forEach((t, i) => { svg += `<text x="${x(t)}" y="${H - 4}" text-anchor="${i === 0 ? "start" : i === 3 ? "end" : "middle"}">${xl(t)}</text>`; });
  svg += `</g>`;
  series.forEach((s) => {
    let d = "", pen = false;
    s.data.forEach((v, i) => { if (v == null) { pen = false; return; } d += `${pen ? "L" : "M"}${x(ts[i]).toFixed(1)},${y(v).toFixed(1)}`; pen = true; });
    svg += `<path d="${d}" fill="none" stroke="${s.color}" stroke-width="2" stroke-linejoin="round" vector-effect="non-scaling-stroke"/>`;
  });
  svg += `<line class="hover" x1="0" x2="0" y1="${P.t}" y2="${H - P.b}" stroke="currentColor" stroke-opacity=".25" style="display:none"/></svg><div class="tip"></div>`;
  el.innerHTML = svg;
  const svgEl = $("svg", el), tip = $(".tip", el), hl = $(".hover", el);
  svgEl.addEventListener("mousemove", (e) => {
    const rect = svgEl.getBoundingClientRect(), px = ((e.clientX - rect.left) / rect.width) * W;
    const t = t0 + ((px - P.l) / (W - P.l - P.r)) * (t1 - t0);
    let i = 0, best = Infinity; ts.forEach((tt, j) => { const dd = Math.abs(tt - t); if (dd < best) { best = dd; i = j; } });
    hl.setAttribute("x1", x(ts[i])); hl.setAttribute("x2", x(ts[i])); hl.style.display = "";
    tip.innerHTML = `<div class="muted">${fmtDate(ts[i])}</div>` + series.map((s) => `<div><i style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${s.color};margin-right:6px"></i>${esc(s.name)}: <b>${s.data[i] == null ? "—" : +s.data[i].toFixed(1)}${opt.unit || ""}</b></div>`).join("");
    tip.style.display = "block";
    const left = (x(ts[i]) / W) * rect.width;
    tip.style.left = `${Math.min(left + 12, rect.width - tip.offsetWidth - 4)}px`; tip.style.top = "6px";
  });
  svgEl.addEventListener("mouseleave", () => { tip.style.display = "none"; hl.style.display = "none"; });
}
const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const legend = (items) => `<div class="legend">${items.map(([n, c]) => `<span><i style="background:${c}"></i>${esc(n)}</span>`).join("")}</div>`;

// ---------------------------------------------------------------- dashboard
async function renderDash() {
  const [drives, st, arr] = await Promise.all([api("/api/drives"), api("/api/status"), api("/api/arrays")]);
  if (currentRoute !== "dash") return;
  const n = (k) => st.counts[k] || 0;
  const temps = drives.filter((d) => d.temperature != null && d.state === "online").map((d) => d.temperature);
  const healths = drives.filter((d) => d.health != null && d.state !== "missing").map((d) => d.health);
  const order = { failing: 0, critical: 1, warning: 2, good: 3, unknown: 4 };
  drives.sort((a, b) => (a.state === "missing" ? -1 : 0) - (b.state === "missing" ? -1 : 0) || (order[a.status] ?? 5) - (order[b.status] ?? 5) || a.device.localeCompare(b.device, undefined, { numeric: true }));
  const bad = n("critical") + n("failing"), warn = n("warning"), miss = n("missing");
  const badPools = arr.arrays.filter((a) => a.role === "data" && ["degraded", "failed", "rebuilding"].includes(a.status));
  const worst = bad || badPools.some((a) => a.status !== "rebuilding") ? "critical" : warn || miss || badPools.length ? "warning" : drives.length ? "good" : "unknown";
  const others = bad ? ` · ${bad} drive${bad > 1 ? "s" : ""} at risk` : "";
  const headline = !drives.length ? "No drives detected yet"
    : badPools.length ? `Storage pool ${badPools.map((a) => a.name).join(", ")} is ${badPools[0].status}`
    : bad ? `${bad} drive${bad > 1 ? "s" : ""} at risk of failure`
    : warn ? `${warn} drive${warn > 1 ? "s" : ""} showing early warning signs` : miss ? `${miss} drive${miss > 1 ? "s" : ""} not responding` : "All drives are healthy";
  const sub = !drives.length ? "Run a scan to discover disks."
    : badPools.some((a) => a.status !== "rebuilding") ? `Replace the failed disk and let the pool rebuild. Until then a second failure means data loss${others}.`
    : badPools.length ? `Rebuild in progress${badPools[0].sync_pct != null ? ` (${badPools[0].sync_pct}%)` : ""}. Avoid heavy I/O until it finishes${others}.`
    : bad ? "Back up affected data and plan replacements now."
    : warn ? "Keep an eye on the flagged drives and make sure backups are current." : `Monitoring ${drives.length} drive${drives.length > 1 ? "s" : ""} · next scan ${fmtIn(st.next_scan)}`;
  const totalCap = drives.reduce((s, d) => s + (d.capacity || 0), 0);
  const avgH = healths.length ? Math.round(healths.reduce((a, b) => a + b, 0) / healths.length) : null;
  const shown = dashFilter === "all" ? drives : drives.filter((d) => dashFilter === "attention" ? (d.status !== "good" || d.state === "missing") : dashFilter === "ssd" ? d.is_ssd : !d.is_ssd);
  view.innerHTML = `
    <section class="hero st-${worst}">
      <div style="text-align:center">${ring(avgH, `st-${worst}`, 92)}<div class="muted small" style="margin-top:2px">avg health</div></div>
      <div><h1>${headline}</h1><p>${sub}</p></div>
      <div class="hero-stats">
        <div class="stat"><div class="label">Drives</div><div class="value">${drives.length}</div></div>
        <div class="stat"><div class="label">Capacity</div><div class="value">${fmtBytes(totalCap).replace(/ (\w+)$/, " <small>$1</small>")}</div></div>
        <div class="stat"><div class="label">Hottest</div><div class="value">${temps.length ? Math.max(...temps) + "<small>°C</small>" : "—"}</div></div>
        <div class="stat"><div class="label">Lowest health</div><div class="value">${healths.length ? Math.min(...healths) + "<small>%</small>" : "—"}</div></div>
      </div>
    </section>
    ${poolsSection(arr)}
    <div class="section-head"><h2>Drives</h2><span class="spacer"></span>
      <div class="chips">${[["all", `All ${drives.length}`], ["attention", `Needs attention ${bad + warn + miss}`], ["hdd", "HDD"], ["ssd", "SSD"]]
        .map(([k, l]) => `<button class="chip ${dashFilter === k ? "on" : ""}" data-f="${k}">${l}</button>`).join("")}</div></div>
    ${drives.length ? (shown.length ? `<div class="cards">${shown.map(card).join("")}</div>` : `<div class="panel empty">Nothing matches this filter.</div>`) :
      `<div class="panel empty"><h2>No drives yet</h2><p>${st.scanning ? "Scanning…" : "Run a scan, or check the container is privileged with <code>/dev</code> mounted."}</p></div>`}`;
  view.querySelectorAll("[data-f]").forEach((b) => b.onclick = () => { dashFilter = b.dataset.f; renderDash(); });
}
let dashFilter = "all";
const ARRAY_ST = { healthy: "good", syncing: "info", rebuilding: "warning", degraded: "critical", failed: "failing" };
function poolsSection(arr) {
  const data = arr.arrays.filter((a) => a.role === "data");
  if (!arr.arrays.length) return "";
  const sys = arr.arrays.filter((a) => a.role !== "data");
  const sysBad = sys.filter((a) => a.status !== "healthy");
  return `<div class="section-head"><h2>Storage pools</h2><span class="spacer"></span>
      <span class="muted small">QNAP system &amp; swap arrays: ${sysBad.length ? `<b style="color:var(--warning)">${sysBad.map((a) => `${a.name} ${a.status}`).join(", ")}</b>` : "all healthy"}</span></div>
    <div class="pools">${data.map((a) => {
      const cls = `st-${ARRAY_ST[a.status] || "unknown"}`;
      const left = a.redundancy_left;
      return `<div class="pool ${cls}">
        <div class="row" style="gap:8px"><b class="mono" style="font-size:14px">${esc(a.name)}</b><span class="badge plain st-unknown">${esc((a.level || "").toUpperCase())}</span>
          <span class="spacer"></span><span class="badge">${esc(a.status)}</span></div>
        <div class="muted small">${a.zfs ? `ZFS pool · state ${esc(a.state.toUpperCase())}` : `${fmtBytes(a.size_bytes)} · ${a.members.length} member${a.members.length > 1 ? "s" : ""} · ${left == null ? "" : left === 0 ? `<b style="color:var(--critical)">no redundancy left</b>` : `survives ${left} more disk failure${left > 1 ? "s" : ""}`}`}</div>
        ${a.sync_pct != null ? `<div class="row small" style="margin-top:2px"><span>${esc(a.sync)} ${a.sync_pct}%</span><span class="spacer"></span><span class="muted">${a.sync_eta_min ? `~${(a.sync_eta_min / 60).toFixed(1)} h left` : ""}</span></div><div class="progress"><div style="width:${a.sync_pct}%"></div></div>` : ""}
        ${a.zfs ? `<div class="muted small">Member disks aren't visible from inside the container. Check them in Storage &amp; Snapshots.</div>` : ""}
        <div class="members">${a.members.map((m) => {
          const d = arr.drives[m.disk] || {};
          const mc = m.failed ? "st-failing" : `st-${d.state === "missing" ? "missing" : d.status || "unknown"}`;
          const inner = `<span class="dot"></span>${esc(m.disk)}${m.spare ? " (spare)" : ""}${m.failed ? " FAILED" : ""}`;
          return d.serial ? `<a class="member ${mc}" href="#/drive/${encodeURIComponent(d.serial)}" title="${esc(d.model)}">${inner}</a>` : `<span class="member ${mc}">${inner}</span>`;
        }).join("")}</div></div>`;
    }).join("")}</div>`;
}
function fmtIn(ts) { const s = ts - Date.now() / 1000; if (s <= 60) return "soon"; if (s < 3600) return `in ${Math.round(s / 60)} min`; return `in ${Math.round(s / 3600)} h`; }
const ICONS = {
  hdd: `<svg class="type-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="3" y="3" width="18" height="18" rx="3"/><circle cx="12" cy="11" r="4.5"/><circle cx="12" cy="11" r="1"/><path d="M7 18h2"/></svg>`,
  ssd: `<svg class="type-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="2.5" y="6" width="19" height="12" rx="2"/><path d="M6 10h5M6 14h3M15 10h3v4h-3z"/></svg>`,
  ok: `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="m8 12 3 3 5-6"/></svg>`,
  alert: `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M12 3 2 20h20L12 3z"/><path d="M12 10v4M12 17h.01"/></svg>`,
};
function card(d) {
  const cls = stClass(d);
  const third = d.eta_days != null && d.status !== "good" ? ["Projected", fmtDays(d.eta_days)]
    : d.life_left != null ? ["Endurance", d.life_left + "%"] : ["Risk", d.risk != null ? d.risk + "%" : "—"];
  const issueCls = d.status === "good" ? "st-info" : cls;
  return `<a class="card ${cls}" href="#/drive/${encodeURIComponent(d.serial)}">
    <div class="card-head">${ring(d.state === "missing" ? null : d.health, cls, 58)}
      <div style="min-width:0;flex:1">
        <div class="row" style="gap:6px;flex-wrap:nowrap">${d.is_ssd ? ICONS.ssd : ICONS.hdd}<div class="card-title" title="${esc(d.model)}">${esc(d.model)}</div></div>
        <div class="card-sub">${esc(d.family || "")}${d.family ? " · " : ""}${fmtBytes(d.capacity)}</div>
        <div class="card-sub mono">${d.label ? `<b style="font-family:inherit">${esc(d.label)}</b> · ` : ""}${esc(d.device)} · ${esc(d.serial)}</div>
      </div></div>
    <div class="pill-row"><span class="badge">${statusLabel(d)}</span>
      ${d.state === "standby" ? `<span class="badge st-unknown">Sleeping</span>` : ""}
      ${d.limited ? `<span class="badge plain st-warning" title="This device reports no SMART attributes">Limited data</span>` : ""}
      ${d.selftest_running ? `<span class="badge st-info">Self-test ${d.selftest_remaining != null ? 100 - d.selftest_remaining + "%" : ""}</span>` : ""}
      ${d.ml_p != null && d.ml_p >= 0.01 ? `<span class="badge plain ${d.ml_p >= 0.1 ? "st-critical" : "st-warning"}" title="AI 30-day failure probability">AI ${(d.ml_p * 100).toFixed(1)}%</span>` : ""}
      ${d.known_issues ? `<span class="badge plain st-warning">Known issue</span>` : ""}
      ${d.perf_score != null && d.perf_score < 75 ? `<span class="badge plain st-warning">Slow ${d.perf_score}%</span>` : ""}
      ${(d.arrays || []).map((a) => `<span class="badge plain st-unknown mono">${esc(a)}</span>`).join("")}</div>
    <div class="metrics">
      <div><div class="k">Temp</div><div class="v">${d.temperature != null ? d.temperature + "°C" : "—"}</div></div>
      <div><div class="k">Age</div><div class="v">${fmtHours(d.power_on_hours)}</div></div>
      <div><div class="k">${third[0]}</div><div class="v" ${third[0] === "Projected" ? 'style="color:var(--fg)"' : ""}>${third[1]}</div></div>
    </div>
    ${d.top_issue ? `<div class="card-issue ${issueCls}"><span class="dot"></span><span>${esc(d.top_issue)}${d.issue_count > 1 ? ` <span class="muted">· +${d.issue_count - 1} more</span>` : ""}</span></div>`
      : `<div class="card-issue st-good"><span class="dot"></span><span class="muted">No warning signs</span></div>`}
  </a>`;
}

// ---------------------------------------------------------------- drive detail
const IMPORTANT = { 5: "c", 197: "c", 198: "c", 187: "c", 184: "c", 10: "w", 188: "w", 196: "w", 199: "w" };
let histDays = 30;

async function renderDrive(serial) {
  const det = await api(`/api/drives/${encodeURIComponent(serial)}`);
  if (currentRoute !== "drive") return;
  const s = det.summary, a = det.analysis, c = det.card, cls = stClass(c);
  const nv = s.nvme, sc = s.scsi, stt = s.selftest || {};
  const facts = [
    ["Device", s.device], ["Serial", s.serial], ["Firmware", s.firmware], ["Capacity", fmtBytes(s.capacity)],
    ["Type", driveType(s)], ["Interface", s.interface_speed || s.protocol], ["Temperature", s.temperature != null ? `${s.temperature}°C` : "—"],
    ["Powered on", `${fmtHours(s.power_on_hours)} (${fmtNum(s.power_on_hours)} h)`], ["Power cycles", fmtNum(s.power_cycles)],
    ["Failure risk", `${a.risk}%`], ["Endurance left", a.life_left != null ? `${a.life_left}%` : "n/a"],
    ["First seen", fmtDate(det.first_seen)], ["Last scanned", fmtAgo(c.last_seen)],
    ["Storage pool", (a.raid || []).filter((r) => r.role === "data").map((r) => `${r.array} (${(r.level || "").toUpperCase()}, ${r.status})`).join(", ") || "—"],
    ["Performance", det.bench_eval ? `${det.bench_eval.score}% of baseline` : "Not benchmarked yet"],
    ...usageFacts(a.usage || {}),
  ];
  view.innerHTML = `
  <div class="stack">
    <div class="crumbs"><a class="btn ghost" href="#/">← All drives</a><span class="spacer"></span>
      <a class="btn" href="/api/drives/${encodeURIComponent(serial)}/raw" target="_blank">Raw SMART data</a>
      <button class="btn danger" id="forgetBtn">Forget drive</button></div>
    <section class="panel ${cls}">
      <div class="detail-hero">
        ${ring(c.state === "missing" ? null : a.health, cls, 124)}
        <div style="flex:1;min-width:260px">
          <div class="muted small" style="font-weight:600;text-transform:uppercase;letter-spacing:.06em">${esc(driveType(s))}</div>
          <h1>${esc(s.model)}</h1>
          <div class="row" style="gap:6px;margin:4px 0"><input id="labelIn" value="${esc(c.label || "")}" maxlength="60" placeholder="Add a label, e.g. Bay 3" style="padding:4px 9px;width:210px;font-size:13px"><button class="btn ghost" id="labelBtn" style="padding:4px 10px">Save label</button></div>
          <div class="muted">${esc(s.family || "")}</div>
          <div class="row" style="margin-top:10px;gap:6px"><span class="badge">${statusLabel(c)}</span> ${c.state !== "online" ? `<span class="badge st-unknown">${esc(c.state)}</span>` : ""}
            ${s.smart_passed === false ? `<span class="badge st-failing">SMART failed</span>` : s.smart_passed ? `<span class="badge st-good">SMART passed</span>` : ""}
            <span class="badge plain st-unknown">Health ${a.health}%</span><span class="badge plain st-unknown">Risk ${a.risk}%</span></div>
        </div>
      </div>
      <div class="verdict">${a.status === "good" ? ICONS.ok : ICONS.alert}<span>${esc(a.verdict)}</span></div>
      <div class="facts">${facts.map(([k, v]) => `<div class="fact"><div class="k">${k}</div><div class="v">${esc(v ?? "—")}</div></div>`).join("")}</div>
    </section>

    <div class="grid cols-3">
      ${reliabilityPanel(a.reliability)}
      <section class="panel"><h2>Findings</h2>
        ${a.issues.length ? a.issues.map((i) => `<div class="issue st-${i.level === "info" ? "info" : i.level}"><span class="dot"></span><span>${esc(i.text)}</span><span class="impact" title="Contribution to failure risk">${i.impact}%</span></div>`).join("") : `<p class="muted">No warning signs detected.</p>`}
      </section>
      <section class="panel"><h2>Predictions</h2>
        ${a.predictions.length ? a.predictions.map((p) => `<div class="issue st-${p.level === "info" ? "info" : p.level}"><span class="dot"></span><span>${esc(p.text)}</span></div>`).join("") : `<p class="muted">No degrading trends detected in the last 30 days.${c.last_seen - det.first_seen < 86400 * 2 ? " Trend prediction needs a couple of days of history." : ""}</p>`}
      </section>
    </div>

    <div class="grid cols-3">
      ${aiPanel(a.ml, s)}
      ${peersPanel(det.peers)}
      ${knownPanel(a.known_issues)}
    </div>

    ${perfPanel(det)}

    <section class="panel">
      <div class="row" style="margin-bottom:10px"><h2 style="margin:0">History</h2><span class="spacer"></span>
        <div class="seg" id="rangeSeg">${[7, 30, 90, 365].map((d) => `<button data-d="${d}" class="${d === histDays ? "on" : ""}">${d}d</button>`).join("")}</div></div>
      <div class="grid cols-2" id="charts"></div>
    </section>

    <section class="panel">
      <div class="row" style="margin-bottom:10px"><h2 style="margin:0">Self-tests</h2><span class="spacer"></span>
        <button class="btn" data-test="short">Run short test</button><button class="btn" data-test="long">Run extended test</button>
        ${stt.running ? `<button class="btn danger" data-test="abort">Abort</button>` : ""}</div>
      ${stt.running ? `<p>Test in progress${stt.remaining != null ? ` — ${100 - stt.remaining}% complete` : ""}</p><div class="progress"><div style="width:${100 - (stt.remaining ?? 100)}%"></div></div>` : `<p class="muted small">${esc(stt.status || "")}</p>`}
      ${stt.log && stt.log.length ? `<div class="table-wrap" style="margin-top:10px"><table><thead><tr><th>Type</th><th>Result</th><th class="num">At power-on hour</th></tr></thead><tbody>
        ${stt.log.map((t) => `<tr class="${t.passed === false ? "row-crit" : ""}"><td>${esc(t.type)}</td><td>${esc(t.result)}</td><td class="num">${fmtNum(t.hours)}</td></tr>`).join("")}</tbody></table></div>` : `<p class="muted">No self-tests logged.</p>`}
    </section>

    ${nv ? `<section class="panel"><h2>NVMe health log</h2><div class="table-wrap"><table><tbody>
      ${[["Critical warning", "0x" + (nv.critical_warning || 0).toString(16)], ["Available spare", nv.available_spare + "%"], ["Spare threshold", nv.available_spare_threshold + "%"],
         ["Percentage used", nv.percentage_used + "%"], ["Data read", fmtBytes((nv.data_units_read || 0) * 512000)], ["Data written", fmtBytes((nv.data_units_written || 0) * 512000)],
         ["Media errors", fmtNum(nv.media_errors)], ["Error log entries", fmtNum(nv.num_err_log_entries)], ["Unsafe shutdowns", fmtNum(nv.unsafe_shutdowns)]]
        .map(([k, v]) => `<tr><td>${k}</td><td class="num">${esc(v)}</td></tr>`).join("")}</tbody></table></div></section>` : ""}
    ${sc ? `<section class="panel"><h2>SCSI error counters</h2><div class="table-wrap"><table><tbody>
      ${[["Grown defects", sc.grown_defects], ["Read uncorrected", sc.read_uncorrected], ["Write uncorrected", sc.write_uncorrected], ["Verify uncorrected", sc.verify_uncorrected]]
        .map(([k, v]) => `<tr><td>${k}</td><td class="num">${fmtNum(v)}</td></tr>`).join("")}</tbody></table></div></section>` : ""}

    ${s.attributes.length ? `<section class="panel"><h2>SMART attributes</h2><div class="table-wrap"><table>
      <thead><tr><th class="num">ID</th><th>Attribute</th><th class="num">Value</th><th class="num">Worst</th><th class="num">Threshold</th><th class="num">Raw</th><th>Type</th></tr></thead><tbody>
      ${s.attributes.map((at) => {
        const failing = at.thresh > 0 && at.value <= at.thresh;
        const imp = IMPORTANT[at.id], bad = imp && at.raw > 0 && !(at.id === 188 && (at.raw & 0xffff) === 0);
        const rc = failing || (bad && imp === "c") ? "row-crit" : bad ? "row-warn" : "";
        return `<tr class="${rc}"><td class="num">${at.id}</td><td>${esc(at.name.replace(/_/g, " "))}</td><td class="num">${at.value}</td><td class="num">${at.worst}</td><td class="num">${at.thresh}</td><td class="num mono">${esc(at.raw_str)}</td><td class="small muted">${at.prefailure ? "Pre-fail" : "Old age"}</td></tr>`;
      }).join("")}</tbody></table></div></section>` : ""}

    <section class="panel"><h2>Drive events</h2>${eventsTable(det.events, false)}</section>
  </div>`;

  $("#forgetBtn").onclick = async () => {
    if (!confirm("Remove this drive and all of its history from DriveSentinel? It will be re-added if it is detected again.")) return;
    await api(`/api/drives/${encodeURIComponent(serial)}/forget`, { method: "POST" }); location.hash = "#/";
  };
  view.querySelectorAll("[data-test]").forEach((b) => b.onclick = async () => {
    const kind = b.dataset.test;
    if (kind === "long" && !confirm("An extended test reads the entire surface and can take many hours (the drive remains usable). Start it?")) return;
    try { const r = await api(`/api/drives/${encodeURIComponent(serial)}/selftest`, { method: "POST", body: { type: kind } }); toast(r.message || "Sent"); setTimeout(() => route(), 6000); }
    catch (e) { toast("Failed: " + e.message); }
  });
  $("#labelBtn").onclick = async () => {
    try { await api(`/api/drives/${encodeURIComponent(serial)}/label`, { method: "POST", body: { label: $("#labelIn").value } }); toast("Label saved"); }
    catch (e) { toast(e.message); }
  };
  const bb = $("#benchBtn");
  if (bb) bb.onclick = async () => {
    try { const r = await api(`/api/drives/${encodeURIComponent(serial)}/benchmark`, { method: "POST" }); toast(r.message); bb.disabled = true; bb.textContent = "Running…";
      const poll = setInterval(async () => { const st = await api("/api/status"); if (!st.benching) { clearInterval(poll); route(); } }, 3000);
    } catch (e) { toast(e.message); }
  };
  drawPerfChart(det.benchmarks);
  $("#rangeSeg").onclick = (e) => { const d = e.target.dataset.d; if (!d) return; histDays = +d; view.querySelectorAll("#rangeSeg button").forEach((x) => x.classList.toggle("on", x.dataset.d === d)); drawCharts(serial, s); };
  drawCharts(serial, s);
}

function pct(p) { return p == null ? "—" : p < 0.001 ? "<0.1%" : p < 0.1 ? `${(p * 100).toFixed(2)}%` : `${(p * 100).toFixed(1)}%`; }
function aiPanel(m, s) {
  if (!m) {
    const why = s.protocol === "NVMe" || s.is_ssd ? "The AI model is trained on hard-drive SMART data, so SSD health comes from wear, spare capacity and error counters instead."
      : "Not enough SMART attributes for the AI model, or the model is disabled in Settings.";
    return `<section class="panel"><h2>AI failure prediction</h2><p class="muted">${why}</p></section>`;
  }
  const cls = m.p_horizon >= 0.1 ? "st-critical" : m.p_horizon >= 0.01 ? "st-warning" : "st-good";
  const max = 0.3, w = Math.min(100, (Math.log10(Math.max(m.p_horizon, 1e-4)) + 4) / (Math.log10(max) + 4) * 100);
  return `<section class="panel ${cls}"><h2>AI failure prediction</h2>
    <div class="row" style="align-items:baseline;gap:6px"><span style="font-size:28px;font-weight:700;letter-spacing:-.02em;color:var(--fg)">${pct(m.p_horizon)}</span><span class="muted">chance of failure in the next ${m.horizon_days} days</span></div>
    <div class="meter" style="margin:10px 0 4px"><div style="width:${w}%"></div></div>
    <div class="muted small" style="margin-bottom:14px">Log scale: 0.01% → 30%. ≈ ${pct(m.p_year)} over 12 months at the current state.</div>
    ${m.drivers.length ? `<div class="small muted" style="font-weight:600;margin-bottom:6px">MAIN DRIVERS</div>${m.drivers.map((d) => `<div class="row small" style="margin-bottom:4px"><span>${esc(d.factor)}</span><span class="spacer"></span><div class="meter" style="width:90px;height:6px"><div style="width:${Math.min(100, d.weight * 40)}%"></div></div></div>`).join("")}`
      : `<p class="small muted" style="margin:0">No SMART indicators above baseline.</p>`}
    <div class="muted small" style="margin-top:12px">Logistic-regression model trained on ${esc(m.trained_on)} · AUC ${m.auc}</div></section>`;
}
function knownPanel(list) {
  list = list || [];
  if (!list.length) return `<section class="panel st-good"><h2>Known issues</h2><p class="muted" style="margin:0">No known firmware bugs or design caveats for this model and firmware.</p></section>`;
  const rank = { critical: 0, warning: 1, info: 2 };
  return `<section class="panel"><h2>Known issues</h2>${list.sort((a, b) => rank[a.severity] - rank[b.severity]).map((k) => `
    <div class="issue st-${k.severity === "info" ? "info" : k.severity}"><span class="dot"></span><div><b>${esc(k.title)}</b>
      <div class="small muted">${esc(k.detail)}</div>${k.fix ? `<div class="small" style="margin-top:4px">Fixed in firmware <code>${esc(k.fix)}</code></div>` : ""}</div></div>`).join("")}</section>`;
}
function peersPanel(p) {
  if (!p || !p.group) return `<section class="panel"><h2>Compared with peers</h2><p class="muted" style="margin:0">No comparable drives in this NAS.</p></section>`;
  return `<section class="panel"><h2>Compared with ${esc(p.group)}</h2>
    ${p.insights.length ? p.insights.map((i) => `<div class="issue st-${i.level === "info" ? "info" : i.level}"><span class="dot"></span><span class="small">${esc(i.text)}</span></div>`).join("") : `<p class="muted small" style="margin-top:0">In line with its peers.</p>`}
    <div class="table-wrap" style="margin-top:10px"><table><thead><tr><th>Drive</th><th class="num">Temp</th><th class="num">Health</th><th class="num">Errors</th><th class="num">MB/s</th></tr></thead><tbody>
    ${p.peers.map((r, i) => `<tr${i === 0 ? ' style="font-weight:650"' : ""}><td class="mono small">${i === 0 ? "▶ " : ""}<a href="#/drive/${encodeURIComponent(r.serial)}">${esc(r.device)}</a></td><td class="num">${r.temp ?? "—"}°</td><td class="num">${r.health ?? "—"}%</td><td class="num">${fmtNum(r.sector_errors)}</td><td class="num">${r.seq ? Math.round(r.seq) : "—"}</td></tr>`).join("")}
    </tbody></table></div></section>`;
}
function perfPanel(det) {
  const ev = det.bench_eval, L = ev && ev.latest;
  const cls = !ev ? "" : ev.score < 55 ? "st-critical" : ev.score < 75 ? "st-warning" : "st-good";
  return `<section class="panel ${cls}">
    <div class="row" style="margin-bottom:12px"><h2 style="margin:0">Performance</h2><span class="spacer"></span>
      <span class="muted small">Read-only test · ~1 min</span><button class="btn" id="benchBtn" ${det.benching ? "disabled" : ""}>${det.benching ? "Running…" : "Run benchmark"}</button></div>
    ${ev ? `<div class="grid cols-2" style="align-items:start">
      <div><div class="row" style="align-items:baseline;gap:6px"><span style="font-size:28px;font-weight:700;letter-spacing:-.02em;color:var(--fg)">${ev.score}%</span><span class="muted">of this drive's baseline speed</span></div>
        <div class="kv" style="margin-top:12px">
          <span class="k">Sequential (outer / middle / inner)</span><span class="v">${L.seq_mbps.map((v) => v == null ? "—" : Math.round(v)).join(" / ")} MB/s</span>
          <span class="k">Baseline sequential avg</span><span class="v">${Math.round(ev.baseline_seq)} MB/s</span>
          <span class="k">Random 4K read latency</span><span class="v">${L.rand_lat_ms} ms (p95 ${L.rand_p95_ms})</span>
          <span class="k">Random 4K IOPS</span><span class="v">${fmtNum(L.rand_iops)}</span>
          <span class="k">Runs recorded</span><span class="v">${ev.runs}</span>
          <span class="k">Last run</span><span class="v">${fmtAgo(L.ts)}</span>
        </div></div>
      <div class="chart-card"><div class="row"><b>Sequential read over time</b><span class="spacer"></span>${legend([["Outer", css("--c1")], ["Middle", css("--c2")], ["Inner", css("--c4")]])}</div><div class="chart" id="pc"></div></div>
    </div>` : `<p class="muted" style="margin:0">No benchmarks yet. A baseline is recorded automatically about an hour after a drive is discovered, then on the schedule set in Settings. Or run one now.</p>`}
  </section>`;
}
function drawPerfChart(rows) {
  const el = $("#pc"); if (!el || !rows || !rows.length) return;
  lineChart(el, rows.map((r) => r.ts), [0, 1, 2].map((i) => ({ name: ["Outer", "Middle", "Inner"][i], color: css(["--c1", "--c2", "--c4"][i]), data: rows.map((r) => (r.seq_mbps || [])[i] ?? null) })), { unit: "", floor: 0 });
}

function usageFacts(u) {
  const out = [];
  if (u.tb_written != null) out.push(["Data written", `${fmtNum(u.tb_written)} TB${u.tb_read != null ? ` (read ${fmtNum(u.tb_read)} TB)` : ""}`]);
  if (u.tb_per_year != null) out.push(["Workload", `${fmtNum(u.tb_per_year)} TB/year${u.workload_rating ? ` (rated ${u.workload_rating}, ${u.drive_class})` : ""}`]);
  if (u.load_cycles != null) out.push(["Head load cycles", `${fmtNum(u.load_cycles)} of ${fmtNum(u.load_rating)}${u.load_per_hour != null ? ` · ${u.load_per_hour}/h` : ""}`]);
  return out;
}

function reliabilityPanel(r) {
  if (!r) return "";
  const max = Math.max(4, r.adjusted_afr * 1.25, r.fleet_average * 1.5);
  const cls = r.adjusted_afr >= 5 ? "st-critical" : r.adjusted_afr >= 2.5 ? "st-warning" : "st-good";
  const matchLbl = { model: "Exact model match", family: "Product-family match", default: "No model data — generic baseline" }[r.match_type];
  return `<section class="panel ${cls}"><h2>Model reliability</h2>
    <div class="muted small" style="margin-top:-8px;margin-bottom:12px">${esc(r.matched)} · ${matchLbl}</div>
    <div class="row" style="align-items:baseline;gap:6px"><span style="font-size:28px;font-weight:700;letter-spacing:-.02em">${r.adjusted_afr}%</span><span class="muted">expected annual failure rate</span></div>
    <div class="meter" style="margin:10px 0 6px"><div style="width:${(r.adjusted_afr / max) * 100}%"></div><span class="mark" style="left:${(r.fleet_average / max) * 100}%" title="Fleet average"></span></div>
    <div class="muted small" style="margin-bottom:14px">Marker = fleet average (${r.fleet_average}%)</div>
    <div class="kv">
      <span class="k">Historical AFR for model</span><span class="v">${r.afr}%</span>
      <span class="k">Vs. fleet average</span><span class="v">${r.vs_fleet != null ? r.vs_fleet + "×" : "—"}</span>
      <span class="k">Age adjustment</span><span class="v">×${r.age_multiplier} (${r.age_years}y)</span>
      <span class="k">Lifecycle phase</span><span class="v">${esc(r.age_phase || "—")}</span>
      <span class="k">Source</span><span class="v">${esc(r.source)}</span>
      ${r.drives ? `<span class="k">Evidence</span><span class="v">${fmtNum(r.drives)} drives · ${fmtNum(r.failures)} failures</span>` : ""}
    </div></section>`;
}

async function drawCharts(serial, s) {
  const h = await api(`/api/drives/${encodeURIComponent(serial)}/history?days=${histDays}`);
  const box = $("#charts"); if (!box) return;
  const C = [css("--c1"), css("--c2"), css("--c3"), css("--c4")];
  const errName = s.protocol === "NVMe" ? "Media errors" : "Offline uncorrectable";
  const charts = [
    { title: "Health", key: "hc", series: [{ name: "Health", data: h.health, color: css("--good") }], opt: { min: 0, max: 100, unit: "%" } },
    { title: "Temperature", key: "tc", series: [{ name: "Temperature", data: h.temp, color: C[1], agg: "max" }], opt: { unit: "°" } },
    { title: "Error counters", key: "ec", series: [
        { name: s.protocol === "SCSI" ? "Grown defects" : "Reallocated", data: h.realloc, color: C[0], agg: "max" },
        { name: "Pending", data: h.pending, color: C[1], agg: "max" },
        { name: errName, data: h.uncorrectable, color: C[2], agg: "max" }], opt: { floor: 0 } },
  ];
  if (h.wear.some((v) => v != null)) charts.push({ title: "Endurance used", key: "wc", series: [{ name: "Used", data: h.wear, color: C[3], agg: "max" }], opt: { unit: "%", floor: 0 } });
  box.innerHTML = charts.map((c) => `<div class="chart-card"><div class="row"><b>${c.title}</b><span class="spacer"></span>${c.series.length > 1 ? legend(c.series.map((x) => [x.name, x.color])) : ""}</div><div class="chart" id="${c.key}"></div></div>`).join("");
  charts.forEach((c) => lineChart($("#" + c.key), h.ts, c.series, c.opt));
}

// ---------------------------------------------------------------- events
function eventsTable(events, showDrive = true) {
  if (!events.length) return `<p class="muted">No events.</p>`;
  return `<div class="table-wrap"><table><thead><tr><th>Time</th><th>Level</th>${showDrive ? "<th>Drive</th>" : ""}<th>Event</th></tr></thead><tbody>
    ${events.map((e) => `<tr><td class="small" style="white-space:nowrap">${fmtDate(e.ts)}</td><td><span class="badge st-${e.level === "info" ? "info" : e.level}">${e.level}</span></td>
    ${showDrive ? `<td class="mono small">${e.serial ? `<a href="#/drive/${encodeURIComponent(e.serial)}">${esc(e.serial)}</a>` : "—"}</td>` : ""}
    <td><b>${esc(e.title)}</b>${e.message ? `<div class="small muted" style="white-space:pre-wrap">${esc(e.message)}</div>` : ""}</td></tr>`).join("")}</tbody></table></div>`;
}
async function renderEvents() {
  const evs = await api("/api/events?limit=500");
  if (currentRoute !== "events") return;
  view.innerHTML = `<section class="panel"><div class="row" style="margin-bottom:12px"><h2 style="margin:0">Event log</h2><span class="spacer"></span>
    <select id="lvl"><option value="">All levels</option><option value="warning">Warning+</option><option value="critical">Critical only</option></select></div><div id="evt"></div></section>`;
  const rank = { info: 0, warning: 1, critical: 2 };
  const draw = () => { const m = $("#lvl").value; $("#evt").innerHTML = eventsTable(evs.filter((e) => !m || rank[e.level] >= rank[m])); };
  $("#lvl").onchange = draw; draw();
}

// ---------------------------------------------------------------- settings
const SETTINGS = [
  ["Monitoring", [
    ["scan_interval_min", "Scan interval (minutes)", "number", "How often SMART data is collected."],
    ["devices", "Devices", "text", "Leave empty to auto-detect. Or list devices, e.g. /dev/sda:sat,/dev/nvme0"],
    ["skip_standby", "Don't wake sleeping disks", "check", "Skips disks in standby so they can spin down."],
    ["retention_days", "History retention (days)", "number", "After 30 days, history is thinned to one reading per 6 hours."],
    ["maintenance_window", "Maintenance window", "text", "When scheduled extended self-tests and benchmarks may start, e.g. 01:00-06:00. Empty = any time."],
  ]],
  ["Thresholds", [
    ["hdd_temp_warn", "HDD temperature warning (°C)", "number", ""], ["hdd_temp_crit", "HDD temperature critical (°C)", "number", ""],
    ["ssd_temp_warn", "SSD/NVMe temperature warning (°C)", "number", ""], ["ssd_temp_crit", "SSD/NVMe temperature critical (°C)", "number", ""],
  ]],
  ["Scheduled self-tests", [
    ["short_test_interval_days", "Short test every N days", "number", "0 disables. Takes ~2 minutes."],
    ["long_test_interval_days", "Extended test every N days", "number", "0 disables. Can take many hours on large HDDs."],
  ]],
  ["Benchmarks", [
    ["bench_interval_days", "Benchmark every N days", "number", "0 disables. Read-only, roughly a minute per drive. Skipped while a RAID rebuild is running."],
    ["bench_seq_mb", "MB read per position", "number", "Read at the outer, middle and inner part of the disk."],
  ]],
  ["AI & datasets", [
    ["ml_enabled", "Use the AI failure-prediction model", "check", ""],
    ["backblaze_quarters", "Backblaze quarters to use", "number", "Each quarter is a 1–2 GB download. More quarters give more stable results but take longer."],
  ]],
  ["Notifications", [
    ["alert_min_level", "Minimum level to notify", ["info", "warning", "critical"], ""],
    ["temp_alert_cooldown_h", "Overheat alert cooldown (hours)", "number", ""],
  ]],
  ["Email (SMTP)", [
    ["smtp_host", "SMTP server", "text", "e.g. smtp.gmail.com"], ["smtp_port", "Port", "number", ""],
    ["smtp_security", "Security", ["starttls", "ssl", "none"], ""], ["smtp_user", "Username", "text", ""],
    ["smtp_pass", "Password / app password", "password", ""], ["smtp_from", "From address", "text", ""],
    ["smtp_to", "To address(es)", "text", "Comma separated"],
  ]],
  ["Push & chat", [
    ["discord_webhook", "Discord webhook URL", "password", ""], ["slack_webhook", "Slack webhook URL", "password", ""],
    ["telegram_token", "Telegram bot token", "password", ""], ["telegram_chat_id", "Telegram chat ID", "text", ""],
    ["ntfy_url", "ntfy topic URL", "text", "e.g. https://ntfy.sh/my-nas-alerts"], ["ntfy_token", "ntfy access token", "password", "Optional"],
    ["webhook_url", "Generic webhook URL", "password", "Receives a JSON POST (Home Assistant, n8n, etc.)"],
  ]],
];
async function renderSettings() {
  const cfg = await api("/api/settings");
  if (currentRoute !== "settings") return;
  const field = ([k, label, type, hint]) => {
    if (type === "check") return `<label class="check"><input type="checkbox" name="${k}" ${cfg[k] ? "checked" : ""}> ${label}</label>`;
    const input = Array.isArray(type) ? `<select name="${k}">${type.map((o) => `<option ${cfg[k] === o ? "selected" : ""}>${o}</option>`).join("")}</select>`
      : `<input name="${k}" type="${type}" value="${esc(cfg[k])}" autocomplete="off">`;
    return `<label class="field">${label}${input}${hint ? `<span class="hint">${esc(hint)}</span>` : ""}</label>`;
  };
  const sid = (t) => "s-" + t.toLowerCase().replace(/[^a-z]+/g, "-");
  view.innerHTML = `<div class="settings-layout">
    <aside class="settings-nav">${SETTINGS.map(([t]) => `<a href="javascript:void 0" data-jump="${sid(t)}">${t}</a>`).join("")}</aside>
    <form id="sf" class="stack">${SETTINGS.map(([title, fields]) => `<section class="panel" id="${sid(title)}"><h2>${title}</h2><div class="form-grid">${fields.map(field).join("")}</div></section>`).join("")}
    <div class="savebar"><button class="btn primary" type="submit">Save settings</button><button class="btn" type="button" id="testBtn">Send test notification</button>
    <span class="muted small">Active channels: ${esc((statusCache?.channels || []).join(", ") || "none")}</span></div></form></div>`;
  view.querySelectorAll("[data-jump]").forEach((a) => a.onclick = () => document.getElementById(a.dataset.jump).scrollIntoView({ behavior: "smooth", block: "start" }));
  const aiSec = $("#s-ai-datasets");
  if (aiSec) {
    aiSec.insertAdjacentHTML("beforeend", `<div id="bbBox" style="margin-top:18px"></div>`);
    drawBB();
  }
  const collect = () => { const out = {}; SETTINGS.forEach(([, fs]) => fs.forEach(([k, , t]) => { const el = $(`[name="${k}"]`); out[k] = t === "check" ? el.checked : el.value; })); return out; };
  $("#sf").onsubmit = async (e) => { e.preventDefault(); try { await api("/api/settings", { method: "POST", body: collect() }); toast("Settings saved"); refreshStatus(); } catch (err) { toast("Save failed: " + err.message); } };
  $("#testBtn").onclick = async () => {
    try { await api("/api/settings", { method: "POST", body: collect() }); const r = await api("/api/alerts/test", { method: "POST" });
      toast(Object.entries(r.results).map(([k, v]) => `${k}: ${v}`).join(" · ")); }
    catch (err) { toast(err.message); }
  };
}

async function drawBB() {
  const box = $("#bbBox"); if (!box) return;
  const [st, job] = await Promise.all([api("/api/status"), api("/api/backblaze")]);
  const ds = st.dataset, m = st.ml;
  const running = job.state === "running";
  box.innerHTML = `<div class="grid cols-2">
    <div class="chart-card"><b>Reliability dataset</b><div class="kv small" style="margin-top:8px">
      <span class="k">Source</span><span class="v">${ds ? (ds.custom ? "Your import" : "Built-in") : "Curated table only"}</span>
      <span class="k">Data period</span><span class="v">${esc(ds?.as_of || "—")}</span>
      <span class="k">Drive models</span><span class="v">${ds?.models ?? "—"}</span></div></div>
    <div class="chart-card"><b>AI model</b><div class="kv small" style="margin-top:8px">
      <span class="k">Trained on</span><span class="v">${esc(m?.trained_on || "—")}</span>
      <span class="k">Accuracy (AUC)</span><span class="v">${m?.auc ?? "—"}</span>
      <span class="k">Failure samples</span><span class="v">${fmtNum(m?.positives)}</span>
      ${m?.recall_at_1pct != null ? `<span class="k">Failing drives flagged in time</span><span class="v">${Math.round(m.recall_at_1pct * 100)}%</span>
      <span class="k">Healthy drives flagged by mistake</span><span class="v">${(m.false_alarm_rate_at_1pct * 100).toFixed(2)}%</span>` : ""}</div></div></div>
    <div class="row" style="margin-top:14px">
      <button class="btn primary" type="button" id="bbBtn" ${running ? "disabled" : ""}>Update from Backblaze</button>
      <span class="small muted">Downloads the latest public Drive Stats, recomputes failure rates per model and retrains the AI model. This runs on your NAS: allow 10–60 min per quarter and keep 1–2 GB of free space in /data.</span></div>
    ${job.state && job.state !== "idle" ? `<div style="margin-top:12px"><div class="row small"><b>${esc(job.step || "")}</b><span class="spacer"></span><span>${job.progress ?? 0}%</span></div>
      <div class="progress" style="margin-top:6px"><div style="width:${job.progress || 0}%"></div></div>
      ${job.error ? `<p class="small" style="color:var(--critical)">${esc(job.error)}</p>` : ""}
      ${job.result ? `<p class="small muted">Done: ${job.result.models} models · fleet AFR ${job.result.fleet_afr}% · AUC ${job.result.auc}</p>` : ""}
      ${(job.log || []).length ? `<pre class="mono small muted" style="max-height:140px;overflow:auto;white-space:pre-wrap;margin:8px 0 0">${esc(job.log.slice(-8).join("\n"))}</pre>` : ""}</div>` : ""}`;
  $("#bbBtn").onclick = async () => {
    const q = $(`[name="backblaze_quarters"]`).value;
    if (!confirm(`Download ${q} quarter(s) of Backblaze data (about ${q * 1.5} GB) and retrain? This can take a while on a NAS.`)) return;
    try { await api("/api/backblaze", { method: "POST", body: { quarters: +q } }); toast("Backblaze update started"); drawBB(); } catch (e) { toast(e.message); }
  };
  if (running) setTimeout(() => { if (currentRoute === "settings") drawBB(); }, 3000);
}

// ---------------------------------------------------------------- shell
async function refreshStatus() {
  try {
    statusCache = await api("/api/status");
    $("#scanInfo").textContent = statusCache.scanning ? "Scanning…" : `Updated ${fmtAgo(statusCache.last_scan)}`;
    $("#scanInfo").classList.toggle("busy", statusCache.scanning);
    $("#scanBtn").disabled = statusCache.scanning;
    const errs = statusCache.errors || [];
    let html = "";
    if (errs.length) html += `<div class="bar st-warning">${errs.map(esc).join("<br>")}</div>`;
    if (statusCache.mock) html += `<div class="bar st-info">Demo mode — showing simulated drives (MOCK=1).</div>`;
    $("#banner").innerHTML = html;
  } catch { $("#scanInfo").textContent = "Offline"; }
}
$("#scanBtn").onclick = async () => {
  try { await api("/api/scan", { method: "POST" }); toast("Scan started"); $("#scanBtn").disabled = true;
    const wait = setInterval(async () => { await refreshStatus(); if (!statusCache.scanning) { clearInterval(wait); route(); } }, 1500);
  } catch (e) { toast(e.message); }
};

const THEME_ICON = {
  light: `<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>`,
  dark: `<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg>`,
};
function effectiveTheme() { return document.documentElement.dataset.theme || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light"); }
function paintThemeBtn() { $("#themeBtn").innerHTML = THEME_ICON[effectiveTheme() === "dark" ? "light" : "dark"]; }
$("#themeBtn").onclick = () => {
  const next = effectiveTheme() === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem("ds-theme", next); } catch (e) { /* storage unavailable */ }
  paintThemeBtn(); if (currentRoute === "drive") route();
};
paintThemeBtn();

async function route() {
  const h = location.hash.replace(/^#\/?/, "");
  const [page, arg] = h.split("/");
  currentRoute = page === "drive" ? "drive" : page === "events" ? "events" : page === "settings" ? "settings" : "dash";
  document.querySelectorAll("nav a").forEach((a) => a.classList.toggle("active", a.dataset.nav === (currentRoute === "drive" ? "dash" : currentRoute)));
  try {
    if (currentRoute === "drive") await renderDrive(decodeURIComponent(arg || ""));
    else if (currentRoute === "events") await renderEvents();
    else if (currentRoute === "settings") await renderSettings();
    else await renderDash();
  } catch (e) { view.innerHTML = `<div class="panel empty">Error: ${esc(e.message)}</div>`; }
}
window.addEventListener("hashchange", () => { window.scrollTo(0, 0); route(); });
let resizeT; window.addEventListener("resize", () => { clearTimeout(resizeT); resizeT = setTimeout(() => { if (currentRoute === "drive") { const s = $("#charts"); if (s) route(); } }, 250); });
setInterval(() => { refreshStatus(); if (currentRoute === "dash") renderDash().catch(() => {}); }, 30000);
refreshStatus().then(route);
