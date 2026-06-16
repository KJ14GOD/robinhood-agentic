const $ = (s) => document.querySelector(s);
const $$ = (s) => document.querySelectorAll(s);
const api = async (path, body) => {
  const opt = body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {};
  return (await fetch("/api/" + path, opt)).json();
};
const money = (n) => "$" + (n || 0).toLocaleString(undefined, { maximumFractionDigits: 2 });
const money0 = (n) => "$" + (n || 0).toLocaleString(undefined, { maximumFractionDigits: 0 });
const pct = (n) => (n == null ? "—" : (n >= 0 ? "+" : "") + n.toFixed(2) + "%");
const cls = (n) => (n == null ? "" : n >= 0 ? "pos" : "neg");
const esc = (s) => String(s ?? "").replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
const localTime = (s) => {
  if (!s) return "";
  const d = new Date(s);
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
};
const mdLite = (s) => {
  let out = esc(s)
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/^#+\s*(.+)$/gm, "<strong>$1</strong>");
  out = out.replace(/(?:^|\n)- (.+)(?=\n|$)/g, (_, item) => `\n<li>${item}</li>`);
  out = out.replace(/(<li>.*<\/li>)/gs, "<ul>$1</ul>");
  return out.replace(/\n/g, "<br>").replace(/<br><ul>/g, "<ul>").replace(/<\/ul><br>/g, "</ul>");
};
const toast = (m) => { const t = $("#toast"); t.textContent = m; t.classList.remove("hidden"); setTimeout(() => t.classList.add("hidden"), 2200); };
const busy = (b, on) => { b.disabled = on; b.dataset.t = b.dataset.t || b.innerHTML; b.innerHTML = on ? '<span class="spin"></span>' : b.dataset.t; };

let STATE = null;
let SELECTED_CHART = "portfolio";
let CHART_SPAN = "3m";
const CHART_STORE = {};
const CHART_CACHE = {};   // body cache, TTL'd — decides when to refetch from the server
const CHART_RAW = {};     // last raw server chart per key, no TTL — for re-applying the live tip
const PREFETCHING = new Set();
const SPARK_SPAN = "1m";          // span used for the per-holding row sparklines
const SPAN_WORD = { "1d": "Today", "1w": "Past week", "1m": "Past month", "3m": "Past 3 months", "6m": "Past 6 months", "1y": "Past year" };
const spanWord = (s) => SPAN_WORD[s] || (s || "").toUpperCase();

function chartTtl(span) {
  return ({ "1d": 20000, "1w": 45000, "1m": 120000, "3m": 300000, "6m": 600000, "1y": 900000 })[span] || 300000;
}

function cachedChart(key) {
  const hit = CHART_CACHE[key];
  if (!hit) return null;
  return Date.now() - hit.at < chartTtl(hit.span) ? hit.chart : null;
}

function setCachedChart(key, span, chart) {
  CHART_CACHE[key] = { at: Date.now(), span, chart };
  CHART_RAW[key] = chart;
}

function clearChartCache() {
  for (const key of Object.keys(CHART_CACHE)) delete CHART_CACHE[key];
  PREFETCHING.clear();
}

// The live value for whatever the chart is currently showing, taken straight from
// the freshest STATE we have — the exact same numbers the header and holdings list
// render. The chart tip is pinned to this so it can never disagree with them.
function liveValueForChart(ticker) {
  if (!STATE || !STATE.portfolio) return null;
  if (ticker === "portfolio") return STATE.portfolio.total_value || null;
  const h = (STATE.portfolio.holdings || []).find((x) => x.ticker === ticker);
  return h && h.current_price ? h.current_price : null;
}

// Return a copy of the server chart whose rightmost point reflects the live value
// at STATE.as_of. The server already tries to anchor a "now" point; we override it
// client-side so the tip keeps moving every state refresh even if a chart fetch
// failed or was skipped. Replace a near-now trailing point; otherwise extend.
function withLiveTip(chart, ticker) {
  const live = liveValueForChart(ticker);
  if (!chart || !Array.isArray(chart.points) || !chart.points.length || !live) return chart;
  const pts = chart.points.slice();
  const stamp = STATE.as_of || new Date().toISOString();
  const lastAt = new Date(pts[pts.length - 1].at).getTime();
  const recent = Number.isFinite(lastAt) && Date.now() - lastAt < 15 * 60 * 1000;
  const tip = { at: stamp, close: live };
  if (recent) pts[pts.length - 1] = tip; else pts.push(tip);
  const first = pts[0].close || 0;
  const ret = first ? ((live - first) / first) * 100 : (chart.return_pct || 0);
  return { ...chart, points: pts, latest: live, return_pct: ret };
}

// Paint a raw server chart into the box with the live tip applied. Used by both the
// fetch path and the no-network tip refresh, so they always render identically.
function paintChart(box, rawChart, ticker) {
  CHART_RAW[`${ticker}:${CHART_SPAN}`] = rawChart;
  const chart = withLiveTip(rawChart, ticker);
  box.classList.remove("loading", "refreshing");
  // The period is the calm subtitle under the title; the live move (value + colored
  // change) lives in the big price block inside chartBlock and updates on hover.
  $("#chartMeta").textContent = spanWord(CHART_SPAN);
  box.innerHTML = chartBlock(chart);
  bindChartInteractions();
}

// tabs
$$(".tab").forEach((t) => t.addEventListener("click", () => {
  $$(".tab").forEach((x) => x.classList.remove("active"));
  $$(".panel").forEach((x) => x.classList.remove("active"));
  t.classList.add("active"); $("#" + t.dataset.tab).classList.add("active");
  if (t.dataset.tab === "portfolio" && STATE) { loadPortfolioChart(); loadStructuralRisk(); }
  if (t.dataset.tab === "activity" && STATE) loadActivity();
  if (t.dataset.tab === "memory" && STATE) { renderMemory(); loadMissions(); loadDeepLog(); }
  if (t.dataset.tab === "shadow" && STATE) loadScore(true);
  if (t.dataset.tab === "evals" && STATE) loadEvals();
  if (t.dataset.tab === "autopilot" && STATE) loadAutopilot();
  if (t.dataset.tab === "home" && STATE) { loadMandate(); renderHome(); }
}));

// ---------- state / header ----------
async function loadState() {
  STATE = await api("state");
  renderState();
  if ($("#activity").classList.contains("active")) loadActivity();
}

function renderState() {
  $("#srcTag").textContent = STATE.source.toUpperCase();
  $("#totVal").textContent = money0(STATE.portfolio.total_value);
  const n = STATE.portfolio.holdings.length;
  const stamp = localTime(STATE.as_of || STATE.portfolio.as_of);
  const sync = STATE.sync_ok ? `updated ${stamp || "now"}` : `sync issue`;
  // Show actual cash to deploy, not broker "buying power" (which can include margin/Instant credit — borrowed money, misleading on an investing tool).
  $("#totMeta").textContent = `${n} position${n === 1 ? "" : "s"} · cash ${money0(STATE.portfolio.cash ?? 0)} · ${sync}`;
  renderStaleBanner();
  if (!STATE.sync_ok && STATE.sync_message) toast(STATE.sync_message);
  else if (STATE.portfolio.pricing_warning) console.warn(STATE.portfolio.pricing_warning);
  renderHome();
  renderHoldings(); renderAllocation(); renderProfile(); renderEditor();
  if ($("#portfolio").classList.contains("active")) loadPortfolioChart();
  if ($("#memory").classList.contains("active")) renderMemory();
}

function renderStaleBanner() {
  const el = $("#staleBanner");
  if (!el) return;
  const r = STATE?.refresh;
  if (r && r.stale) {
    el.textContent = "⚠ " + r.message;
    el.classList.remove("hidden");
  } else {
    el.classList.add("hidden");
  }
}

async function refreshLive({ quiet = false } = {}) {
  try {
    const next = await api("refresh", {});
    STATE = next;
    renderState();
    if (!quiet) toast(STATE.sync_ok ? "Live data refreshed" : "Refresh had a sync issue");
  } catch (e) {
    if (!quiet) toast("Live refresh failed");
  }
}

$("#refreshAll").onclick = async (e) => {
  busy(e.target, true);
  clearChartCache();
  await refreshLive({ quiet: true });
  if ($("#portfolio").classList.contains("active")) await loadPortfolioChart({ force: true });
  await loadScore();
  busy(e.target, false);
  toast(STATE.sync_ok ? "Live data refreshed" : "Refresh had a sync issue");
};

// (the old Today metrics strip is now the opener message in the Home stream — see openerHTML)

function renderHoldings() {
  const hs = STATE.portfolio.holdings;
  $("#holdRows").innerHTML = hs.map((h) => `
    <div class="hrow ${SELECTED_CHART === h.ticker ? "selected" : ""}" onclick="selectPortfolioChart('${h.ticker}')">
      <div class="hrow-l">
        <div class="sym">${h.ticker}</div>
        <div class="sub2">${h.quantity}@$${h.current_price.toFixed(2)} · ${h.weight.toFixed(1)}%</div>
      </div>
      <div class="hrow-spark ${cls(h.unrealized_pct)}" data-spark="${h.ticker}"></div>
      <div class="hrow-v">
        <div class="val">${money0(h.market_value)}</div>
        <div class="chg ${cls(h.unrealized_pct)}">${pct(h.unrealized_pct)}</div>
      </div>
      <button class="ghost mini hrow-an" onclick="event.stopPropagation(); analyze('${h.ticker}')">Analyze</button>
    </div>`).join("") || `<p class="muted hrow-empty">No holdings yet.${STATE.source === "manual" ? " Add some below." : ""}</p>`;
  $("#editor").classList.toggle("hidden", STATE.source !== "manual");
  $("#holdNote").textContent = !STATE.sync_ok ? STATE.sync_message :
    STATE.source === "manual" ? "" :
    `read-only · ${STATE.portfolio.pricing_source || "Robinhood"}`;
  loadSparklines();
}

// Tiny price line for each holding row, colored to match the row's P/L so a row
// reads as one green/red unit. Reuses the same /api/chart endpoint + cache as the
// hero, so each ticker is fetched at most once per page load.
function sparkSvg(points, k) {
  const { path } = svgPath(points, 80, 26, 2);
  if (!path) return "";
  const stroke = k === "neg" ? "#ff6f5e" : "#3fcf6a";
  return `<svg viewBox="0 0 80 26" preserveAspectRatio="none" aria-hidden="true"><path d="${path}" fill="none" stroke="${stroke}" stroke-width="1.6" vector-effect="non-scaling-stroke"/></svg>`;
}

function loadSparklines() {
  const holds = (STATE && STATE.portfolio && STATE.portfolio.holdings) || [];
  holds.forEach((h) => {
    const key = `${h.ticker}:${SPARK_SPAN}`;
    const draw = (chart) => {
      const el = document.querySelector(`.hrow-spark[data-spark="${h.ticker}"]`);
      if (el && chart && chart.points) el.innerHTML = sparkSvg(chart.points, cls(h.unrealized_pct));
    };
    const ready = cachedChart(key) || CHART_RAW[key];
    if (ready) { draw(ready); return; }
    if (PREFETCHING.has(key)) return;
    PREFETCHING.add(key);
    fetch(`/api/chart/${encodeURIComponent(h.ticker)}?span=${SPARK_SPAN}`)
      .then((r) => r.json())
      .then((chart) => { if (chart && chart.points) { setCachedChart(key, SPARK_SPAN, chart); draw(chart); } })
      .catch(() => {})
      .finally(() => PREFETCHING.delete(key));
  });
}

// Allocation donut — the same holdings as the list, seen as one ring. Slices and
// legend rows drive the hero chart too, and the whole ring reacts to the current
// selection (focused slice pops, the rest dim, center shows its weight).
const ALLOC_COLORS = ["#10a348", "#0d9488", "#2563eb", "#6e56cf", "#b7791f", "#db2777", "#0891b2", "#65a30d", "#9333ea", "#e11d48"];
function renderAllocation() {
  const box = $("#allocDonut");
  if (!box || !STATE) return;
  const pf = STATE.portfolio;
  const tv = pf.total_value || 0;
  const holds = (pf.holdings || []).slice().sort((a, b) => (b.market_value || 0) - (a.market_value || 0));
  if (!holds.length || tv <= 0) { box.innerHTML = `<p class="muted alloc-empty">No allocation to show yet.</p>`; return; }

  const slices = holds.map((h, i) => ({ ticker: h.ticker, val: h.market_value || 0, pct: h.weight || 0, color: ALLOC_COLORS[i % ALLOC_COLORS.length], click: true }));
  // Cash is the residual of equity not in positions (not buying power, which can
  // include margin) — so holdings + cash always sum to exactly 100% of the ring.
  const investedVal = slices.reduce((s, x) => s + x.val, 0);
  const cashVal = Math.max(0, tv - investedVal);
  if (cashVal / tv > 0.005) slices.push({ ticker: "Cash", val: cashVal, pct: cashVal / tv * 100, color: "#cbd5e1", click: false });

  // Geometry is normalized to the slice total so the ring always closes cleanly,
  // even if reported equity differs slightly from summed positions + cash.
  const total = slices.reduce((s, x) => s + x.val, 0) || 1;
  const R = 52, C = 2 * Math.PI * R, BASE = 17;
  const sel = SELECTED_CHART;
  let acc = 0;
  const segs = slices.map((s) => {
    const frac = s.val / total;
    const dash = `${(frac * C).toFixed(2)} ${C.toFixed(2)}`;
    const off = `${(-acc * C).toFixed(2)}`;
    acc += frac;
    const dim = (sel !== "portfolio" && sel !== s.ticker) ? "opacity:.25;" : "";
    const sw = sel === s.ticker ? BASE + 4 : BASE;
    const handlers = s.click ? `onclick="selectPortfolioChart('${s.ticker}')" style="cursor:pointer;${dim}"` : `style="${dim}"`;
    return `<circle cx="60" cy="60" r="${R}" fill="none" stroke="${s.color}" stroke-width="${sw}" stroke-dasharray="${dash}" stroke-dashoffset="${off}" ${handlers}><title>${esc(s.ticker)} · ${s.pct.toFixed(1)}%</title></circle>`;
  }).join("");

  const focus = sel !== "portfolio" ? slices.find((s) => s.ticker === sel) : null;
  const cTop = focus ? `${focus.pct.toFixed(1)}%` : `${holds.length}`;
  const cBot = focus ? esc(focus.ticker) : holds.length === 1 ? "position" : "positions";

  const legend = slices.map((s) => `
    <div class="lg-row ${sel === s.ticker ? "on" : ""} ${s.click ? "" : "static"}" ${s.click ? `onclick="selectPortfolioChart('${s.ticker}')"` : ""}>
      <span class="lg-dot" style="background:${s.color}"></span>
      <span class="lg-tk">${esc(s.ticker)}</span>
      <span class="lg-pct">${s.pct.toFixed(1)}%</span>
    </div>`).join("");

  box.innerHTML = `
    <div class="donut">
      <svg viewBox="0 0 120 120" aria-label="allocation by holding"><g transform="rotate(-90 60 60)">${segs}</g></svg>
      <div class="donut-c"><strong>${cTop}</strong><span>${cBot}</span></div>
    </div>
    <div class="alloc-legend">${legend}</div>`;
}
window.renderAllocation = renderAllocation;

function svgPath(points, w = 760, h = 220, pad = 10) {
  const pts = (points || []).map((p) => p.close).filter((n) => Number.isFinite(n));
  if (pts.length < 2) return { path: "", min: 0, max: 0, coords: [] };
  const min = Math.min(...pts), max = Math.max(...pts), range = max - min || 1;
  const coords = pts.map((v, i) => {
    const x = pad + (i / (pts.length - 1)) * (w - pad * 2);
    const y = h - pad - ((v - min) / range) * (h - pad * 2);
    return { x, y, close: v, at: points[i]?.at || "" };
  });
  const path = coords.map((p, i) => `${i ? "L" : "M"}${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(" ");
  return { path, min, max, coords };
}

function chartTime(s) {
  if (!s) return "";
  const d = new Date(s);
  if (Number.isNaN(d.getTime())) return s;
  const sameYear = d.getFullYear() === new Date().getFullYear();
  return d.toLocaleString([], sameYear
    ? { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }
    : { year: "numeric", month: "short", day: "numeric" });
}

// ---------- Autopilot (the autonomous paper fund that races your real book) ----------
let AUTOPILOT = null;

async function loadAutopilot(refresh) {
  const box = $("#apBody");
  if (box && !AUTOPILOT) box.innerHTML = `<div class="loading"><span class="spin"></span> Loading Autopilot…</div>`;
  try { AUTOPILOT = await api("twin" + (refresh ? "?refresh=1" : "")); }
  catch (e) { if (box) box.innerHTML = `<p class="muted">Could not load Autopilot.</p>`; return; }
  renderAutopilot();
}

async function startAutopilot(btn) {
  busy(btn, true);
  try {
    await api("twin/start", {});
    toast("Autopilot launched — it just cloned your book");
    await loadAutopilot(true);
  } catch (e) { toast("Could not start Autopilot"); busy(btn, false); }
}
window.startAutopilot = startAutopilot;

function renderAutopilot() {
  const box = $("#apBody");
  if (!box) return;
  const c = AUTOPILOT;
  box.innerHTML = (!c || !c.started)
    ? apEmptyHTML()
    : apHeroHTML(c) + apStatusHTML(c) + apDecisionTraceHTML(c) + apLessonsHTML(c) + apControlsHTML(c) + apChartHTML(c) + apHoldingsHTML(c) + apHistoryHTML(c);
}

function apControlsHTML(c) {
  const pending = c?.pending || {};
  const market = c?.market || {};
  const queued = pending.count ? `${pending.count} queued for ${market.is_open ? "this session" : "next open"}` : "no queued orders";
  const session = market.is_open ? "Market open" : `Market closed${market.next_open ? ` · next open ${chartTime(market.next_open)}` : ""}`;
  return `<div class="ap-controls">
    <span class="ap-cadence"><strong>${esc(session)}</strong> · ${esc(queued)} · thinks every few hours, fills only during market hours.</span>
    <button class="ap-cycle" onclick="runAutopilotCycle(this)">Run a cycle now</button>
    <button class="ap-reset" onclick="resetAutopilot(this)">Reset</button>
  </div>`;
}

async function resetAutopilot(btn) {
  if (!confirm("Reset Autopilot? This wipes its fund, positions, trades, and history so you can start fresh from your current book. Your real account is untouched.")) return;
  busy(btn, true);
  try {
    AUTOPILOT = await api("twin/reset", {});
    toast("Autopilot reset — start it again to re-clone fresh");
    renderAutopilot();
  } catch (e) { toast("Could not reset"); busy(btn, false); }
}
window.resetAutopilot = resetAutopilot;

async function runAutopilotCycle(btn) {
  busy(btn, true);
  const hadPending = (AUTOPILOT?.pending?.count || 0) > 0;
  try {
    AUTOPILOT = await api("twin/decide", {});   // forces a decision; may take a moment (one model call)
    const pending = AUTOPILOT?.pending?.count || 0;
    if (hadPending && pending) toast("Orders already queued for next open");
    else if (pending) toast(`Autopilot queued ${pending} order${pending === 1 ? "" : "s"}`);
    else toast("Autopilot held this cycle");
    renderAutopilot();
  } catch (e) { toast("Could not run a cycle"); busy(btn, false); }
}
window.runAutopilotCycle = runAutopilotCycle;

function apEmptyHTML() {
  const noPlan = !(MANDATE && MANDATE.statement);
  return `<div class="ap-empty"><div class="ap-empty-card">
    <h3>Autopilot hasn't launched yet</h3>
    <p>It clones your account exactly — same cash, same stocks, same dollar value — then trades itself to pursue your plan. Your real account stays untouched; this one races it, using only the money it starts with.</p>
    ${noPlan ? `<p class="ap-warn">Set your plan on Home first, so Autopilot knows what it's chasing.</p>` : ""}
    <button class="ap-start" onclick="startAutopilot(this)">Start Autopilot</button>
    <p class="ap-note">Starts once. From the moment you launch it, it diverges from you — there's no re-syncing.</p>
  </div></div>`;
}

function apHeroHTML(c) {
  const edge = c.edge_pct || 0, ahead = edge >= 0;
  const edgeTxt = Math.abs(edge) < 0.05 ? "Dead even with you"
    : ahead ? `Autopilot is ${pct(edge).replace("+", "")} ahead of you`
            : `You're ahead by ${pct(-edge).replace("+", "")}`;
  const side = (label, v, cls) => `<div class="ap-side ${cls}">
    <span class="ap-side-lbl">${label}</span>
    <div class="ap-side-val">${money0(v.value)}</div>
    <div class="ap-side-ret ${(v.return_pct || 0) >= 0 ? "pos" : "neg"}">${pct(v.return_pct)}</div>
  </div>`;
  return `<div class="ap-hero">
    ${side("You", c.real, "you")}
    <div class="ap-vs">
      <div class="ap-edge ${ahead ? "ahead" : "behind"}">${esc(edgeTxt)}</div>
      <span class="ap-since">since ${esc(chartTime(c.inception_at))}</span>
    </div>
    ${side("Autopilot", c.twin, "twin")}
  </div>`;
}

function apStatusHTML(c) {
  const pending = c.pending || {};
  const gross = pending.gross_value || 0;
  const buys = pending.buy_value || 0;
  const sells = pending.sell_value || 0;
  const pendingLine = pending.count
    ? `buys ${money0(buys)} / sells ${money0(sells)}`
    : "No queued orders";
  return `<div class="ap-status">
    <div class="ap-stat">
      <span>Starting value</span>
      <strong>${money0(c.inception_value)}</strong>
      <em>clone baseline</em>
    </div>
    <div class="ap-stat">
      <span>Current marked value</span>
      <strong>${money0(c.twin.value)}</strong>
      <em>latest quotes, not trade fills</em>
    </div>
    <div class="ap-stat ${pending.count ? "queued" : ""}">
      <span>Queued orders</span>
      <strong>${pending.count ? `${pending.count} pending` : "None"}</strong>
      <em>${esc(pendingLine)}${gross ? ` · ${money0(gross)} total` : ""} · not applied yet</em>
    </div>
  </div>`;
}

function apMoveTraceRow(m, opts = {}) {
  const action = (m.action || "").toUpperCase();
  const tk = m.ticker || "";
  const usd = +m.usd || +m.value || 0;
  const tactic = m.tactic ? `<span>${esc((m.tactic || "").replaceAll("_", " "))}</span>` : "";
  const step = m.plan_step ? `<em>step ${esc(m.plan_step)}</em>` : "";
  const deps = (m.depends_on || []).length ? `<span>depends on ${esc(m.depends_on.join(", "))}</span>` : "";
  const why = m.reasoning || m.thesis || m.reason || "";
  return `<div class="ap-trace-move ${opts.rejected ? "rejected" : ""}">
    <div><b>${esc(action)}</b><strong>${esc(tk)}</strong><small>${money0(usd)}</small></div>
    <div class="ap-trace-tags">${step}${tactic}${deps}</div>
    ${why ? `<p>${esc(why)}</p>` : ""}
  </div>`;
}

function apDecisionTraceHTML(c) {
  const run = c.decision_trace;
  if (!run) return "";
  const steps = run.steps || [];
  const legacy = steps.find((s) => s.type === "twin_decision") || {};
  const ctx = steps.find((s) => s.type === "decision_context") || {};
  const draft = steps.find((s) => s.type === "model_draft") || {};
  const gov = steps.find((s) => s.type === "governor_review") || {};
  const draftMoves = draft.moves || legacy.original_moves || [];
  const finalMoves = gov.ordered_plan || legacy.moves || [];
  const rejected = gov.rejected || legacy.rejected || [];
  const notes = gov.critic_notes || legacy.critic_notes || {};
  const noteRows = Object.values(notes).filter(Boolean).map((n) => `<li>${esc(n)}</li>`).join("");
  const rejectedRows = rejected.map((r) => apMoveTraceRow(r, { rejected: true })).join("");
  const finalRows = finalMoves.map((m) => apMoveTraceRow(m)).join("");
  const draftRows = draftMoves.map((m) => apMoveTraceRow(m)).join("");
  const sample = (ctx.candidate_sample || []).slice(0, 8)
    .map((x) => `<span>${esc(x.ticker || "")}</span>`).join("");
  const book = ctx.book || {};
  const bookValue = Number.isFinite(+book.value) ? money0(+book.value) : "—";
  const bookCash = Number.isFinite(+book.cash) ? money0(+book.cash) : "—";
  const summary = gov.final_summary || legacy.summary || run.answer || "Latest Autopilot decision cycle";
  return `<details class="ap-trace">
    <summary>
      <div>
        <strong>Latest decision trace</strong>
        <span>${esc(summary)}</span>
      </div>
      <em>${esc(chartTime(run.created_at))}</em>
    </summary>
    <div class="ap-trace-body">
      <div class="ap-trace-context">
        <div><span>Book</span><strong>${bookValue}</strong><em>cash ${bookCash}</em></div>
        <div><span>Candidates</span><strong>${esc(ctx.candidate_count ?? "—")}</strong><em>${esc(ctx.market_regime || "regime unknown")}</em></div>
        <div><span>Model</span><strong>${esc(run.model || "LLM")}</strong><em>structured decision artifact</em></div>
      </div>
      ${sample ? `<div class="ap-trace-sample">${sample}</div>` : ""}
      <div class="ap-trace-grid">
        <section>
          <h4>Model draft</h4>
          ${draftRows || `<p class="muted">No draft moves captured.</p>`}
        </section>
        <section>
          <h4>Governor</h4>
          ${noteRows ? `<ul class="ap-trace-notes">${noteRows}</ul>` : `<p class="muted">No critic edits.</p>`}
          ${rejectedRows ? `<div class="ap-trace-rejected">${rejectedRows}</div>` : ""}
        </section>
      </div>
      <section class="ap-trace-final">
        <h4>Final ordered plan</h4>
        ${finalRows || `<p class="muted">Held this cycle — no orders queued.</p>`}
      </section>
    </div>
  </details>`;
}

function lessonStatus(row) {
  const n = row.count || 0;
  if (!n) return "learning";
  if ((row.break_rate || 0) >= 34) return "fragile";
  if ((row.avg_sector_alpha || 0) > 1 && (row.win_rate || 0) >= 50) return "working";
  if ((row.avg_sector_alpha || 0) < -1) return "lagging";
  return "mixed";
}

function apLessonsHTML(c) {
  const l = c.lessons || {};
  const tactics = l.tactics || [];
  const sectors = l.sectors || [];
  const themes = l.themes || [];
  const strategies = l.strategies || [];
  const recent = l.recent || [];
  const rules = l.rules || [];
  const bandit = l.bandit || {};
  const tacticRows = tactics.slice(0, 4).map((t) => {
    const st = lessonStatus(t);
    return `<div class="ap-lesson-row">
      <div>
        <strong>${esc((t.key || "trade").replaceAll("_", " "))}</strong>
        <span>${esc(t.count || 0)} judged · ${esc(st)}</span>
      </div>
      <div class="ap-lesson-metrics">
        <b class="${(t.avg_sector_alpha || 0) >= 0 ? "pos" : "neg"}">${pct(t.avg_sector_alpha || 0)}</b>
        <em>sector alpha</em>
      </div>
      <div class="ap-lesson-metrics">
        <b>${Math.round(t.win_rate || 0)}%</b>
        <em>worked</em>
      </div>
    </div>`;
  }).join("");
  const sectorRows = sectors.slice(0, 4).map((s) => `<div class="ap-sector-chip">
    <strong>${esc(s.key || "market")}</strong>
    <span>${esc((s.best_tactic || "unclear").replaceAll("_", " "))}</span>
    <em class="${(s.avg_sector_alpha || 0) >= 0 ? "pos" : "neg"}">${pct(s.avg_sector_alpha || 0)} alpha</em>
  </div>`).join("");
  const themeRows = themes.slice(0, 4).map((t) => {
    const names = (t.candidates || []).slice(0, 4).map((c) => c.ticker).filter(Boolean).join(", ");
    const tested = t.tested_count ? `${t.tested_count} tested · ${pct(t.avg_sector_alpha || 0)} alpha · ${esc(t.stance || "testing")}` : "not tested yet";
    return `<div class="ap-theme-scout">
      <div><strong>${esc(t.name || t.key || "theme")}</strong><span>${esc(names || "building roster")} · ${tested}</span></div>
      <b>${Math.round(t.score || 0)}</b>
    </div>`;
  }).join("");
  const strategyRows = strategies.slice(0, 4).map((s) => {
    const names = (s.candidates || []).slice(0, 4).map((c) => c.ticker).filter(Boolean).join(", ");
    const tested = s.tested_count ? `${s.tested_count} tested · ${pct(s.avg_sector_alpha || 0)} alpha · ${esc(s.stance || "testing")}` : "not tested yet";
    return `<div class="ap-strategy-exp">
      <div><strong>${esc(s.title || s.key || "strategy")}</strong><span>${esc(s.tactic || "tactic")} · ${esc(names || "building roster")} · ${tested}</span></div>
      <b>${Math.round(s.score || 0)}</b>
    </div>`;
  }).join("");
  const ruleRows = rules.slice(0, 4).map((r) => `<li>${esc(r)}</li>`).join("");
  const banditRows = [...(bandit.top || []).slice(0, 2), ...(bandit.bottom || []).slice(0, 2)]
    .map((a) => `<div class="ap-policy-arm ${a.avg_reward >= 0 ? "good" : "bad"}">
      <strong>${esc(a.label || a.key || "context")}</strong>
      <span>${esc((a.stance || "learning").replaceAll("_", " "))} · ${esc(a.count || 0)} judged · ${Math.round((a.confidence || 0) * 100)}% confidence</span>
      <em>${(a.avg_reward || 0) >= 0 ? "+" : ""}${(a.avg_reward || 0).toFixed(1)} reward</em>
    </div>`).join("");
  const recentRows = recent.slice(0, 3).map((r) => `<div class="ap-review-mini ${esc(r.verdict || "")}">
    <strong>${esc(r.ticker || "")} ${esc(r.window || "")}</strong>
    <span>${esc(r.verdict || "reviewed")} · thesis ${esc(r.thesis_state || "unknown")}</span>
    <em>${pct(r.sector_alpha_pct || 0)} sector alpha</em>
  </div>`).join("");
  const empty = !tactics.length && !sectors.length && !recent.length;
  return `<section class="ap-lessons">
    <div class="ap-sec-head">
      <div><h3>Lessons</h3><p>What Autopilot has learned from judged review windows.</p></div>
      <span>${empty ? "collecting evidence" : `${recent.length} recent reviews`}</span>
    </div>
    ${empty ? `<div class="ap-lessons-empty">No judged windows yet. As filled trades age into their review windows, this becomes the policy memory Autopilot uses before making new moves.</div>` : `
      <div class="ap-lessons-grid">
        <div class="ap-lesson-card wide">
          <h4>Tactics</h4>
          ${tacticRows || `<p class="muted">No tactic has enough judged reviews yet.</p>`}
        </div>
        <div class="ap-lesson-card">
          <h4>Sector read</h4>
          <div class="ap-sector-list">${sectorRows || `<p class="muted">Sector lessons need more judged trades.</p>`}</div>
        </div>
      </div>
      <div class="ap-lessons-grid second">
        <div class="ap-lesson-card">
          <h4>Policy forming</h4>
          <ul class="ap-rule-list">${ruleRows}</ul>
          ${banditRows ? `<div class="ap-policy-list">${banditRows}</div>` : ""}
        </div>
        <div class="ap-lesson-card">
          <h4>Strategy experiments</h4>
          ${strategyRows || `<p class="muted">Strategy Discovery has not promoted an experiment yet.</p>`}
        </div>
      </div>
      <div class="ap-lessons-grid second">
        <div class="ap-lesson-card">
          <h4>Discovered themes</h4>
          ${themeRows || `<p class="muted">Theme Scout has not found a strong autonomous theme yet.</p>`}
        </div>
        <div class="ap-lesson-card ap-review-card">
        <h4>Latest reviews</h4>
        ${recentRows || `<p class="muted">No completed reviews yet.</p>`}
        </div>
      </div>`}
  </section>`;
}

function dualLine(you, twin, w = 760, h = 180, pad = 12) {
  const norm = (s) => (s || []).map((p) => ({ t: new Date(p.at).getTime(), v: +p.value }))
    .filter((p) => Number.isFinite(p.t) && Number.isFinite(p.v));
  const ys = norm(you), ts = norm(twin), all = [...ys, ...ts];
  if (all.length < 2) return { empty: true };
  const tA = all.map((p) => p.t), vA = all.map((p) => p.v);
  const t0 = Math.min(...tA), tr = (Math.max(...tA) - t0) || 1;
  const v0 = Math.min(...vA), vr = (Math.max(...vA) - v0) || 1;
  const draw = (s) => s.length < 2 ? "" : s.map((p, i) => {
    const x = pad + ((p.t - t0) / tr) * (w - pad * 2);
    const y = h - pad - ((p.v - v0) / vr) * (h - pad * 2);
    return `${i ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(" ");
  return { empty: false, you: draw(ys), twin: draw(ts) };
}

function apChartHTML(c) {
  const d = dualLine(c.real_equity_curve, c.equity_curve);
  if (d.empty) return `<div class="ap-chart-empty">The race line builds as Autopilot runs — check back after it's been live a while.</div>`;
  const w = 760, h = 180;
  return `<div class="ap-chart">
    <svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">
      ${d.you ? `<path d="${d.you}" fill="none" stroke="var(--faint)" stroke-width="2" vector-effect="non-scaling-stroke"/>` : ""}
      ${d.twin ? `<path d="${d.twin}" fill="none" stroke="var(--brain)" stroke-width="2.5" vector-effect="non-scaling-stroke"/>` : ""}
    </svg>
    <div class="ap-legend"><span class="lg you">You</span><span class="lg twin">Autopilot</span></div>
  </div>`;
}

function apHoldingsHTML(c) {
  const yourTk = new Set((c.real.holdings || []).map((h) => h.ticker));
  const apTk = new Set((c.twin.positions || []).map((p) => p.ticker));
  const yourRows = (c.real.holdings || []).map((h) => `<div class="ap-hrow">
    <span class="ap-h-tk">${esc(h.ticker)}</span>
    <span class="ap-h-w">${(h.weight || 0).toFixed(0)}%</span>
    <span class="ap-h-v">${money0(h.market_value)}</span></div>`).join("") || `<p class="muted">No holdings.</p>`;
  const apRows = (c.twin.positions || []).map((p) => {
    const h = p.health || {};
    const hk = (h.state || "none").toLowerCase();
    return `<div class="ap-hrow ${h.label ? "has-health" : ""}">
    <span class="ap-h-tk">${esc(p.ticker)}${!yourTk.has(p.ticker) ? ` <span class="ap-tag">added</span>` : ""}</span>
    <span class="ap-h-w ${(p.return_pct || 0) >= 0 ? "pos" : "neg"}">${pct(p.return_pct)}</span>
    <span class="ap-h-v">${money0(p.market_value)}</span>
    ${h.label ? `<span class="ap-health ${esc(hk)}" title="${esc(h.note || "")}">${esc(h.label)}</span>` : ""}
    </div>`;
  }).join("");
  const cashRow = `<div class="ap-hrow cash"><span class="ap-h-tk">Cash</span><span class="ap-h-w"></span><span class="ap-h-v">${money0(c.twin.cash)}</span></div>`;
  const added = [...apTk].filter((t) => !yourTk.has(t));
  const sold = [...yourTk].filter((t) => !apTk.has(t));
  let diff = "";
  if (added.length || sold.length) {
    const parts = [];
    if (added.length) parts.push(`bought ${added.join(", ")}`);
    if (sold.length) parts.push(`exited ${sold.join(", ")}`);
    diff = `<p class="ap-diff">Diverged from you: ${esc(parts.join(" · "))}.</p>`;
  }
  return `<div class="ap-holdings-wrap">${diff}<div class="ap-holdings">
    <div class="ap-col"><span class="ap-col-h">Your book</span>${yourRows}</div>
    <div class="ap-col"><span class="ap-col-h">Autopilot's book</span>${apRows}${cashRow}</div>
  </div></div>`;
}

function apHistoryHTML(c) {
  const trades = c.trades || [];
  if (!trades.length) {
    return `<div class="ap-history"><h3>History</h3>
      <p class="ap-hist-empty">No moves yet — Autopilot is holding the cloned book. Every buy and sell it makes, with the exact time and its reasoning, lands here once its decision engine is switched on.</p></div>`;
  }
  let html = `<div class="ap-history"><h3>History</h3><p class="ap-hist-sub">Every move Autopilot has made, newest first.</p>`;
  let lastDay = null;
  for (const t of trades) {
    const stamp = t.filled_at || t.decided_at;
    const day = actDay(stamp);
    if (day !== lastDay) { html += `<div class="ap-day">${esc(day)}</div>`; lastDay = day; }
    const act = (t.action || "").toLowerCase();
    const sh = (+t.shares || 0).toLocaleString(undefined, { maximumFractionDigits: 2 });
    const detail = t.status === "filled"
      ? `${sh} sh @ ${money(t.price)} · ${money0(t.value)}`
      : t.status === "canceled"
        ? `~${money0(t.value || 0)} canceled`
        : `~${money0(t.value || 0)} queued`;
    const grade = t.judgement ? ` <span class="ap-grade">judge ${esc(t.judgement.verdict)} ${t.judgement.score}</span>` : "";
    const tags = [t.tactic, t.horizon].filter(Boolean).map((x) => `<span class="ap-meta">${esc(x)}</span>`).join("");
    const deps = (t.depends_on || []).length ? `<span class="ap-meta">depends: ${esc(t.depends_on.join(", "))}</span>` : "";
    const step = t.plan_step ? `<span class="ap-step">step ${esc(t.plan_step)}</span>` : "";
    const preflightTag = preflightBadge(t.preflight_note || "");
    const critic = t.critic_note ? `<p class="ap-critic">${esc(t.critic_note)}</p>` : "";
    const preflight = t.preflight_note ? `<p class="ap-preflight">${esc(t.preflight_note)}</p>` : "";
    const review = t.review_note ? `<p class="ap-review">${esc(t.review_note)}</p>` : "";
    html += `<div class="ap-trade">
      <span class="ap-t-time" title="${esc(stamp)}">${esc(actTime(stamp))}</span>
      <span class="ap-act ${act}">${esc((t.action || "").toUpperCase())}</span>
      <span class="ap-t-tk">${esc(t.ticker)}</span>
      <span class="ap-t-detail">${esc(detail)}</span>
      <span class="ap-t-status ${esc(t.status)}">${esc(t.status)}</span>
      ${(tags || deps || step || preflightTag) ? `<div class="ap-t-tags">${step}${tags}${deps}${preflightTag}</div>` : ""}
      ${critic}
      ${preflight}
      ${t.reasoning ? `<p class="ap-t-why">${esc(t.reasoning)}${grade}</p>` : (grade ? `<p class="ap-t-why">${grade}</p>` : "")}
      ${review}
    </div>`;
  }
  return html + `</div>`;
}

function preflightBadge(note) {
  const n = (note || "").toLowerCase();
  if (!n) return "";
  if (n.includes("resized")) return `<span class="ap-preflight-badge resized">resized: funding changed</span>`;
  if (n.includes("thesis-break")) return `<span class="ap-preflight-badge canceled">canceled: thesis broke</span>`;
  if (n.includes("not chasing")) return `<span class="ap-preflight-badge canceled">canceled: chased too far</span>`;
  if (n.includes("gap-down")) return `<span class="ap-preflight-badge canceled">canceled: gap down</span>`;
  if (n.includes("funding")) return `<span class="ap-preflight-badge canceled">canceled: funding missing</span>`;
  return `<span class="ap-preflight-badge">preflight checked</span>`;
}

const _apRefresh = $("#apRefresh");
if (_apRefresh) _apRefresh.addEventListener("click", () => loadAutopilot(true));

function chartBlock(chart, opts = {}) {
  const w = opts.width || 760, h = opts.height || 220;
  const { path, min, max, coords } = svgPath(chart.points || [], w, h);
  if (!path) return `<div class="empty-chart">No chart data available for ${esc(chart.ticker || "this asset")}.</div>`;
  const up = (chart.return_pct || 0) >= 0;
  const latest = chart.latest || 0;
  const first = (chart.points || [])[0]?.close || 0;
  const range = max - min || 1;
  const baseY = first ? (h - 10 - ((first - min) / range) * (h - 20)).toFixed(1) : (h / 2).toFixed(1);
  const id = "chart_" + Math.random().toString(36).slice(2);
  CHART_STORE[id] = { chart, coords, w, h, first };
  return `<div class="chart-frame" data-chart-id="${id}">
    <div class="chart-price">
      <strong class="cp-val ${up ? "pos" : "neg"}">${money(latest)}</strong>
      <span class="cp-chg ${up ? "pos" : "neg"}"><span class="cp-move">${up ? "▲" : "▼"} ${money(Math.abs(latest - first))} (${Math.abs(chart.return_pct || 0).toFixed(2)}%)</span><em class="cp-time">${chartTime((chart.points || []).at(-1)?.at)}</em></span>
    </div>
    <svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" aria-label="${esc(chart.ticker)} chart">
      <path d="M10,${baseY} L${w - 10},${baseY}" class="baseline"/>
      <path d="${path}" fill="none" stroke="${up ? "#10a348" : "#d9432f"}" stroke-width="${opts.stroke || 3}" vector-effect="non-scaling-stroke"/>
      <line class="crosshair-x hidden" y1="0" y2="${h}"></line>
      <circle class="crosshair-dot hidden" r="4"></circle>
      <rect class="chart-hit" x="0" y="0" width="${w}" height="${h}"></rect>
    </svg>
    <div class="chart-tooltip hidden"></div>
    <div class="sub2">${esc(chart.source || "chart")}</div>
  </div>`;
}

// One place that fills the hero price block — used by the initial paint's
// counterparts (hover/leave) so the value, arrow, colored change and time always
// move together and never disagree.
function setHeroPrice(frame, value, delta, ret, timeStr) {
  const k = ret >= 0 ? "pos" : "neg";
  const val = frame.querySelector(".cp-val");
  const chg = frame.querySelector(".cp-chg");
  if (!val || !chg) return;
  val.textContent = money(value); val.className = "cp-val " + k;
  chg.className = "cp-chg " + k;
  chg.querySelector(".cp-move").textContent = `${ret >= 0 ? "▲" : "▼"} ${money(Math.abs(delta))} (${Math.abs(ret).toFixed(2)}%)`;
  chg.querySelector(".cp-time").textContent = timeStr || "";
}

function chartHover(event, svg) {
  const frame = svg.closest(".chart-frame");
  const stored = CHART_STORE[frame?.dataset.chartId];
  if (!stored || !stored.coords.length) return;
  const box = svg.getBoundingClientRect();
  const x = ((event.clientX - box.left) / box.width) * stored.w;
  let best = stored.coords[0];
  for (const p of stored.coords) if (Math.abs(p.x - x) < Math.abs(best.x - x)) best = p;
  const line = svg.querySelector(".crosshair-x");
  const dot = svg.querySelector(".crosshair-dot");
  line.setAttribute("x1", best.x); line.setAttribute("x2", best.x);
  dot.setAttribute("cx", best.x); dot.setAttribute("cy", best.y);
  line.classList.remove("hidden"); dot.classList.remove("hidden");
  const ret = stored.first ? ((best.close - stored.first) / stored.first * 100) : 0;
  setHeroPrice(frame, best.close, best.close - stored.first, ret, chartTime(best.at));
  const tip = frame.querySelector(".chart-tooltip");
  tip.innerHTML = `<strong>${money(best.close)}</strong><span>${chartTime(best.at)}</span>`;
  tip.style.left = `${Math.min(Math.max((best.x / stored.w) * 100, 6), 94)}%`;
  tip.style.top = `${Math.max(best.y - 34, 12)}px`;
  tip.classList.remove("hidden");
}
function chartLeave(svg) {
  const frame = svg.closest(".chart-frame");
  const stored = CHART_STORE[frame?.dataset.chartId];
  if (!stored) return;
  svg.querySelector(".crosshair-x")?.classList.add("hidden");
  svg.querySelector(".crosshair-dot")?.classList.add("hidden");
  frame.querySelector(".chart-tooltip")?.classList.add("hidden");
  const chart = stored.chart;
  setHeroPrice(frame, chart.latest || 0, (chart.latest || 0) - stored.first,
    chart.return_pct || 0, chartTime((chart.points || []).at(-1)?.at));
}

function bindChartInteractions() {
  const svg = $("#portfolioChart svg");
  if (!svg) return;
  const move = (event) => chartHover(event.touches ? event.touches[0] : event, svg);
  svg.addEventListener("mousemove", move);
  svg.addEventListener("touchmove", (event) => { event.preventDefault(); move(event); }, { passive: false });
  svg.addEventListener("mouseleave", () => chartLeave(svg));
  svg.addEventListener("touchend", () => chartLeave(svg));
}

async function loadPortfolioChart({ force = false } = {}) {
  const box = $("#portfolioChart");
  if (!box || !STATE) return;
  const ticker = SELECTED_CHART || "portfolio";
  const label = ticker === "portfolio" ? "Portfolio" : ticker;
  const cacheKey = `${ticker}:${CHART_SPAN}`;
  $("#chartTitle").textContent = label;
  $("#resetChart").classList.toggle("hidden", ticker === "portfolio");
  document.querySelector(".portfolio-chart")?.classList.toggle("linked", ticker !== "portfolio");

  // Fresh body cached → paint it (with a live tip from current STATE) and stop.
  const cached = force ? null : cachedChart(cacheKey);
  if (cached) {
    paintChart(box, cached, ticker);
    prefetchChartSet(ticker);
    return;
  }

  // Body needs (re)fetching. If a chart is already on screen, keep it and just
  // refresh its tip immediately so the line stays live while the body loads;
  // otherwise show a spinner for the first paint.
  const haveDrawn = box.querySelector("svg") && CHART_RAW[cacheKey];
  if (haveDrawn) {
    paintChart(box, CHART_RAW[cacheKey], ticker);
    box.classList.add("refreshing");
  } else {
    $("#chartMeta").textContent = "Loading chart...";
    box.innerHTML = `<span class="spin"></span>`;
    box.classList.add("loading");
  }

  try {
    const r = await fetch(`/api/chart/${encodeURIComponent(ticker)}?span=${encodeURIComponent(CHART_SPAN)}${force ? "&refresh=true" : ""}`);
    const chart = await r.json();
    if (!chart || !Array.isArray(chart.points) || !chart.points.length) throw new Error("empty chart");
    setCachedChart(cacheKey, CHART_SPAN, chart);
    paintChart(box, chart, ticker);
    prefetchChartSet(ticker);
  } catch (e) {
    box.classList.remove("loading", "refreshing");
    // Never wipe a working chart over a transient fetch hiccup — the live tip
    // already kept it current. Only show an error if we have nothing to show.
    if (!box.querySelector("svg")) {
      $("#chartMeta").textContent = "Chart failed to load";
      box.innerHTML = `<div class="empty-chart">Chart request failed.</div>`;
    }
  }
}

function prefetchChartSet(ticker) {
  ["1d", "1w", "1m", "3m", "6m", "1y"].forEach((span) => {
    const key = `${ticker}:${span}`;
    if (span === CHART_SPAN || cachedChart(key) || PREFETCHING.has(key)) return;
    PREFETCHING.add(key);
    fetch(`/api/chart/${encodeURIComponent(ticker)}?span=${encodeURIComponent(span)}`)
      .then((r) => r.json())
      .then((chart) => { if (chart && chart.points) setCachedChart(key, span, chart); })
      .catch(() => {})
      .finally(() => PREFETCHING.delete(key));
  });
}

function selectPortfolioChart(ticker) {
  const next = ticker || "portfolio";
  // Clicking the already-selected holding again jumps back to the full portfolio.
  SELECTED_CHART = (next !== "portfolio" && SELECTED_CHART === next) ? "portfolio" : next;
  renderHoldings();
  renderAllocation();
  loadPortfolioChart();
}
window.selectPortfolioChart = selectPortfolioChart;

$("#chartTitle").onclick = () => selectPortfolioChart("portfolio");
$("#resetChart").onclick = () => selectPortfolioChart("portfolio");
$$('#chartSpans button').forEach((btn) => btn.addEventListener("click", () => {
  $$("#chartSpans button").forEach((x) => x.classList.remove("active"));
  btn.classList.add("active");
  CHART_SPAN = btn.dataset.span;
  loadPortfolioChart();
}));

// ---------- Research Memory tab (Dossier: master / detail) ----------
let MEM_SELECTED = null;

function memItems() {
  const research = STATE.research || {};
  const theses = research.theses || {};
  return (research.watchlist || []).slice().reverse().map((x) => ({
    ...x,
    mode: x.mode || "balanced",
    th: theses[x.ticker] || {},
  }));
}

function callShort(it) {
  return esc(((it.th.last_decision || "watchlist") + "").split(" ")[0].toLowerCase());
}

// The shared "case file" — used by Dossier (right pane) and Ledger (inline).
function memDetail(it) {
  const th = it.th;
  // Thesis-first: the durable belief (Thesis record) is the headline; the
  // watchlist reason is shown separately as the forward "tracking" trigger.
  const thesis = esc(th.thesis || it.reason || "No thesis stored yet.");
  const track = it.reason && it.reason !== th.thesis ? esc(it.reason) : "";
  const breaks = th.invalidation ? esc(th.invalidation) : "";
  const sup = (th.strengthens || []).map((s) => `<li>${esc(s)}</li>`).join("");
  const wk = (th.weakens || []).map((s) => `<li>${esc(s)}</li>`).join("");
  const stat = (th.status || "active").toLowerCase();
  const meta = [
    ["Conviction", it.mode],
    ["Last call", (th.last_decision || "WATCHLIST").toLowerCase()],
    it.max_allocation_pct ? ["Max size", it.max_allocation_pct + "%"] : null,
    it.added_at ? ["First tracked", (it.added_at || "").slice(0, 10)] : null,
  ].filter(Boolean);
  return `
    <div class="dos-h">
      <div class="dos-h-l">
        <span class="wl-dot ${it.mode}"></span>
        <h3>${esc(it.ticker)}</h3>
        <span class="dos-status ${stat}">${esc(stat)}</span>
      </div>
      <button class="dos-refresh" onclick="event.stopPropagation(); refreshThesis('${it.ticker}', this)" title="Re-run the analyst on ${esc(it.ticker)}">↻ refresh thesis</button>
    </div>
    <p class="dos-thesis">${thesis}</p>
    ${track ? `<div class="dos-block"><span class="dos-lbl">Tracking for</span><p>${track}</p></div>` : ""}
    ${breaks ? `<div class="dos-block warn"><span class="dos-lbl">Breaks if</span><p>${breaks}</p></div>` : ""}
    ${(sup || wk) ? `<div class="dos-cols">
      ${sup ? `<div><span class="dos-lbl up">Supports</span><ul>${sup}</ul></div>` : ""}
      ${wk ? `<div><span class="dos-lbl down">Pressures</span><ul>${wk}</ul></div>` : ""}
    </div>` : ""}
    <div class="dos-target">
      <span class="dos-lbl">Alert me under</span>
      <input class="dos-target-in" type="number" step="0.01" min="0" placeholder="price" value="${it.target_entry || ""}" />
      <button class="dos-target-save" onclick="setWatchTarget('${esc(it.ticker)}', this)">Set</button>
      <span class="dos-target-hint">pings when ${esc(it.ticker)} trades at or below this</span>
    </div>
    <div class="dos-meta">${meta.map(([k, v]) => `<div><span class="k">${esc(k)}</span><span class="v">${esc(String(v))}</span></div>`).join("")}</div>`;
}

async function setWatchTarget(ticker, btn) {
  const input = btn.parentElement.querySelector(".dos-target-in");
  const val = Math.max(0, parseFloat(input.value) || 0);
  busy(btn, true);
  try {
    STATE.research = await api("watch/target", { ticker, target_entry: val });
    MEM_SELECTED = ticker;
    renderMemory();
    toast(val > 0 ? `Alert set: ${ticker} under ${money(val)}` : `Alert cleared for ${ticker}`);
  } catch (e) {
    busy(btn, false);
    toast(`Could not set alert for ${ticker}`);
  }
}
window.setWatchTarget = setWatchTarget;

function memDossier(items) {
  if (!MEM_SELECTED || !items.some((i) => i.ticker === MEM_SELECTED)) MEM_SELECTED = items[0].ticker;
  const list = items.map((it) => `
    <button class="dos-item ${it.ticker === MEM_SELECTED ? "sel" : ""}" data-tkr="${it.ticker}">
      <span class="wl-dot ${it.mode}"></span>
      <span class="dos-tkr">${esc(it.ticker)}</span>
      <span class="dos-call">${callShort(it)}</span>
    </button>`).join("");
  const it = items.find((i) => i.ticker === MEM_SELECTED);
  return `<div class="dossier">
    <div class="dos-list">${list}</div>
    <div class="dos-detail">${memDetail(it)}</div>
  </div>`;
}

function renderMemory() {
  const body = $("#memBody");
  if (!body) return;
  const items = memItems();
  if (!items.length) {
    body.innerHTML = `<div class="wl-empty">Nothing tracked yet. Analyze a holding or run Discover — every thesis Signal forms is remembered here.</div>`;
    return;
  }
  body.innerHTML = memDossier(items);
  $$(".dos-item").forEach((b) => b.onclick = () => {
    MEM_SELECTED = b.dataset.tkr;
    $$(".dos-item").forEach((x) => x.classList.toggle("sel", x.dataset.tkr === MEM_SELECTED));
    $(".dos-detail").innerHTML = memDetail(items.find((i) => i.ticker === MEM_SELECTED));
  });
}

async function refreshThesis(ticker, btn) {
  busy(btn, true);
  try {
    const t = await api("analyze", { ticker, refresh: true });
    STATE = await api("state");
    MEM_SELECTED = ticker;
    renderMemory();
    toast(`${ticker} thesis refreshed`);
    showAnalysisModal(ticker, t);
  } catch (e) {
    toast(`Could not refresh ${ticker}`);
  } finally {
    busy(btn, false);
  }
}
window.refreshThesis = refreshThesis;

// ---------- strategy missions ----------
const M_LABEL_ORDER = { BUY: 0, WATCH: 1, WAIT: 2, REJECT: 3 };
let MISSIONS = [];

function missionRoster(m) {
  const cands = (m.candidates || []).slice().sort((a, b) =>
    (M_LABEL_ORDER[a.label] ?? 9) - (M_LABEL_ORDER[b.label] ?? 9) || (b.conviction - a.conviction));
  if (!cands.length) return `<p class="mission-empty">No candidates yet — try Refresh.</p>`;
  return `<div class="mission-roster">` + cands.map((c) => `
    <div class="mrow">
      <span class="m-tkr" onclick="analyze('${esc(c.ticker)}')">${esc(c.ticker)}</span>
      <span class="m-label ${(c.label || "watch").toLowerCase()}">${esc(c.label || "WATCH")}</span>
      <span class="m-conv">${c.conviction}</span>
      <span class="m-reason">${esc(c.reason || "")}</span>
    </div>`).join("") + `</div>`;
}

function renderMissions() {
  const box = $("#missionList");
  if (!box) return;
  const visible = MISSIONS.filter((m) => m.status !== "archived");
  if (!visible.length) {
    box.innerHTML = `<p class="mission-empty">No missions yet. Name a theme above and Signal will build and track a roster for it.</p>`;
    return;
  }
  box.innerHTML = visible.map((m) => {
    const paused = m.status === "paused";
    const n = (m.candidates || []).length;
    const when = m.last_classified_at ? localTime(m.last_classified_at) : "just now";
    return `<div class="mission ${paused ? "paused" : ""}" data-id="${m.id}">
      <div class="mission-head">
        <div class="mission-id">
          <span class="mission-title">${esc(m.title)}</span>
          ${m.theme ? `<span class="mission-theme">${esc(m.theme)} · ${esc(m.mode)}</span>` : ""}
          ${paused ? `<span class="mission-theme">paused</span>` : ""}
        </div>
        <div class="mission-right">
          <span class="mission-when">${n} name${n === 1 ? "" : "s"} · ${when}</span>
          <div class="mission-ctrl">
            <button class="linklike" onclick="runMission('${m.id}', this)">Refresh</button>
            <button class="linklike" onclick="toggleMission('${m.id}', '${paused ? "active" : "paused"}')">${paused ? "Resume" : "Pause"}</button>
            <button class="linklike" onclick="archiveMission('${m.id}')">Archive</button>
          </div>
        </div>
      </div>
      ${missionRoster(m)}
    </div>`;
  }).join("");
}

async function loadMissions() {
  try {
    const r = await api("missions");
    MISSIONS = r.missions || [];
  } catch (e) {
    MISSIONS = [];
  }
  renderMissions();
}

async function startMission() {
  const input = $("#missionInput");
  const title = (input.value || "").trim();
  if (!title) { toast("Name a theme to track"); return; }
  const btn = $("#startMission");
  busy(btn, true);
  toast("Building the roster…");
  try {
    const m = await api("missions", { title, mode: $("#missionMode").value });
    if (m && m.error) { toast(m.error); }
    else { input.value = ""; await loadMissions(); toast(`Mission started: ${m.title}`); }
  } catch (e) {
    toast("Could not start mission");
  } finally {
    busy(btn, false);
  }
}

async function runMission(id, btn) {
  if (btn) busy(btn, true);
  toast("Re-checking the roster…");
  try {
    await api(`missions/${id}/run`, {});
    await loadMissions();
    toast("Mission refreshed");
  } catch (e) {
    toast("Could not refresh mission");
    if (btn) busy(btn, false);
  }
}
window.runMission = runMission;

async function toggleMission(id, status) {
  try {
    await api(`missions/${id}/status`, { status });
    await loadMissions();
  } catch (e) { toast("Could not update mission"); }
}
window.toggleMission = toggleMission;

async function archiveMission(id) {
  if (!confirm("Archive this mission? It stops tracking and disappears from view.")) return;
  try {
    await api(`missions/${id}/status`, { status: "archived" });
    await loadMissions();
    toast("Mission archived");
  } catch (e) { toast("Could not archive mission"); }
}
window.archiveMission = archiveMission;

$("#startMission").onclick = startMission;
$("#missionInput").addEventListener("keydown", (e) => { if (e.key === "Enter") startMission(); });

// ---------- deep research log (Memory tab) ----------
let DEEP_REPORTS = [];
function openDeepLog(i) { const rp = DEEP_REPORTS[i]; if (rp) showDeepReport(rp); }
window.openDeepLog = openDeepLog;

// Rebuild a report card from a stored run — handles both the current format (a
// single "report" step) and older runs that stored granular plan/bull/bear steps.
function reportFromRun(run) {
  const steps = run.steps || [];
  const packed = steps.find((s) => s.type === "report");
  if (packed && packed.report) return packed.report;
  if (!steps.length) return null;
  const find = (t) => steps.find((s) => s.type === t) || {};
  const items = (t) => find(t).items || [];
  const verdict = find("verdict");
  return {
    ticker: (run.query || "").replace(/^deep research:\s*/i, "").trim().toUpperCase(),
    plan: items("plan"), bull_case: items("bull"), bear_case: items("bear"),
    evidence: items("evidence"), critique: items("critique"),
    verdict: verdict.label || verdict.action || "", action: verdict.action || "",
    conviction: verdict.conviction || 0, thesis: "", invalidation: "", changed: false, note: "",
  };
}

async function loadDeepLog() {
  const box = $("#deepLog");
  if (!box) return;
  let runs = [];
  try { const r = await api("agent_runs?kind=deep_research&limit=15"); runs = r.runs || []; } catch (e) {}
  const rows = runs.map((run) => {
    const report = reportFromRun(run);
    return report ? { report, at: run.created_at } : null;
  }).filter(Boolean);
  DEEP_REPORTS = rows.map((r) => r.report);
  if (!rows.length) {
    box.innerHTML = `<p class="mission-empty">No deep dives yet. Open any ticker and run Deep research.</p>`;
    return;
  }
  box.innerHTML = `<div class="deeplog">` + rows.map((row, i) => {
    const rp = row.report;
    return `<button class="deeplog-row" onclick="openDeepLog(${i})">
      <span class="m-tkr">${esc(rp.ticker)}</span>
      <span class="pill ${rp.action}">${esc(rp.verdict || rp.action)}</span>
      <span class="dl-conv">conv ${rp.conviction}/10</span>
      <span class="dl-when">${(row.at || "").slice(0, 10)}</span>
    </button>`;
  }).join("") + `</div>`;
}

// ---------- Re-judgement evidence (audit trail) ----------
// Loaded alongside Activity; a thesis judgement row links to its evidence here.
let REJUDGE_RUNS = [];
const REJUDGE_PILL = { broken: "sell", review: "trim", active: "buy" };

async function loadRejudgeRuns() {
  let runs = [];
  try { const r = await api("agent_runs?kind=rejudge&limit=60"); runs = r.runs || []; } catch (e) {}
  REJUDGE_RUNS = runs.map((run) => {
    const step = (run.steps || []).find((s) => s.type === "rejudge") || {};
    return { ...step, at: run.created_at, brief: step.brief || run.answer || "" };
  }).filter((s) => s.ticker);
}

// Match a thesis event to the re-judgement that produced it: same ticker, closest
// timestamp within a few minutes (the event and the saved run are written together).
function rejudgeForEvent(e) {
  if (!e || !e.ticker) return null;
  const tk = e.ticker.toUpperCase();
  const t = new Date(e.created_at).getTime();
  let best = null, bestGap = Infinity;
  for (const s of REJUDGE_RUNS) {
    if ((s.ticker || "").toUpperCase() !== tk) continue;
    const gap = Math.abs(new Date(s.at).getTime() - t);
    if (gap < bestGap) { bestGap = gap; best = s; }
  }
  return bestGap <= 10 * 60 * 1000 ? best : null;  // within 10 min
}

function openRejudgeFor(ticker, iso) {
  const s = rejudgeForEvent({ ticker, created_at: iso });
  if (s) openRejudge(s);
}
window.openRejudgeFor = openRejudgeFor;

function openRejudge(s) {
  if (!s) return;
  const srcs = (s.sources || []).length
    ? `<span class="lbl">Sources cited</span><ul class="dr-list">${s.sources.map((x) =>
        `<li><a href="${esc(x.url)}" target="_blank" rel="noopener">${esc(x.title || x.url)}</a></li>`).join("")}</ul>`
    : `<span class="lbl">Sources cited</span><p class="muted">No live web citations — graded off recent headlines.</p>`;
  showModal(`<div class="card dr" style="border:none;box-shadow:none;padding:0">
    <div class="dr-head">
      <div class="dr-id"><h3>${esc(s.ticker)}</h3><span class="pill ${REJUDGE_PILL[s.status] || "neutral"}">${esc(s.label || s.status)}</span></div>
      <span class="dr-conv">${(s.at || "").slice(0, 10)}</span>
    </div>
    <span class="lbl">What triggered the review</span><p>${esc(s.trigger || "—")}</p>
    <span class="lbl">Verdict</span><p>${esc(s.reason || "—")}</p>
    ${s.brief ? `<div class="dr-web"><span class="lbl">Live web evidence it read</span><p>${esc(s.brief).replace(/\n/g, "<br>")}</p></div>` : ""}
    ${srcs}
    <p class="muted dr-foot">Persisted audit trail · ${esc(s.ticker)} · this is exactly what Signal read before re-grading the thesis.</p>
  </div>`);
  $("#modalCard").classList.add("modal-wide");
}
window.openRejudge = openRejudge;

// ---------- judge deep-link (a ping/activity row -> the judge's read of its trace) ----------
let JUDGE_BY_RUN = {};
function indexJudgements(events) {
  for (const e of events || []) {
    if (e.judgement && e.judgement.run_id) JUDGE_BY_RUN[e.judgement.run_id] = e.judgement;
  }
}
function judgeChip(e) {
  const j = e.judgement;
  if (!j || !j.run_id) return "";
  return ` <button class="judge-link ${esc(j.verdict)}" onclick="showJudge('${esc(j.run_id)}');event.stopPropagation()" title="how Signal graded this reasoning">judge ${esc(j.verdict)} ${j.score}${j.revised ? " · revised" : ""} →</button>`;
}
function showJudge(runId) {
  const j = JUDGE_BY_RUN[runId];
  if (!j) return;
  showModal(`<div class="card" style="border:none;box-shadow:none;padding:0">
    <h3 style="margin:0 0 4px">How Signal graded this</h3>
    <p class="muted" style="margin:0 0 12px">The auto-judge's read of the reasoning behind this ${esc((EVAL_KINDLABEL[j.kind] || j.kind || "call")).toLowerCase()}. Process quality — grounded, falsifiable, sourced — not market outcome.</p>
    ${judgeBlock(j)}
  </div>`);
  $("#modalCard").classList.add("modal-wide");
}
window.showJudge = showJudge;

// (briefings render as agent messages in the Home stream — see briefMsgHTML / chipBrief)

// ---------- Activity (dense terminal log) ----------
const JUDGEMENT_TYPES = new Set(["thesis_broken", "thesis_review", "thesis_affirmed", "ticker_research"]);
let ACT_FILTER = "all";
let ACT_EVENTS = [];

function actKind(e) {
  return JUDGEMENT_TYPES.has(e.event_type) || e.source === "memory" || e.source === "analyze"
    || e.source === "autoresearch" || e.source === "mandate"
    ? "judgement" : "signal";
}
function actTime(iso) {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
}
function actDay(iso) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const a = new Date(d.getFullYear(), d.getMonth(), d.getDate());
  const now = new Date();
  const b = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const diff = Math.round((b - a) / 86400000);
  if (diff <= 0) return "Today";
  if (diff === 1) return "Yesterday";
  return d.toLocaleDateString([], { month: "short", day: "numeric" });
}
// detail text, with a trailing source URL (catalyst pings) pulled out into a link
function actDetail(e) {
  const raw = e.summary || "";
  const m = raw.match(/\s*(https?:\/\/\S+)\s*$/);
  if (!m) return esc(raw);
  const text = raw.slice(0, m.index).trim();
  return `${esc(text)} <a class="act-src" href="${esc(m[1])}" target="_blank" rel="noopener" onclick="event.stopPropagation()">source →</a>`;
}
// the verb phrase: the stored title minus the leading ticker
function actWhat(e) {
  let t = e.title || e.event_type || "";
  if (e.ticker && t.toUpperCase().startsWith(e.ticker.toUpperCase())) t = t.slice(e.ticker.length).trim();
  return t;
}

async function loadActivity() {
  let r;
  try { r = await api("events?limit=120"); } catch (e) { return; }
  ACT_EVENTS = (r && r.events) || [];
  indexJudgements(ACT_EVENTS);   // so rows can deep-link to the judge's read of their trace
  await loadRejudgeRuns();  // so thesis rows can link straight to their evidence
  renderActivity();
  markPingsRead(ACT_EVENTS);  // looking at the full log clears the "new" pings
}
function renderActivity() {
  const box = $("#actLog");
  if (!box) return;
  const evs = ACT_EVENTS
    .filter((e) => ACT_FILTER === "all" || actKind(e) === ACT_FILTER)
    .sort((a, b) => (b.created_at || "").localeCompare(a.created_at || ""));
  const sub = $("#actSub");
  if (sub) {
    const j = ACT_EVENTS.filter((e) => actKind(e) === "judgement").length;
    sub.textContent = ACT_EVENTS.length
      ? `${ACT_EVENTS.length} entries · ${j} judgement${j === 1 ? "" : "s"}`
      : "What Signal has noticed and decided, newest first.";
  }
  if (!evs.length) {
    box.innerHTML = `<div class="act-empty">Nothing logged yet. Signal writes here as it watches — checks run every couple of minutes.</div>`;
    return;
  }
  let html = "", lastDay = null;
  for (const e of evs) {
    const d = actDay(e.created_at);
    if (d !== lastDay) { html += `<div class="act-day">${esc(d)}</div>`; lastDay = d; }
    const kind = actKind(e);
    const ev = rejudgeForEvent(e);  // a saved re-judgement behind this row?
    const evBtn = ev
      ? ` <button class="act-evidence" onclick="openRejudgeFor('${esc(e.ticker)}','${esc(e.created_at)}')">evidence${(ev.sources || []).length ? ` · ${ev.sources.length} src` : ""} →</button>`
      : "";
    html += `<div class="act-row ${kind} ${esc(e.severity || "info")}">
      <span class="act-time">${esc(actTime(e.created_at))}</span>
      <span class="act-tk"${e.ticker ? ` onclick="analyze('${esc(e.ticker)}')"` : ""}>${esc(e.ticker || "")}</span>
      <span class="act-main"><span class="act-what">${esc(actWhat(e))}</span><span class="act-detail">${actDetail(e)}${evBtn}${judgeChip(e)}</span></span>
    </div>`;
  }
  box.innerHTML = html;
}
$$("#actFilter button").forEach((b) => b.addEventListener("click", () => {
  $$("#actFilter button").forEach((x) => x.classList.remove("on"));
  b.classList.add("on");
  ACT_FILTER = b.dataset.f;
  renderActivity();
}));

// ---------- mandate (the standing goal) ----------
// No card UI anymore — the plan is set by telling the agent in chat (set_mandate tool)
// and read back via the opener + the "My plan" chip. This just keeps the data fresh.
let MANDATE = null, MANDATE_REVIEW = null;

async function loadMandate() {
  try {
    const d = await api("mandate");
    MANDATE = d.mandate || {};
    MANDATE_REVIEW = d.review || null;
  } catch (e) {}
  renderHome();   // the opener reads the mandate state
}

// ---------- findings (folded into the agent's opener message) ----------
let FINDINGS = [];
async function loadFeed() {
  try { FINDINGS = (await api("feed")).findings || []; } catch (e) {}
  renderHome();   // the opener carries the top findings
}

// ---------- editor ----------
function editorRow(h = {}) {
  const d = document.createElement("div"); d.className = "editrow";
  d.innerHTML = `<input placeholder="TICKER" value="${h.ticker || ""}" data-k="ticker"/>
    <input placeholder="qty" type="number" value="${h.quantity ?? ""}" data-k="quantity"/>
    <input placeholder="avg cost" type="number" value="${h.avg_cost ?? ""}" data-k="avg_cost"/>
    <button class="ghost" onclick="this.parentElement.remove()">×</button>`;
  return d;
}
function renderEditor() {
  const box = $("#editRows"); box.innerHTML = "";
  const hs = STATE.portfolio.holdings;
  (hs.length ? hs : [{}]).forEach((h) => box.appendChild(editorRow(h)));
  $("#cashInput").value = STATE.portfolio.cash || 0;
}
$("#addRow").onclick = () => $("#editRows").appendChild(editorRow());
$("#saveHold").onclick = async (e) => {
  const holdings = [...$$("#editRows .editrow")].map((r) => {
    const o = {}; r.querySelectorAll("input").forEach((i) => (o[i.dataset.k] = i.value)); return o;
  }).filter((o) => o.ticker);
  busy(e.target, true);
  await api("holdings", { holdings, cash: parseFloat($("#cashInput").value) || 0 });
  await loadState(); busy(e.target, false); toast("Saved");
};

// ---------- analyze (modal) ----------
async function analyze(ticker) {
  showModal(`<h2>${ticker}</h2><div class="loading"><span class="spin"></span> Loading research…</div>`);
  const t = await api("analyze", { ticker });
  showAnalysisModal(ticker, t);
}
window.analyze = analyze;
async function deepAnalyze(ticker, btn) {
  busy(btn, true);
  const t = await api("analyze", { ticker, refresh: true });
  showAnalysisModal(ticker, t);
}
window.deepAnalyze = deepAnalyze;

// Deep research — the heavy, cited, self-critiqued dive, shown as a report card.
async function deepResearch(ticker) {
  showModal(`<h2 style="margin-bottom:6px">Deep research · ${esc(ticker)}</h2>
    <p class="muted" style="font-size:13px;margin:0 0 16px">Planning, building the bull and bear cases, then self-critiquing before it concludes.</p>
    <div class="loading"><span class="spin"></span> Researching ${esc(ticker)}… this takes a bit.</div>`);
  try {
    const r = await api("deep_research", { ticker });
    if (r && r.error) { showModal(`<h2>${esc(ticker)}</h2><p class="muted">${esc(r.error)}</p>`); return; }
    showDeepReport(r);
    try { STATE = await api("state"); if ($("#memory").classList.contains("active")) { renderMemory(); loadDeepLog(); } } catch (e) {}
  } catch (e) {
    showModal(`<h2>Deep research · ${esc(ticker)}</h2><p class="muted">Research failed — try again in a moment.</p>`);
  }
}
window.deepResearch = deepResearch;

function drList(items) {
  return `<ul class="dr-list">${(items || []).map((x) => `<li>${esc(x)}</li>`).join("") || `<li class="muted">—</li>`}</ul>`;
}

function showDeepReport(r) {
  const flag = r.changed ? `<span class="dr-flag">revised after self-critique</span>` : "";
  showModal(`<div class="card dr" style="border:none;box-shadow:none;padding:0">
    <div class="dr-head">
      <div class="dr-id"><h3>${esc(r.ticker)}</h3><span class="pill ${r.action}">${esc(r.verdict || r.action)}</span></div>
      <span class="dr-conv">conviction ${r.conviction}/10</span>
    </div>
    <div class="bar" style="margin:10px 0 8px"><i style="width:${(r.conviction || 0) * 10}%"></i></div>
    ${r.note || flag ? `<p class="dr-note">${esc(r.note || "")}${flag}</p>` : ""}

    <span class="lbl">Research plan</span>${drList(r.plan)}
    <div class="dr-cols">
      <div><span class="dos-lbl up">Bull case</span>${drList(r.bull_case)}</div>
      <div><span class="dos-lbl down">Bear case</span>${drList(r.bear_case)}</div>
    </div>
    <span class="lbl">Evidence</span>${drList(r.evidence)}
    ${(r.dossier || r.web_brief) ? `<div class="dr-web"><span class="lbl">Research dossier</span><p>${esc(r.dossier || r.web_brief).replace(/\n/g, "<br>")}</p></div>` : ""}
    <div class="dr-crit"><span class="lbl">Self-critique</span>${drList(r.critique)}</div>
    ${r.thesis ? `<span class="lbl">Thesis</span><p>${esc(r.thesis)}</p>` : ""}
    ${r.invalidation ? `<span class="lbl">Breaks if</span><p>${esc(r.invalidation)}</p>` : ""}
    <p class="muted dr-foot">Saved to Signal memory and audit trail · ${esc(r.ticker)}'s thesis updated · logged to the scorecard. Execute manually if you act.</p>
  </div>`);
  $("#modalCard").classList.add("modal-wide");
}
window.showDeepReport = showDeepReport;

function showAnalysisModal(ticker, t) {
  const meta = t.cached
    ? `Cached research${t.refreshed_at ? ` · ${new Date(t.refreshed_at).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })}` : ""}`
    : "Fresh LLM research · saved to DB";
  const sources = (t.sources || []).length
    ? `<span class="lbl">Sources <span class="src-count">${t.sources.length}</span></span><ul class="dr-list src-list">${t.sources.slice(0, 10).map((x) =>
        `<li>${x.kind === "catalyst" ? '<span class="src-tag">catalyst</span> ' : ""}<a href="${esc(x.url)}" target="_blank" rel="noopener">${esc(x.title || x.url)}</a>${x.source ? ` <span class="src-pub">${esc(x.source)}</span>` : ""}</li>`).join("")}</ul>`
    : "";
  showModal(`<div class="card" style="border:none;box-shadow:none;padding:0">${ticketHTML(t)}
    ${sources}
    <div class="fb">
      <button class="yes" onclick="fb('${ticker}',true)">Good idea</button>
      <button class="no" onclick="fb('${ticker}',false)">Pass</button>
    </div>
    <div class="row" style="margin-top:10px;gap:10px">
      <button class="ghost" onclick="deepAnalyze('${ticker}', this)">Re-run analyst</button>
      <button class="primary" onclick="deepResearch('${ticker}')">Deep research →</button>
    </div>
    <p class="muted" style="margin-top:10px;font-size:12px">${esc(meta)}. Deep research runs a fuller, self-critiqued dive and updates the thesis. Execute manually if you act.</p></div>`);
}
function ticketHTML(t) {
  return `<div class="head"><span class="tkr">${t.ticker}</span><span class="pill ${t.action}">${esc(t.decision_label || t.action)}</span></div>
    <div class="conv">conviction ${t.conviction}/10</div><div class="bar"><i style="width:${t.conviction * 10}%"></i></div>
    <span class="lbl">Thesis</span><p>${esc(t.thesis)}</p>
    <span class="lbl">Catalyst</span><p>${esc(t.catalyst)}</p>
    <span class="lbl">Risks</span><p>${esc(t.risks)}</p>
    ${t.suggested_size_pct ? `<span class="lbl">Suggested size</span><p>${t.suggested_size_pct}% of portfolio</p>` : ""}
    ${t.fits_profile_because ? `<span class="lbl">Fits you because</span><p>${esc(t.fits_profile_because)}</p>` : ""}`;
}
async function fb(t, a) { STATE.profile = await api("feedback", { ticker: t, accepted: a }); renderLearned(); toast(a ? "Learned: you like this" : "Learned: you passed"); closeModal(); }
window.fb = fb;

// (discovery now runs from the Home composer's "Find ideas" chip — see chipIdeas)

// ---------- shadow (the scorecard) ----------
const CONV_LABEL = { high: "High · 7–10", medium: "Medium · 4–6", low: "Low · 1–3" };
const titleCase = (s) => (s || "—").replace(/\b\w/g, (c) => c.toUpperCase());

// One row of a summary cut (calibration / by-engine): count, win, return, alpha.
function scCut(label, r) {
  const thin = r.count < 3;   // too few to read as anything but noise
  const alpha = r.benchmarked
    ? `<span class="${cls(r.avg_alpha_pct)}">${pct(r.avg_alpha_pct)}</span>`
    : '<span class="muted">—</span>';
  return `<tr class="${thin ? "sc-thin" : ""}"${thin ? ' title="small sample — treat as noise, not signal"' : ""}>
    <td>${label}</td><td>${r.count}${thin ? '<span class="sc-flag">thin</span>' : ""}</td>
    <td class="${cls(r.win_rate - 50)}">${r.win_rate}%</td>
    <td class="${cls(r.avg_return_pct)}">${pct(r.avg_return_pct)}</td>
    <td>${alpha}</td></tr>`;
}

async function loadScore(refresh = false) {
  const c = await api("scorecard" + (refresh ? "?refresh=1" : ""));
  const h = c.headline || {};
  const f = c.forming || {};
  const narrative = c.narrative || [];
  const bar = h.mature_days || 5;
  const matured = h.matured || 0, total = h.total || 0, forming = h.forming || 0;
  const med = h.median_age_days ?? 0;
  const stamp = new Date().toLocaleTimeString();

  // Truly empty ledger.
  if (!total) {
    $("#scoreRead").innerHTML = `<p class="sc-empty">${esc(narrative[0]
      || "No recommendations logged yet. Analyze a stock, run discovery, or let the assistant make a call to start a track record.")}</p>`;
    $("#scoreCalib").innerHTML = $("#scoreSource").innerHTML = $("#scoreRows").innerHTML = "";
    return;
  }

  // Maturity caveat — loud when nothing's graded, softer when the sample is thin.
  let caveat = "";
  if (matured === 0) {
    caveat = `<div class="sc-caveat warn">Too fresh to grade. The trusted record counts only calls held ≥ ${bar} days — all ${forming} are still forming (median age ${med}d). Everything below is provisional.</div>`;
  } else if (matured < 5) {
    caveat = `<div class="sc-caveat">Only ${matured} call${matured === 1 ? "" : "s"} past the ${bar}-day bar — early signal, not proof.${forming ? ` ${forming} still forming.` : ""}</div>`;
  }

  // Headline — show no win rate at all until something matures; don't dress up noise.
  let statline;
  if (matured === 0) {
    statline = `<div class="statline">
      <div><label>Graded</label><strong class="muted">None yet</strong></div>
      <div><label>Forming</label><strong>${forming}<span class="sc-sub"> call${forming === 1 ? "" : "s"}</span></strong></div>
      <div><label>Median age</label><strong>${med}d<span class="sc-sub"> · need ${bar}d</span></strong></div>
      <div><label>Provisional return</label><strong class="${cls(f.avg_return_pct)} sc-prov">${pct(f.avg_return_pct)}</strong></div>
      <div><label>Updated</label><strong class="sc-stamp">${stamp}</strong></div>
    </div>`;
  } else {
    const edge = h.benchmarked
      ? `<div><label>Edge vs SPY</label><strong class="${cls(h.avg_alpha_pct)}">${pct(h.avg_alpha_pct)}</strong></div>`
      : "";
    statline = `<div class="statline">
      <div><label>Win rate</label><strong class="${cls(h.win_rate - 50)}">${h.win_rate}%</strong></div>
      <div><label>Avg return</label><strong class="${cls(h.avg_return_pct)}">${pct(h.avg_return_pct)}</strong></div>
      ${edge}
      <div><label>Graded</label><strong>${matured}<span class="sc-sub"> of ${total}${forming ? ` · ${forming} forming` : ""}</span></strong></div>
      <div><label>Median age</label><strong>${med}d</strong></div>
      <div><label>Updated</label><strong class="sc-stamp">${stamp}</strong></div>
    </div>`;
  }
  $("#scoreRead").innerHTML = caveat + statline
    + `<div class="sc-narrative">${narrative.map((n) => `<p>${esc(n)}</p>`).join("")}</div>`;

  // Calibration + attribution are computed over matured calls only — show a locked
  // placeholder until calls clear the bar, rather than grading noise.
  const lockRow = (msg) => `<tr class="sc-locked"><td colspan="5">${esc(msg)}</td></tr>`;
  $("#scoreCalib").innerHTML = (c.calibration || []).filter((r) => r.count)
    .map((r) => scCut(CONV_LABEL[r.key] || r.key, r)).join("")
    || lockRow(`Calibration unlocks once calls clear the ${bar}-day bar.`);
  $("#scoreSource").innerHTML = (c.by_source || []).filter((r) => r.count)
    .map((r) => scCut(titleCase(r.key), r)).join("")
    || lockRow("By-engine edge unlocks once calls mature.");

  // Best / worst calls by alpha (vs SPY) — computed server-side over matured,
  // benchmark-anchored calls; "which calls beat the market, and by how much."
  const leadRow = (t) => `
    <div class="lead-row">
      <span class="lead-tk" onclick="analyze('${t.ticker}')">${t.ticker}</span>
      <span class="lead-meta">${esc(t.decision_label || t.action)} · ${esc(titleCase(t.source))}</span>
      <span class="lead-alpha ${cls(t.alpha_pct)}">${pct(t.alpha_pct)}<em>vs SPY</em></span>
      <span class="lead-ret ${cls(t.return_pct)}">${pct(t.return_pct)}<em>return</em></span>
    </div>`;
  const lockLead = (msg) => `<p class="sc-locked-note">${esc(msg)}</p>`;
  $("#scoreBest").innerHTML = (c.best || []).length
    ? c.best.map(leadRow).join("") : lockLead(`Unlocks once calls clear the ${bar}-day bar.`);
  $("#scoreWorst").innerHTML = (c.worst || []).length
    ? c.worst.map(leadRow).join("") : lockLead("Unlocks once calls mature.");

  // Themes that are working — calls grouped by mission (with sector fallback), ranked
  // by alpha. Mission rows deep-link to the Memory tab so you can lean into a winner.
  const themes = c.themes || [];
  $("#scoreThemes").innerHTML = themes.length
    ? themes.map((t, i) => {
        const isMission = t.kind === "mission";
        const stocks = (t.stocks || []).map((s) => `
          <div class="theme-stock">
            <span class="ts-tk" onclick="event.stopPropagation();analyze('${s.ticker}')">${s.ticker}</span>
            <span class="ts-src">${esc(titleCase(s.source))}${s.mature ? "" : " · forming"}</span>
            <span class="ts-alpha ${s.alpha_pct == null ? "muted" : cls(s.alpha_pct)}">${s.alpha_pct == null ? "—" : pct(s.alpha_pct)}<em>vs SPY</em></span>
          </div>`).join("");
        const missionLink = isMission
          ? `<button class="theme-open" onclick="event.stopPropagation();document.querySelector('.tab[data-tab=memory]').click()">open mission in Memory →</button>`
          : `<p class="theme-hint">Auto-grouped by sector (not in a mission). Start a mission to track this theme.</p>`;
        return `<div class="theme-block">
          <div class="theme-row${t.graded ? "" : " provisional"}" onclick="toggleTheme(${i})">
            <span class="theme-name"><span class="theme-caret" id="themeCaret${i}">▸</span>${esc(t.theme)}<span class="theme-kind ${isMission ? "" : "sector"}">${isMission ? "mission" : "sector"}</span></span>
            <span class="theme-alpha ${cls(t.avg_alpha_pct)}">${pct(t.avg_alpha_pct)}<em>vs SPY</em></span>
            <span class="theme-beat">${t.beat_rate}%<em>beat</em></span>
            <span class="theme-calls">${t.calls} call${t.calls === 1 ? "" : "s"}${t.graded ? "" : " · forming"}</span>
          </div>
          <div class="theme-detail" id="themeDetail${i}" hidden>${stocks}${missionLink}</div>
        </div>`;
      }).join("")
    : `<p class="sc-locked-note">No themes yet — log some calls and they'll group by your missions here.</p>`;

  // Every call — the full ledger with age; forming rows dimmed and tagged.
  $("#scoreRows").innerHTML = (c.trades || []).map((t) => `
    <tr class="${t.mature ? "" : "sc-forming"}${t.duplicate ? " sc-dup" : ""}">
      <td>${(t.entry_at || "").slice(0, 10)}</td>
      <td>${t.age_days}d${t.mature ? "" : ' <span class="sc-tag">forming</span>'}</td>
      <td class="tk"><span onclick="analyze('${t.ticker}')">${t.ticker}</span>${t.duplicate ? ` <span class="sc-tag dup" title="A later re-call of a name already tracked — kept for reference">repeat</span> <button class="sc-replace" title="Close the older call(s) for this name; keep this one" onclick="reconcileDup('${t.id}','${t.ticker}')">replace older</button>` : ""}</td>
      <td><span class="sc-call">${t.decision_label || t.action}</span></td>
      <td>${t.conviction}</td>
      <td>$${(t.entry_price || 0).toFixed(2)}</td>
      <td>$${(t.last_price || 0).toFixed(2)}</td>
      <td class="${cls(t.return_pct)}">${pct(t.return_pct)}</td>
      <td>${t.benchmarked ? `<span class="${cls(t.alpha_pct)}">${pct(t.alpha_pct)}</span>` : '<span class="muted">—</span>'}</td>
      <td class="muted">${t.source}</td>
    </tr>`).join("");
}
async function reconcileDup(tradeId, ticker) {
  if (!confirm(`Replace the older ${ticker} call(s) with this one? The older paper trade(s) are deleted and this re-call becomes the live one.`)) return;
  try {
    const r = await api("shadow/reconcile", { trade_id: tradeId, mode: "replace" });
    toast(r && r.ok ? `${ticker}: removed ${r.removed} older call${r.removed === 1 ? "" : "s"}` : "Could not reconcile");
  } catch (e) { toast("Could not reconcile"); }
  loadScore(true);  // re-fetch with fresh marks + prices
}
window.reconcileDup = reconcileDup;
function toggleTheme(i) {
  const d = $("#themeDetail" + i), caret = $("#themeCaret" + i);
  if (!d) return;
  d.hidden = !d.hidden;
  if (caret) caret.textContent = d.hidden ? "▸" : "▾";
}
window.toggleTheme = toggleTheme;
$("#runScore").onclick = () => loadScore(true);

// ---------- evals (error analysis on the brain's own traces) ----------
let EVAL_TAXONOMY = [];
let EVAL_KIND = "";
const EVAL_KINDLABEL = { analyst: "Analyst", rejudge: "Re-judge", deep_research: "Deep research" };

async function loadEvals() {
  const box = $("#evalList");
  if (box) box.innerHTML = `<div class="loading"><span class="spin"></span> Loading traces…</div>`;
  let d;
  try { d = await api("evals" + (EVAL_KIND ? `?kind=${EVAL_KIND}` : "")); }
  catch (e) { if (box) box.innerHTML = `<p class="muted">Could not load evals.</p>`; return; }
  EVAL_TAXONOMY = d.taxonomy || [];
  renderEvalSuite(d);
  renderEvalList(d.traces || []);
}

function renderEvalSuite(d) {
  const box = $("#evalSuite");
  if (!box) return;
  box.innerHTML = `<div class="eval-suites">${judgeSuiteHTML(d.judge || {})}${humanSuiteHTML(d.summary || {})}</div>`;
}

function prettyTag(t) {
  const m = (EVAL_TAXONOMY || []).find((x) => x.id === t);
  return m ? m.label : String(t || "").replace(/_/g, " ");
}

function verdictChips(v) {
  const chip = (k) => `<span class="eval-stat ${k}">${v[k] || 0}<em>${k}</em></span>`;
  return `<div class="eval-verdicts">${chip("good")}${chip("mixed")}${chip("flawed")}</div>`;
}

function failBars(fails) {
  if (!fails || !fails.length) return '<p class="muted">None flagged yet.</p>';
  const max = fails[0].count || 1;
  return fails.map((f) => `<div class="eval-fail"><span class="ef-label">${esc(f.label || f.tag)}</span><span class="ef-bar"><i style="width:${Math.round(f.count / max * 100)}%"></i></span><span class="ef-n">${f.count}</span></div>`).join("");
}

function judgeSuiteHTML(j) {
  if (!j.judged) {
    return `<div class="esuite">
      <div class="esuite-head"><span class="esuite-title">Auto-judge</span><span class="esuite-sub">scored on creation</span></div>
      <div class="eval-empty">No traces scored yet. Run an Analyze or Deep research — the judge scores it against your taxonomy the instant it's made, no market wait.</div></div>`;
  }
  const rev = j.revised ? ` · ${j.revised} self-revised` : "";
  const agree = j.agreement == null ? "" : ` · agrees with you ${j.agreement}%<span class="esuite-dim"> (n=${j.agreement_n})</span>`;
  return `<div class="esuite">
    <div class="esuite-head"><span class="esuite-title">Auto-judge</span><span class="esuite-sub">continuous · scored on creation</span></div>
    <div class="esuite-score"><b>${j.avg_score}</b><em>/100 avg quality</em></div>
    ${verdictChips(j.verdicts || {})}
    <div class="esuite-meta">${j.judged} judged${rev}${agree}</div>
    <span class="tb-lbl">Failure modes the judge flags</span>
    <div class="eval-fails">${failBars(j.failure_counts)}</div>
  </div>`;
}

function humanSuiteHTML(s) {
  const total = s.labeled || 0;
  const body = total
    ? `${verdictChips(s.verdicts || {})}
       <span class="tb-lbl">Your failure taxonomy</span>
       <div class="eval-fails">${failBars(s.failure_counts)}</div>`
    : `<div class="eval-empty">Nothing labeled yet — open a trace below and tag what failed. Your taxonomy builds itself from what you tag; it's the ground truth the judge is measured against.</div>`;
  return `<div class="esuite">
    <div class="esuite-head"><span class="esuite-title">Your labels</span><span class="esuite-sub">ground truth · ${total} tagged</span></div>
    ${body}
  </div>`;
}

function evalTicker(r) {
  const s = (r.steps || [])[0] || {};
  const fromStep = s.ticker || (s.report && s.report.ticker);
  if (fromStep) return String(fromStep).toUpperCase();
  return ((r.query || "").replace(/^(analyze|re-judge|deep research):?\s*/i, "").split(/[\s:]/)[0] || "").toUpperCase();
}

function renderEvalList(traces) {
  const box = $("#evalList");
  if (!box) return;
  EVAL_TRACES = traces;
  if (!traces.length) { box.innerHTML = `<p class="muted">No traces for this filter yet. Run an Analyze or Deep research, then come back.</p>`; return; }
  box.innerHTML = traces.map((r, i) => {
    const lab = r.label, j = r.judgement;
    const jchip = j ? `<span class="eval-verdict-tag j ${j.verdict}" title="auto-judge: ${esc(j.verdict)} ${j.score}/100${j.revised ? " · self-revised" : ""}">${j.score}</span>` : "";
    const tag = lab ? `<span class="eval-verdict-tag ${lab.verdict}" title="your label">${esc(lab.verdict)}</span>` : `<span class="eval-unlabeled">unlabeled</span>`;
    return `<div class="eval-item">
      <div class="eval-row" onclick="toggleEval(${i})">
        <span class="eval-caret" id="evalCaret${i}">▸</span>
        <span class="eval-tk">${esc(evalTicker(r))}</span>
        <span class="eval-kind">${esc(EVAL_KINDLABEL[r.kind] || r.kind)}</span>
        <span class="eval-when">${(r.created_at || "").slice(0, 10)}</span>
        <span class="eval-tags">${jchip}${tag}</span>
      </div>
      <div class="eval-detail" id="evalDetail${i}" hidden>${traceBody(r)}${judgeBlock(r.judgement)}${labelForm(r, lab)}</div>
    </div>`;
  }).join("");
}
let EVAL_TRACES = [];

function traceBody(r) {
  const step = (r.steps || [])[0] || {};
  let out = "";
  if (r.kind === "analyst") {
    out = `${step.label ? `<div class="tb-call"><span class="pill ${step.action}">${esc(step.label)}</span> conviction ${step.conviction || "?"}/10</div>` : ""}
      ${step.thesis ? `<span class="tb-lbl">Thesis</span><p>${esc(step.thesis)}</p>` : ""}
      ${step.catalyst ? `<span class="tb-lbl">Catalyst</span><p>${esc(step.catalyst)}</p>` : ""}
      ${step.risks ? `<span class="tb-lbl">Risks</span><p>${esc(step.risks)}</p>` : ""}`;
  } else if (r.kind === "rejudge") {
    const pill = step.status === "broken" ? "sell" : step.status === "review" ? "trim" : "buy";
    out = `${step.status ? `<div class="tb-call"><span class="pill ${pill}">${esc(step.label || step.status)}</span></div>` : ""}
      ${step.trigger ? `<span class="tb-lbl">What triggered it</span><p>${esc(step.trigger)}</p>` : ""}
      ${step.reason ? `<span class="tb-lbl">Verdict</span><p>${esc(step.reason)}</p>` : ""}`;
  } else if (r.kind === "deep_research") {
    const rep = step.report || {};
    out = `${rep.verdict ? `<div class="tb-call"><span class="pill ${rep.action}">${esc(rep.verdict)}</span> conviction ${rep.conviction || "?"}/10</div>` : ""}
      ${rep.thesis ? `<span class="tb-lbl">Thesis</span><p>${esc(rep.thesis)}</p>` : ""}
      ${(rep.critique || []).length ? `<span class="tb-lbl">Self-critique</span>${rep.critique.map((x) => `<p>· ${esc(x)}</p>`).join("")}` : ""}`;
  }
  const brief = step.brief || r.answer || "";
  const sources = step.sources || (step.report && step.report.sources) || [];
  if (brief) out += `<div class="tb-brief"><span class="tb-lbl">Evidence it read</span><p>${esc(brief).slice(0, 1400).replace(/\n/g, "<br>")}</p></div>`;
  if (sources.length) out += `<span class="tb-lbl">Sources</span><ul class="dr-list src-list">${sources.slice(0, 8).map((s) => `<li><a href="${esc(s.url)}" target="_blank" rel="noopener">${esc(s.title || s.url)}</a></li>`).join("")}</ul>`;
  return `<div class="tb">${out || '<p class="muted">No detail captured for this trace.</p>'}</div>`;
}

function judgeBlock(j) {
  if (!j) return "";
  const modes = (j.failure_modes || []).map((t) => `<span class="jm">${esc(prettyTag(t))}</span>`).join("");
  const gnd = (j.grounding || []).map((g) =>
    `<div class="jg ${g.supported ? "ok" : "no"}"><p>${esc(g.claim)}${g.note ? ` — <em>${esc(g.note)}</em>` : ""}</p></div>`).join("");
  return `<div class="jbox">
    <div class="jhead"><span class="jh-title">Auto-judge</span><span class="jverdict ${j.verdict}">${esc(j.verdict)} · ${j.score}/100</span>${j.revised ? `<span class="jrev">self-revised before shipping</span>` : ""}</div>
    ${j.rationale ? `<p class="jrat">${esc(j.rationale)}</p>` : ""}
    ${modes ? `<div class="jmodes">${modes}</div>` : ""}
    ${gnd ? `<div class="jgnd"><span class="tb-lbl">Grounding check</span>${gnd}</div>` : ""}
    ${j.fix ? `<p class="jfix"><b>Suggested fix</b> · ${esc(j.fix)}</p>` : ""}
  </div>`;
}

function labelForm(r, lab) {
  const sel = new Set((lab && lab.failure_modes) || []);
  const v = (lab && lab.verdict) || "";
  const checks = EVAL_TAXONOMY.map((m) =>
    `<label class="fm${sel.has(m.id) ? " on" : ""}" title="${esc(m.desc)}"><input type="checkbox" value="${m.id}"${sel.has(m.id) ? " checked" : ""}/> ${esc(m.label)}</label>`).join("");
  const seedIds = new Set(EVAL_TAXONOMY.map((m) => m.id));
  const custom = [...sel].filter((t) => !seedIds.has(t))
    .map((t) => `<label class="fm on"><input type="checkbox" value="${esc(t)}" checked/> ${esc(t.replace(/_/g, " "))}</label>`).join("");
  const vr = ["good", "mixed", "flawed"].map((x) =>
    `<label class="vr${v === x ? " on " + x : ""}"><input type="radio" name="v_${esc(r.id)}" value="${x}"${v === x ? " checked" : ""}/> ${x}</label>`).join("");
  return `<div class="eval-form" data-run="${esc(r.id)}" data-kind="${esc(r.kind)}" data-tk="${esc(evalTicker(r))}">
    <span class="tb-lbl">Verdict</span><div class="ef-verdict">${vr}</div>
    <span class="tb-lbl">What failed</span><div class="ef-modes">${checks}${custom}</div>
    <input class="ef-custom" placeholder="add a failure mode (free text), e.g. anchored on price not fundamentals…"/>
    <textarea class="ef-note" placeholder="the error-analysis note — what specifically failed, in your words">${esc((lab && lab.note) || "")}</textarea>
    <button class="primary" onclick="saveEval(this)">${lab ? "Update label" : "Save label"}</button>
  </div>`;
}

function toggleEval(i) {
  const d = $("#evalDetail" + i), c = $("#evalCaret" + i);
  if (!d) return;
  d.hidden = !d.hidden;
  if (c) c.textContent = d.hidden ? "▸" : "▾";
}
window.toggleEval = toggleEval;

async function saveEval(btn) {
  const form = btn.closest(".eval-form");
  const runId = form.dataset.run;
  const verdict = (form.querySelector("input[type=radio]:checked") || {}).value || "";
  const modes = [...form.querySelectorAll("input[type=checkbox]:checked")].map((c) => c.value);
  const custom = form.querySelector(".ef-custom").value.trim();
  if (custom) modes.push(custom);
  const note = form.querySelector(".ef-note").value;
  if (!verdict && !modes.length && !note.trim()) { toast("Add a verdict or a failure mode first"); return; }
  busy(btn, true);
  try {
    await api("evals/label", { run_id: runId, kind: form.dataset.kind, ticker: form.dataset.tk, verdict, failure_modes: modes, note });
    toast("Label saved");
    loadEvals();
  } catch (e) { toast("Could not save label"); }
  busy(btn, false);
}
window.saveEval = saveEval;

$$("#evalFilter button").forEach((b) => b.addEventListener("click", () => {
  $$("#evalFilter button").forEach((x) => x.classList.remove("on"));
  b.classList.add("on");
  EVAL_KIND = b.dataset.k;
  loadEvals();
}));
const _evalRefresh = $("#evalRefresh");
if (_evalRefresh) _evalRefresh.onclick = () => loadEvals();

// ---------- structural risk (portfolio tab) ----------
async function loadStructuralRisk() {
  const box = $("#riskBox");
  if (!box) return;
  box.classList.remove("hidden", "hot");
  box.innerHTML = `<div class="risk-board loading-board">
    <div class="risk-head"><div><p class="risk-kicker">Risk map</p><h3>Portfolio structure</h3></div></div>
    <p class="risk-loading"><span class="spin"></span> Analyzing portfolio structure...</p>
  </div>`;
  let r;
  try {
    r = await api("structural_risk");
    if (r && !((r.clusters || []).length) && /unavailable/i.test(r.headline || "")) {
      r = await api("structural_risk?refresh=true");
    }
  } catch (e) { box.classList.add("hidden"); return; }
  if (!r || !r.headline) { box.classList.add("hidden"); return; }
  renderStructuralRisk(r);
}

function renderStructuralRisk(r) {
  const box = $("#riskBox");
  if (!box) return;
  // Overlapping factor lenses (not pie slices), sorted by exposure. A ticker in 2+ factors is a
  // "doubled-up" name — the real concentration — so we mark those chips and call them out.
  const clusters = (r.clusters || []).slice().sort((a, b) => (b.weight_pct || 0) - (a.weight_pct || 0));
  const count = {};
  clusters.forEach((c) => (c.tickers || []).forEach((t) => { count[t] = (count[t] || 0) + 1; }));
  const dup = Object.keys(count).filter((t) => count[t] >= 2);
  const hasClusters = clusters.length > 0;
  const primary = clusters[0] || {};
  const primaryPct = hasClusters ? Math.round(primary.weight_pct || 0) : null;

  const lenses = clusters.slice(0, 5).map((c, i) => {
    const lead = i === 0 && r.concentrated;
    const w = Math.max(2, Math.min(100, Math.round(c.weight_pct || 0)));
    const chips = (c.tickers || []).map((t) =>
      `<span class="rf-tk ${count[t] >= 2 ? "dup" : ""}">${esc(t)}</span>`).join(" ");
    return `<div class="risk-lens ${lead ? "lead" : ""}">
      <div class="risk-lens-meta">
        <div>
          <div class="risk-lens-top"><span class="risk-lens-title">${esc(c.label)}</span><span class="risk-lens-pct">${Math.round(c.weight_pct || 0)}%</span></div>
          <div class="risk-lens-bar"><i style="width:${w}%"></i></div>
          <div class="rf-tks">${chips}</div>
        </div>
        ${c.breaks_if ? `<p class="risk-lens-breaks">${esc(c.breaks_if)}</p>` : ""}
      </div>
    </div>`;
  }).join("");

  box.classList.remove("hidden");
  box.classList.toggle("hot", !!r.concentrated);
  const status = r.concentrated ? "Concentrated" : "Balanced";
  const dupChips = dup.map((t) => `<span class="rf-tk dup">${esc(t)}</span>`).join(" ");
  box.innerHTML = `
    <div class="risk-board">
      <div class="risk-head"><div><p class="risk-kicker">Risk map</p><h3>Portfolio structure</h3></div>
        <span class="risk-asof">${r.as_of ? new Date(r.as_of).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) : ""}</span></div>
      <div class="risk-summary ${hasClusters ? "" : "empty"}">
        <span class="risk-state">${status}</span>
        ${hasClusters ? `<strong>${primaryPct}%</strong>` : ""}
        <p>${esc(r.headline)}</p>
      </div>
      ${lenses ? `<section class="risk-lenses">${lenses}</section>` : ""}
      ${!lenses ? `<p class="risk-empty">No shared multi-name driver was detected from the current holdings.</p>` : ""}
      ${dup.length ? `<div class="risk-overlap"><span>Doubled-up names</span><div>${dupChips}</div></div>` : ""}
      ${r.note ? `<p class="risk-note">${esc(r.note)}</p>` : ""}
    </div>
  `;
}

// ---------- agentic chat (streaming) ----------
const CHAT_HISTORY = [];
function chartHTML(chart) {
  const w = 520, h = 96;
  const { path } = svgPath(chart.points || [], w, h, 6);
  if (!path) return "";
  const up = (chart.return_pct || 0) >= 0;
  return `<div class="chat-chart">
    <div class="ct"><strong>${esc(chart.ticker)} ${esc(chart.span)}</strong><span class="${up ? "pos" : "neg"}">${pct(chart.return_pct || 0)}</span></div>
    <svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">
      <path d="${path}" fill="none" stroke="${up ? "#00a504" : "#ff5000"}" stroke-width="3" vector-effect="non-scaling-stroke"/>
    </svg>
    <div class="sub2">latest ${money(chart.latest || 0)} · ${esc(chart.source || "chart")}</div>
  </div>`;
}

// ---------- Home — the chat-first cockpit ----------
// The stream is one conversation: everything Signal does on its own (plan pings,
// thesis judgements, briefings, alerts) lands here as agent messages, merged by
// time with your actual chat. Chat turns persist server-side; chip results and
// research traces are session-only (regenerable).
let CHAT_MSGS = [];          // persisted conversation (user + assistant)
let CHAT_BUSY = false;       // an answer is streaming — don't re-render over it
let STREAM_STICK = true;     // pinned to the newest message unless the user scrolls up
const EPHEMERAL = [];        // session-only agent cards (plan reads, idea screens)

async function loadChatHistory() {
  try { CHAT_MSGS = (await api("chat/history?limit=80")).messages || []; }
  catch (e) { CHAT_MSGS = []; }
  // Restore the model's conversational context across reloads (last few turns).
  CHAT_HISTORY.length = 0;
  for (const m of CHAT_MSGS.slice(-12)) {
    CHAT_HISTORY.push({ role: m.role === "user" ? "user" : "assistant", content: m.content });
  }
  renderHome();
}

// Which brain events belong in the conversation: its judgements plus anything it
// flagged as worth interrupting you for. Raw info-level signals stay in Activity.
function chatWorthy(e) {
  return actKind(e) === "judgement" || e.severity === "warn" || e.severity === "alert";
}

const streamTs = (s) => { const t = new Date(s).getTime(); return Number.isFinite(t) ? t : 0; };

function homeItems() {
  const items = [];
  for (const m of CHAT_MSGS) items.push({ at: m.created_at || "", kind: m.role === "user" ? "user" : "assistant", m });
  for (const e of PING_EVENTS) {
    if (chatWorthy(e)) items.push({ at: e.created_at || "", kind: "event", e, fresh: (e.id || 0) > PING_SEEN });
  }
  for (const b of ((STATE && STATE.research && STATE.research.briefings) || []).slice(-6)) {
    items.push({ at: b.created_at || "", kind: "brief", b });
  }
  for (const x of EPHEMERAL) items.push({ at: x.at, kind: "raw", x });
  items.sort((a, b) => streamTs(a.at) - streamTs(b.at));
  return items.slice(-90);
}

function renderHome() {
  const box = $("#homeStream");
  if (!box || CHAT_BUSY) return;
  const items = homeItems();
  let html = "", lastDay = null;
  for (const it of items) {
    const d = actDay(it.at);
    if (d !== lastDay) { html += `<div class="stream-day">${esc(d)}</div>`; lastDay = d; }
    if (it.kind === "user") html += userMsgHTML(it.m.content);
    else if (it.kind === "assistant") html += botMsgHTML(it.m);
    else if (it.kind === "event") html += eventMsgHTML(it.e, it.fresh);
    else if (it.kind === "brief") html += briefMsgHTML(it.b);
    else if (it.kind === "raw") html += it.x.html;
  }
  if (STATE) {
    if (lastDay !== "Today") html += `<div class="stream-day">Today</div>`;
    html += showOnboarding() ? onboardingHTML() : openerHTML();
  }
  box.innerHTML = html || `<div class="loading"><span class="spin"></span> Waking Signal up…</div>`;
  if (STREAM_STICK) box.scrollTop = box.scrollHeight;
  if ($("#home").classList.contains("active") && document.hasFocus()) markPingsRead();
}

const userMsgHTML = (text) => `<div class="msg user"><span>${esc(text)}</span></div>`;
const botMsgHTML = (m) => `<div class="msg bot">${m.trace_html || ""}<span class="ans">${mdLite(m.content)}</span></div>`;

// The agent's standing greeting — live numbers, what's new, and the one ask.
function openerHTML() {
  const pf = STATE.portfolio || {};
  const hs = pf.holdings || [];
  const top = hs.reduce((a, h) => ((h.weight || 0) > ((a && a.weight) || 0) ? h : a), null);
  const profileMax = (STATE.profile && STATE.profile.max_single_position_pct) || 15;
  const bits = [];
  if (hs.length) {
    bits.push(`Your book is ${money0(pf.total_value)} across ${hs.length} position${hs.length === 1 ? "" : "s"}, cash ${money0(pf.cash ?? 0)}.`);
    if (top && (top.weight || 0) > profileMax) bits.push(`${top.ticker} is ${(top.weight || 0).toFixed(1)}% of it — above your ${profileMax}% line.`);
  } else {
    bits.push("No holdings synced yet — connect or enter them in Portfolio and I'll start working.");
  }
  const unread = pingUnread().filter(chatWorthy).length;
  if (unread) bits.push(`${unread} new thing${unread === 1 ? "" : "s"} since you last looked — just above.`);
  let ask = "";
  if (!(MANDATE && MANDATE.statement)) {
    ask = "Tell me what you're trying to do — in plain words, right here — and everything I find will align to it.";
  } else if (MANDATE_REVIEW && (MANDATE_REVIEW.moves || []).length) {
    const n = MANDATE_REVIEW.moves.length;
    ask = `Your plan has ${n} move${n === 1 ? "" : "s"} to consider — tap My plan below.`;
  }
  // the most pressing reads on your names right now (the old Watching panel, in-line)
  const rows = FINDINGS.slice(0, 4).map((x) => {
    let head = x.headline || "";
    if (x.ticker && head.toUpperCase().startsWith(x.ticker.toUpperCase())) {
      head = head.slice(x.ticker.length).replace(/^[\s—:-]+/, "");
    }
    return `<div class="op-row"${x.ticker ? ` onclick="analyze('${esc(x.ticker)}')"` : ""}>
      <span class="ic ${esc(x.kind || "")}"></span>
      <span class="op-head">${x.ticker ? `<em>${esc(x.ticker)}</em> ` : ""}${esc(head)}</span>
    </div>`;
  }).join("");
  const bell = notifyOn() ? "" : ` <button class="linklike" onclick="toggleNotify()">turn on notifications</button>`;
  return `<div class="msg agent opener">
    <span class="agent-kicker">Signal</span>
    <p>${esc(bits.join(" "))}${ask ? " " + esc(ask) : ""}${bell}</p>
    ${rows ? `<span class="op-lbl">On my radar right now</span>${rows}` : ""}
  </div>`;
}

// ---------- first-run onboarding (the guided front door) ----------
// Shown in place of the opener until a new user has connected money, stated a goal, and seen a
// first plan. Keyed off real state, so anyone already set up never sees it; dismissable.
let ONBOARD_DISMISSED = localStorage.getItem("onboardDismissed") === "1";

function onboardState() {
  const hs = (STATE && STATE.portfolio && STATE.portfolio.holdings) || [];
  return { money: hs.length > 0, goal: !!(MANDATE && MANDATE.statement), plan: !!MANDATE_REVIEW };
}
function onboardComplete() { const s = onboardState(); return s.money && s.goal; }   // a goal + a book = onboarded; the first plan then renders below
function onboardFirstTodo() { const s = onboardState(); return !s.money ? "money" : !s.goal ? "goal" : null; }
function showOnboarding() { return !!STATE && !ONBOARD_DISMISSED && !onboardComplete(); }

function onboardingHTML() {
  const s = onboardState(), active = onboardFirstTodo();
  const actions = {
    money: `<button class="ob-btn" onclick="goTab('portfolio')">Add or connect holdings</button>`,
    goal: `<textarea class="ob-input" id="obGoal" placeholder="e.g. long-term growth I can hold a year+, moderate risk, nothing too speculative"></textarea>
           <button class="ob-btn" onclick="saveOnboardGoal(this)">Save my goal</button>`,
  };
  const step = (key, n, title, todo, done) => {
    const isDone = s[key], isActive = active === key;
    return `<div class="ob-step ${isDone ? "done" : isActive ? "active" : "todo"}">
      <span class="ob-dot">${isDone ? "" : n}</span>
      <div class="ob-body">
        <div class="ob-title">${esc(title)}</div>
        <p class="ob-desc">${esc(isDone ? done : todo)}</p>
        ${isActive ? actions[key] : ""}
      </div>
    </div>`;
  };
  return `<div class="msg agent onboard">
    <span class="agent-kicker">Welcome to Signal</span>
    <p class="ob-intro">I'm your research agent. Give me your money and your goal, and I'll watch your book, dig into your names, and bring you a plan — you stay in control of every trade. Three quick steps:</p>
    <div class="ob-steps">
      ${step("money", 1, "Connect your money", "Sync or enter your holdings so I know what you actually own.", "Holdings are in — I can see your book.")}
      ${step("goal", 2, "State your goal", "Tell me what you're trying to do, in plain words. It becomes the standing instruction behind every call.", "Goal set — everything I find now aligns to it.")}
      ${step("plan", 3, "Get your first plan", "The moment your goal is set, I read your book against it and drop the first moves right here in the chat.", "Done — your plan is in the conversation above.")}
    </div>
    <button class="linklike ob-skip" onclick="dismissOnboarding()">skip for now</button>
  </div>`;
}

function goTab(name) { const t = document.querySelector(`.tab[data-tab="${name}"]`); if (t) t.click(); }
window.goTab = goTab;

async function saveOnboardGoal(btn) {
  const ta = $("#obGoal");
  const text = ((ta && ta.value) || "").trim();
  if (!text) { toast("Tell me your goal first"); if (ta) ta.focus(); return; }
  busy(btn, true);
  try {
    await api("mandate", { statement: text });
    await loadMandate();   // saves the goal + warms the plan; re-renders (onboarding completes)
    toast("Goal set");
    if (MANDATE_REVIEW) {   // hand them the first plan as the payoff, right in the stream
      STREAM_STICK = true;
      pushEphemeral(planMsgHTML(MANDATE_REVIEW));
      renderHome();
    }
  } catch (e) { toast("Could not save your goal"); busy(btn, false); }
}
window.saveOnboardGoal = saveOnboardGoal;

function dismissOnboarding() {
  ONBOARD_DISMISSED = true;
  localStorage.setItem("onboardDismissed", "1");
  renderHome();
}
window.dismissOnboarding = dismissOnboarding;

function eventMsgHTML(e, fresh) {
  const kind = actKind(e);
  const click = e.event_type === "mandate_plan" ? ` onclick="chipPlan()"`
    : e.ticker ? ` onclick="analyze('${esc(e.ticker)}')"` : "";
  return `<div class="msg agent ev ${kind} ${esc(e.severity || "info")}${fresh ? " fresh" : ""}"${click}>
    <div class="ev-top">
      ${e.ticker ? `<span class="ev-tk">${esc(e.ticker)}</span>` : ""}
      <span class="ev-what">${esc(actWhat(e))}</span>
      <span class="ev-time">${esc(actTime(e.created_at))}</span>
    </div>
    ${e.summary ? `<p class="ev-sum">${actDetail(e)}</p>` : ""}
    ${e.judgement ? `<div class="ev-judge">${judgeChip(e)}</div>` : ""}
  </div>`;
}

function briefMsgHTML(b) {
  const bullets = (b.bullets || []).slice(0, 4).map((x) => `<li>${esc(x)}</li>`).join("");
  const acts = (b.actions || []).slice(0, 3).map((x) => `<li>${esc(x)}</li>`).join("");
  return `<div class="msg agent brief">
    <span class="agent-kicker">${esc(b.kind || "")} briefing</span>
    <p class="brief-title">${esc(b.title || "Briefing")}</p>
    ${b.summary ? `<p>${esc(b.summary)}</p>` : ""}
    ${bullets ? `<ul>${bullets}</ul>` : ""}
    ${acts ? `<span class="brief-lbl">Do</span><ul>${acts}</ul>` : ""}
  </div>`;
}

function planMsgHTML(r) {
  if (!r) return `<div class="msg agent"><p>Couldn't read the book against your plan just now — try again in a moment.</p></div>`;
  const moves = (r.moves || []).map((mv) => `
    <div class="plan-move">
      <span class="pm-tk" onclick="analyze('${esc(mv.ticker)}')">${esc(mv.ticker)}</span>
      <span class="pill ${esc(mv.action)}">${esc(mv.action)}</span>
      <span class="pm-why">${esc(mv.reason)}</span>
    </div>`).join("");
  return `<div class="msg agent plan">
    <span class="agent-kicker">Where you stand vs your plan</span>
    <p>${esc(r.alignment || "")}</p>
    ${moves || `<p class="plan-fit">Nothing to change — the book fits your plan.</p>`}
    ${r.note ? `<p class="plan-note">${esc(r.note)}</p>` : ""}
  </div>`;
}

function ideasMsgHTML(ideas) {
  if (!ideas.length) return `<div class="msg agent"><p>No fresh ideas cleared the screen right now — try again later or ask for a specific flavor.</p></div>`;
  const rows = ideas.map((i) => `
    <div class="idea-row" onclick="analyze('${esc(i.ticker)}')">
      <span class="ev-tk">${esc(i.ticker)}</span>
      <span class="pill ${esc(i.risk_flavor)}">${esc(i.risk_flavor)}</span>
      <span class="idea-conv">${i.conviction}/10</span>
      <span class="idea-why">${esc(i.why_now || "")}</span>
    </div>`).join("");
  return `<div class="msg agent ideas">
    <span class="agent-kicker">Fresh ideas — outside your holdings</span>
    ${rows}
    <p class="plan-note">Tap a name for the full thesis, or ask me for a stable / more volatile cut.</p>
  </div>`;
}

const spinnerMsg = (text) => `<div class="msg agent temp"><p><span class="spin"></span> ${esc(text)}</p></div>`;

function pushEphemeral(html) {
  return EPHEMERAL.push({ at: new Date().toISOString(), html }) - 1;
}

// ----- composer quick actions ----- //
async function chipPlan() {
  if (!(MANDATE && MANDATE.statement)) {
    toast("No plan yet — tell me your goal in the chat and I'll set it");
    const inp = $("#chatInput");
    if (inp) { inp.placeholder = "e.g. long-term holds I can keep a year+, moderate risk, nothing too speculative"; inp.focus(); }
    return;
  }
  STREAM_STICK = true;
  const ix = pushEphemeral(spinnerMsg("Reading your book against your plan…"));
  renderHome();
  let r = null;
  try { r = (await api("mandate/review", {})).review || null; } catch (e) {}
  if (r) MANDATE_REVIEW = r;
  EPHEMERAL[ix] = { at: EPHEMERAL[ix].at, html: planMsgHTML(r) };
  renderHome();
}
window.chipPlan = chipPlan;

async function chipIdeas() {
  STREAM_STICK = true;
  const ix = pushEphemeral(spinnerMsg("Screening 500+ stocks and writing theses — takes a moment…"));
  renderHome();
  let ideas = [];
  try { ideas = (await api("discover", { flavor: "any", top_n: 4 })).ideas || []; } catch (e) {}
  EPHEMERAL[ix] = { at: EPHEMERAL[ix].at, html: ideasMsgHTML(ideas) };
  renderHome();
}

async function chipBrief() {
  const kind = new Date().getHours() < 14 ? "morning" : "evening";
  STREAM_STICK = true;
  const ix = pushEphemeral(spinnerMsg(`Pulling your ${kind} briefing together…`));
  renderHome();
  let b = null;
  try { b = await api("briefing", { kind }); } catch (e) {}
  if (b && !b.error) {
    STATE.research = STATE.research || {};
    STATE.research.briefings = [...(STATE.research.briefings || []), b];
    EPHEMERAL.splice(ix, 1);   // the saved briefing renders from research state
  } else {
    EPHEMERAL[ix] = { at: EPHEMERAL[ix].at, html: `<div class="msg agent"><p>Couldn't build the briefing just now.</p></div>` };
  }
  renderHome();
}

$$("#homeChips .chip").forEach((c) => c.addEventListener("click", () => {
  if (c.dataset.chip === "plan") chipPlan();
  else if (c.dataset.chip === "ideas") chipIdeas();
  else if (c.dataset.chip === "brief") chipBrief();
}));

const _homeStream = $("#homeStream");
if (_homeStream) _homeStream.addEventListener("scroll", () => {
  STREAM_STICK = _homeStream.scrollTop + _homeStream.clientHeight >= _homeStream.scrollHeight - 80;
});

// ----- send a message (streams the agent's work live) ----- //
async function sendChat() {
  const input = $("#chatInput"); const msg = input.value.trim();
  if (!msg || CHAT_BUSY) return;
  CHAT_BUSY = true;
  STREAM_STICK = true;
  const log = $("#homeStream");
  log.insertAdjacentHTML("beforeend", userMsgHTML(msg));
  input.value = ""; log.scrollTop = log.scrollHeight;
  const id = "b" + Date.now();
  log.insertAdjacentHTML("beforeend", `<div class="msg bot" id="${id}"><details class="toolbox" open><summary>Research trace</summary><div class="steps"></div></details><span class="ans"><span class="spin"></span></span></div>`);
  const wrap = $("#" + id), trace = wrap.querySelector(".toolbox"), steps = wrap.querySelector(".steps"), ans = wrap.querySelector(".ans");
  log.scrollTop = log.scrollHeight;

  let answer = "";
  try {
    const res = await fetch("/api/chat/stream", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: msg, history: CHAT_HISTORY }),
    });
    const reader = res.body.getReader(), dec = new TextDecoder();
    let buf = "";
    while (true) {
      const { done, value } = await reader.read(); if (done) break;
      buf += dec.decode(value, { stream: true });
      const parts = buf.split("\n\n"); buf = parts.pop();
      for (const p of parts) {
        const line = p.replace(/^data:\s*/, "").trim(); if (!line) continue;
        let ev; try { ev = JSON.parse(line); } catch { continue; }
        if (ev.type === "tool") steps.innerHTML += `<div class="step">${esc(ev.name.replace(/_/g, " "))} <em>${esc(JSON.stringify(ev.input))}</em></div>`;
        else if (ev.type === "chart") steps.innerHTML += chartHTML(ev.chart || {});
        else if (ev.type === "tool_result") steps.innerHTML += `<div class="step result">${esc(ev.summary)}</div>`;
        else if (ev.type === "note" && !answer) steps.innerHTML += `<div class="step note">${esc(ev.text).slice(0, 200)}</div>`;
        else if (ev.type === "answer") answer = ev.text;
        else if (ev.type === "error") answer = "Error: " + ev.text;
        if (STREAM_STICK) log.scrollTop = log.scrollHeight;
      }
    }
  } catch (e) {
    answer = answer || "Connection dropped — try again.";
  }
  ans.innerHTML = mdLite(answer);
  let traceHtml = "";
  if (!steps.innerHTML.trim()) trace.remove();
  else { trace.open = false; traceHtml = trace.outerHTML; }
  CHAT_HISTORY.push({ role: "user", content: msg }, { role: "assistant", content: answer });
  const now = new Date().toISOString();
  CHAT_MSGS.push({ role: "user", content: msg, created_at: now },
                 { role: "assistant", content: answer, created_at: now, trace_html: traceHtml });
  CHAT_BUSY = false;
  renderHome();   // settle the in-flight DOM into the data-driven stream
  // The agent may have changed tracked state (watchlist, missions, mandate) — refresh quietly.
  try { STATE = await api("state"); renderState(); loadMandate(); } catch (e) {}
}
$("#sendChat").onclick = sendChat;
$("#chatInput").addEventListener("keydown", (e) => { if (e.key === "Enter") sendChat(); });

// ---------- profile ----------
const opt = (vals, sel) => vals.map((v) => `<option ${v === sel ? "selected" : ""}>${v}</option>`).join("");
const splitc = (s) => s.split(",").map((x) => x.trim()).filter(Boolean);
function renderLearned() {
  const p = STATE.profile;
  const log = (p.learning_log || []);
  $("#learned").innerHTML = (p.investor_signature || log.length) ? `
    <div class="card" style="margin:0 0 18px">
      ${p.investor_signature ? `<span class="lbl">What Signal has learned about you</span><p><strong>${esc(p.investor_signature)}</strong></p>` : ""}
      ${log.length ? `<span class="lbl">Recent adjustments (and why)</span>${log.slice(0, 6).map((l) => `<p>· ${esc(l)}</p>`).join("")}` : ""}
    </div>` : `<p class="sub">No learned signals yet — 👍/👎 some ideas or hit "Learn from my holdings".</p>`;
}
$("#learnBtn").onclick = async (e) => { busy(e.target, true); STATE.profile = await api("learn"); renderLearned(); busy(e.target, false); toast("Learned from your holdings"); };

function renderProfile() {
  renderLearned();
  const p = STATE.profile;
  $("#profileForm").innerHTML = `
    <div class="field"><label>Risk appetite</label><select id="p_appetite">${opt(["conservative", "balanced", "aggressive"], p.appetite)}</select></div>
    <div class="field"><label>Time horizon</label><select id="p_horizon">${opt(["short", "medium", "long"], p.horizon)}</select></div>
    <div class="field"><label>Max single-position %</label><input id="p_maxpos" type="number" value="${p.max_single_position_pct}"/></div>
    <div class="field checkbox"><input id="p_div" type="checkbox" ${p.prefers_dividends ? "checked" : ""}/><label>Prefer dividend income</label></div>
    <div class="field"><label>Favor sectors (comma-sep)</label><input id="p_favor" value="${(p.favor_sectors || []).join(", ")}"/></div>
    <div class="field"><label>Avoid sectors (comma-sep)</label><input id="p_avoid" value="${(p.avoid_sectors || []).join(", ")}"/></div>
    <div class="field full"><label>About you as an investor</label><textarea id="p_notes">${esc(p.notes || "")}</textarea></div>`;
}
$("#saveProfile").onclick = async (e) => {
  const body = { ...STATE.profile, appetite: $("#p_appetite").value, horizon: $("#p_horizon").value,
    max_single_position_pct: parseFloat($("#p_maxpos").value) || 15, prefers_dividends: $("#p_div").checked,
    favor_sectors: splitc($("#p_favor").value), avoid_sectors: splitc($("#p_avoid").value), notes: $("#p_notes").value };
  busy(e.target, true); STATE.profile = await api("profile", body); busy(e.target, false); toast("Saved");
};

// ---------- modal ----------
function showModal(h) { $("#modalCard").innerHTML = h; $("#modal").classList.remove("hidden"); }
function closeModal() { $("#modal").classList.add("hidden"); $("#modalCard").classList.remove("modal-wide"); }
window.closeModal = closeModal;
$("#modal").addEventListener("click", (e) => { if (e.target.id === "modal") closeModal(); });

// ---------- pings: unread tracking + notifications ----------
// Rides on the same event stream the brain already logs. Unread = events with an id newer
// than the last one acknowledged (persisted in localStorage). New events flow straight into
// the Home stream as agent messages; this tracks badges + browser notifications.
let PING_SEEN = +(localStorage.getItem("pingSeenId") || 0);
let PING_EVENTS = [];

function pingUnread() { return PING_EVENTS.filter((e) => (e.id || 0) > PING_SEEN); }

async function loadPings() {
  let r;
  try { r = await api("events?limit=120"); } catch (e) { return; }
  const prev = PING_EVENTS;
  PING_EVENTS = (r && r.events) || [];
  indexJudgements(PING_EVENTS);   // so Home event messages can deep-link to the judge's read
  if (localStorage.getItem("pingSeenId") === null && PING_EVENTS.length) {
    // First ever load: start "caught up" on the existing backlog so it doesn't all show as new.
    // Only events logged from here on will register as pings.
    PING_SEEN = PING_EVENTS.reduce((m, e) => Math.max(m, e.id || 0), 0);
    localStorage.setItem("pingSeenId", String(PING_SEEN));
  }
  renderPings();
  renderHome();   // new brain events land in the conversation
  maybeNotify(prev);
}

function setBadge(id, n) {
  const el = $("#" + id);
  if (!el) return;
  if (n > 0) { el.textContent = n > 99 ? "99+" : n; el.classList.remove("hidden"); }
  else el.classList.add("hidden");
}

function renderPings() {
  const unread = pingUnread();
  setBadge("badgeHome", unread.length);
  setBadge("badgeActivity", unread.length);
}

function markPingsRead(events) {
  const list = (events && events.length) ? events : PING_EVENTS;
  PING_SEEN = list.reduce((m, e) => Math.max(m, e.id || 0), PING_SEEN);
  localStorage.setItem("pingSeenId", String(PING_SEEN));
  renderPings();
}

// Browser notifications — opt-in, fires only for genuinely new warn/alert events (the ones worth
// interrupting you for), and never on first load.
function notifyOn() {
  return localStorage.getItem("pingNotify") === "1" && "Notification" in window && Notification.permission === "granted";
}
function toggleNotify() {
  if (!("Notification" in window)) { toast("This browser can't show notifications"); return; }
  if (notifyOn()) { localStorage.setItem("pingNotify", "0"); renderHome(); toast("Notifications off"); return; }
  Notification.requestPermission().then((p) => {
    localStorage.setItem("pingNotify", p === "granted" ? "1" : "0");
    toast(p === "granted" ? "Notifications on" : "Notifications blocked in browser settings");
    renderHome();   // the opener's notification link reflects the new state
  });
}
window.toggleNotify = toggleNotify;
function maybeNotify(prevEvents) {
  if (!notifyOn()) return;
  const prevMax = prevEvents.reduce((m, e) => Math.max(m, e.id || 0), 0);
  if (!prevMax) return;  // skip the first load — don't dump a notification on open
  const fresh = PING_EVENTS.filter((e) => (e.id || 0) > prevMax && (e.severity === "alert" || e.severity === "warn"));
  for (const e of fresh.slice(0, 3)) {
    try {
      const n = new Notification(`${e.ticker ? e.ticker + " · " : ""}${e.title || "Signal update"}`,
        { body: e.summary || "", tag: "signal-" + e.id });
      n.onclick = () => { window.focus(); if (e.ticker) analyze(e.ticker); };
    } catch (_) { /* notification API can throw on some platforms — never let it break the loop */ }
  }
}

// ---------- boot ----------
loadState().then(() => {
  loadMandate();
  loadChatHistory();
  loadScore();
  loadPings();
  setTimeout(() => refreshLive({ quiet: true }), 250);
  setTimeout(loadFeed, 800);
  // deep-link a tab via #hash (e.g. /#autopilot) once state is ready
  const h = (location.hash || "").replace("#", "");
  const tb = h && document.querySelector(`.tab[data-tab="${h}"]`);
  if (tb) tb.click();
});
setInterval(() => {
  loadState();
  loadPings();
  loadFeed();   // keeps the opener's radar fresh (server-side cached, cheap)
  // Keep the Shadow scorecard live while you're watching it (marks open trades to fresh quotes).
  if ($("#shadow").classList.contains("active")) loadScore(true);
}, 60000);
