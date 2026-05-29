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
const esc = (s) => (s || "").replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
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
const CHART_CACHE = {};
const PREFETCHING = new Set();

// tabs
$$(".tab").forEach((t) => t.addEventListener("click", () => {
  $$(".tab").forEach((x) => x.classList.remove("active"));
  $$(".panel").forEach((x) => x.classList.remove("active"));
  t.classList.add("active"); $("#" + t.dataset.tab).classList.add("active");
  if (t.dataset.tab === "portfolio" && STATE) loadPortfolioChart();
}));

// ---------- state / header ----------
async function loadState() {
  STATE = await api("state");
  renderState();
}

function renderState() {
  $("#srcTag").textContent = STATE.source.toUpperCase();
  $("#totVal").textContent = money0(STATE.portfolio.total_value);
  const n = STATE.portfolio.holdings.length;
  const stamp = localTime(STATE.as_of || STATE.portfolio.as_of);
  const sync = STATE.sync_ok ? `updated ${stamp || "now"}` : `sync issue`;
  $("#totMeta").textContent = `${n} position${n === 1 ? "" : "s"} · buying power ${money0(STATE.portfolio.buying_power ?? STATE.portfolio.cash)} · ${sync}`;
  renderStaleBanner();
  if (!STATE.sync_ok && STATE.sync_message) toast(STATE.sync_message);
  else if (STATE.portfolio.pricing_warning) console.warn(STATE.portfolio.pricing_warning);
  renderToday();
  renderHoldings(); renderProfile(); renderEditor(); renderResearchState();
  if ($("#portfolio").classList.contains("active")) loadPortfolioChart();
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
  await refreshLive({ quiet: true });
  await loadScore();
  busy(e.target, false);
  toast(STATE.sync_ok ? "Live data refreshed" : "Refresh had a sync issue");
};

function renderToday() {
  if (!STATE) return;
  const hs = STATE.portfolio.holdings || [];
  const weights = hs.map((h) => h.weight || 0);
  const maxWeight = Math.max(0, ...weights);
  const top = hs.find((h) => (h.weight || 0) === maxWeight);
  const profileMax = STATE.profile?.max_single_position_pct || 15;
  const concentration = maxWeight > profileMax ? "Elevated" : maxWeight > profileMax * 0.8 ? "Watch" : "Controlled";
  const dataState = STATE.sync_ok ? (STATE.portfolio.pricing_warning ? "Approximate" : "Fresh") : "Needs sync";
  const next = maxWeight > profileMax ? `Review ${top?.ticker || "largest position"}` : "No forced action";
  const sourceLabel = STATE.portfolio.pricing_source || STATE.source;
  const caveat = STATE.portfolio.pricing_warning ? "Overnight web value may differ" : "Use chat for a researched decision card";
  $("#todayLine").textContent = hs.length
    ? `${concentration} concentration, ${dataState.toLowerCase()} data, ${next}.`
    : "Add or sync holdings to start the assistant workspace.";
  $("#todayMetrics").innerHTML = `
    <div class="signal"><label>Risk</label><strong>${concentration}</strong><span>${top ? `${top.ticker} is ${maxWeight.toFixed(1)}% of portfolio` : "No positions loaded"}</span></div>
    <div class="signal"><label>Data</label><strong>${dataState}</strong><span>${esc(sourceLabel)}</span></div>
    <div class="signal"><label>Next</label><strong>${next}</strong><span>${caveat}</span></div>`;
}

function renderHoldings() {
  const hs = STATE.portfolio.holdings;
  const maxw = Math.max(1, ...hs.map((h) => h.weight));
  $("#holdRows").innerHTML = hs.map((h) => `
    <div class="hrow ${SELECTED_CHART === h.ticker ? "selected" : ""}" onclick="selectPortfolioChart('${h.ticker}')">
      <div><div class="sym">${h.ticker}</div><div class="sub2">${h.quantity}@$${h.current_price.toFixed(2)}</div></div>
      <div class="hbar"><i style="width:${(h.weight / maxw) * 100}%"></i></div>
      <div class="val">${money0(h.market_value)}<div class="sub2">${h.weight.toFixed(1)}%</div></div>
      <div class="h-actions">
        <div class="chg ${cls(h.unrealized_pct)}">${pct(h.unrealized_pct)}</div>
        <button class="ghost mini" onclick="event.stopPropagation(); analyze('${h.ticker}')">Analyze</button>
      </div>
    </div>`).join("") || `<p class="muted">No holdings yet.${STATE.source === "manual" ? " Add some below." : ""}</p>`;
  $("#editor").classList.toggle("hidden", STATE.source !== "manual");
  $("#holdNote").textContent = !STATE.sync_ok ? STATE.sync_message :
    STATE.source === "manual" ? "" :
    `read-only · ${STATE.portfolio.pricing_source || "Robinhood"}`;
}

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
    <div class="chart-price"><strong>${money(latest)}</strong><span class="${up ? "pos" : "neg"}">${pct(chart.return_pct || 0)}</span><em>${chartTime((chart.points || []).at(-1)?.at)}</em></div>
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
  const price = frame.querySelector(".chart-price");
  price.querySelector("strong").textContent = money(best.close);
  price.querySelector("span").textContent = pct(ret);
  price.querySelector("span").className = ret >= 0 ? "pos" : "neg";
  price.querySelector("em").textContent = chartTime(best.at);
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
  const up = (chart.return_pct || 0) >= 0;
  const price = frame.querySelector(".chart-price");
  price.querySelector("strong").textContent = money(chart.latest || 0);
  price.querySelector("span").textContent = pct(chart.return_pct || 0);
  price.querySelector("span").className = up ? "pos" : "neg";
  price.querySelector("em").textContent = chartTime((chart.points || []).at(-1)?.at);
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

async function loadPortfolioChart() {
  const box = $("#portfolioChart");
  if (!box || !STATE) return;
  const ticker = SELECTED_CHART || "portfolio";
  const label = ticker === "portfolio" ? "Portfolio" : ticker;
  const cacheKey = `${ticker}:${CHART_SPAN}`;
  $("#chartTitle").textContent = label;
  $("#resetChart").classList.toggle("hidden", ticker === "portfolio");
  if (CHART_CACHE[cacheKey]) {
    const chart = CHART_CACHE[cacheKey];
    const retClass = (chart.return_pct || 0) >= 0 ? "pos" : "neg";
    $("#chartMeta").innerHTML = `<span class="${retClass}">${pct(chart.return_pct || 0)}</span> over ${esc(CHART_SPAN.toUpperCase())}`;
    box.classList.remove("loading", "refreshing");
    box.innerHTML = chartBlock(chart);
    bindChartInteractions();
    prefetchChartSet(ticker);
    return;
  }
  $("#chartMeta").textContent = "Loading chart...";
  if (box.querySelector("svg")) box.classList.add("refreshing");
  else {
    box.innerHTML = `<span class="spin"></span>`;
    box.classList.add("loading");
  }
  try {
    const r = await fetch(`/api/chart/${encodeURIComponent(ticker)}?span=${encodeURIComponent(CHART_SPAN)}`);
    const chart = await r.json();
    CHART_CACHE[cacheKey] = chart;
    box.classList.remove("loading", "refreshing");
    const retClass = (chart.return_pct || 0) >= 0 ? "pos" : "neg";
    $("#chartMeta").innerHTML = `<span class="${retClass}">${pct(chart.return_pct || 0)}</span> over ${esc(CHART_SPAN.toUpperCase())}`;
    box.innerHTML = chartBlock(chart);
    bindChartInteractions();
    prefetchChartSet(ticker);
  } catch (e) {
    box.classList.remove("loading", "refreshing");
    $("#chartMeta").textContent = "Chart failed to load";
    box.innerHTML = `<div class="empty-chart">Chart request failed.</div>`;
  }
}

function prefetchChartSet(ticker) {
  ["1d", "1w", "1m", "3m", "6m", "1y"].forEach((span) => {
    const key = `${ticker}:${span}`;
    if (span === CHART_SPAN || CHART_CACHE[key] || PREFETCHING.has(key)) return;
    PREFETCHING.add(key);
    fetch(`/api/chart/${encodeURIComponent(ticker)}?span=${encodeURIComponent(span)}`)
      .then((r) => r.json())
      .then((chart) => { if (chart && chart.points) CHART_CACHE[key] = chart; })
      .catch(() => {})
      .finally(() => PREFETCHING.delete(key));
  });
}

function selectPortfolioChart(ticker) {
  const next = ticker || "portfolio";
  // Clicking the already-selected holding again jumps back to the full portfolio.
  SELECTED_CHART = (next !== "portfolio" && SELECTED_CHART === next) ? "portfolio" : next;
  renderHoldings();
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

function renderResearchState() {
  const box = $("#watchlist");
  if (!box) return;
  const research = STATE.research || {};
  const watch = research.watchlist || [];
  const theses = research.theses || {};
  box.innerHTML = watch.length ? watch.slice(-6).reverse().map((x) => {
    const th = theses[x.ticker];
    return `<div class="card">
      <div class="head"><span class="tkr" onclick="analyze('${x.ticker}')">${x.ticker}</span><span class="pill ${x.mode}">${x.mode}</span></div>
      <p>${esc(x.reason || (th && th.thesis) || "No thesis stored yet.")}</p>
      ${th ? `<span class="lbl">Last decision</span><p>${esc(th.last_decision)} · ${esc(th.status)}</p>` : ""}
      ${x.max_allocation_pct ? `<span class="lbl">Max size</span><p>${x.max_allocation_pct}%</p>` : ""}
    </div>`;
  }).join("") : `<p class="muted">No watchlist memory yet. Run Discover or analyze a ticker.</p>`;
  renderBriefings(research.briefings || []);
}

function renderBriefings(items) {
  const box = $("#briefings");
  if (!box) return;
  box.innerHTML = items.length ? items.slice(-4).reverse().map((b) => `
    <div class="card">
      <div class="head"><span class="tkr">${esc(b.kind || "briefing")}</span><span class="pill neutral">${esc((b.created_at || "").slice(0, 10))}</span></div>
      <p><strong>${esc(b.title || "Briefing")}</strong></p>
      <p>${esc(b.summary || "")}</p>
      ${(b.bullets || []).slice(0, 5).map((x) => `<p>· ${esc(x)}</p>`).join("")}
      ${(b.actions || []).length ? `<span class="lbl">Actions</span>${b.actions.map((x) => `<p>${esc(x)}</p>`).join("")}` : ""}
    </div>`).join("") : `<p class="muted">No briefings yet. Generate one for morning or evening.</p>`;
}

async function createBriefing(kind, btn) {
  busy(btn, true);
  const b = await api("briefing", { kind });
  STATE.research = STATE.research || {};
  STATE.research.briefings = [...(STATE.research.briefings || []), b];
  renderBriefings(STATE.research.briefings);
  busy(btn, false);
  toast(`${kind} briefing saved`);
}

$("#morningBrief").onclick = (e) => createBriefing("morning", e.target);
$("#eveningBrief").onclick = (e) => createBriefing("evening", e.target);

// ---------- findings feed ----------
async function loadFeed() {
  const box = $("#feed");
  box.innerHTML = `<div class="loading"><span class="spin"></span> Scanning your portfolio and the market…</div>`;
  const r = await api("feed");
  const f = r.findings || [];
  box.innerHTML = f.length ? f.map((x) => `
    <div class="finding">
      <div class="ic ${x.kind || ""}"></div>
      <div class="body">
        <div class="ft">${x.ticker ? `<span class="tk" onclick="analyze('${x.ticker}')">${x.ticker}</span> ` : ""}${esc(x.headline)}</div>
        <div class="fd">${esc(x.detail)}</div>
      </div>
      <span class="pill ${x.kind}">${x.kind}</span>
    </div>`).join("") : `<div class="loading">Nothing pressing right now. Add holdings or ask the brain below.</div>`;
}
$("#refreshFeed").onclick = loadFeed;

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
function showAnalysisModal(ticker, t) {
  const meta = t.cached
    ? `Cached research${t.refreshed_at ? ` · ${new Date(t.refreshed_at).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })}` : ""}`
    : "Fresh LLM research · saved to DB";
  showModal(`<div class="card" style="border:none;box-shadow:none;padding:0">${ticketHTML(t)}
    <div class="fb">
      <button class="yes" onclick="fb('${ticker}',true)">Good idea</button>
      <button class="no" onclick="fb('${ticker}',false)">Pass</button>
      <button class="ghost" onclick="deepAnalyze('${ticker}', this)">Deep refresh</button>
    </div>
    <p class="muted" style="margin-top:10px;font-size:12px">${esc(meta)}. Execute manually if you act on it.</p></div>`);
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

// ---------- discover ----------
$("#runDiscover").onclick = async (e) => {
  busy(e.target, true);
  $("#ideas").innerHTML = `<div class="loading"><span class="spin"></span> Screening 500+ stocks and writing theses…</div>`;
  const r = await api("discover", { flavor: $("#flavor").value, top_n: 5 });
  $("#ideas").innerHTML = (r.ideas || []).map((i) => `
    <div class="card"><div class="head"><span class="tkr" onclick="analyze('${i.ticker}')">${i.ticker}</span>
      <span class="pill ${i.risk_flavor}">${i.risk_flavor}</span></div>
      <div class="conv">${esc(i.name || "")} · conviction ${i.conviction}/10</div><div class="bar"><i style="width:${i.conviction * 10}%"></i></div>
      <span class="lbl">Why now</span><p>${esc(i.why_now)}</p>
      <span class="lbl">Signal</span><p>${esc(i.signal_summary)}</p></div>`).join("") || `<p class="muted">No matches — try another flavor.</p>`;
  busy(e.target, false);
};

// ---------- digest ----------
$("#runDigest").onclick = async (e) => {
  busy(e.target, true);
  $("#digestSummary").innerHTML = `<div class="loading"><span class="spin"></span> Reading news & signals for every holding…</div>`;
  $("#digestInsights").innerHTML = "";
  const d = await api("digest");
  $("#digestSummary").innerHTML = `<div class="card" style="margin:14px 0">${mdLite(d.summary || "")}
    ${(d.concentration_flags || []).map((f) => `<p class="neg">⚠ ${esc(f)}</p>`).join("")}</div>`;
  $("#digestInsights").innerHTML = (d.insights || []).map((i) => `
    <div class="card"><div class="head"><span class="tkr" onclick="analyze('${i.ticker}')">${i.ticker}</span>
      <span class="pill ${i.sentiment}">${i.sentiment}</span></div>
      <p><strong>${esc(i.headline)}</strong></p><p>${esc(i.detail)}</p></div>`).join("");
  busy(e.target, false);
};

// ---------- shadow ----------
async function loadScore() {
  const s = await api("scoreboard");
  $("#scoreStats").innerHTML = s.count ? `
    <div><label>Logged</label><strong>${s.count}</strong></div>
    <div><label>Win rate</label><strong class="${cls(s.win_rate - 50)}">${s.win_rate}%</strong></div>
    <div><label>Avg return</label><strong class="${cls(s.avg_return_pct)}">${pct(s.avg_return_pct)}</strong></div>
    <div><label>Best / Worst</label><strong><span class="pos">${pct(s.best)}</span> / <span class="neg">${pct(s.worst)}</span></strong></div>`
    : `<p class="muted">No recommendations logged yet. Analyze a stock or run discovery to start a track record.</p>`;
  $("#scoreRows").innerHTML = (s.trades || []).map((t) => `
    <tr><td>${(t.entry_at || "").slice(0, 10)}</td><td class="tk" onclick="analyze('${t.ticker}')">${t.ticker}</td>
    <td>${t.action}</td><td>${t.conviction}</td><td>$${t.entry_price.toFixed(2)}</td><td>$${t.last_price.toFixed(2)}</td>
    <td class="${cls(t.return_pct)}">${pct(t.return_pct)}</td><td class="muted">${t.source}</td></tr>`).join("");
}
$("#runScore").onclick = loadScore;

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

async function sendChat() {
  const input = $("#chatInput"); const msg = input.value.trim(); if (!msg) return;
  const log = $("#chatLog");
  log.innerHTML += `<div class="msg user"><span>${esc(msg)}</span></div>`;
  input.value = ""; log.scrollTop = log.scrollHeight;
  const id = "b" + Date.now();
  log.innerHTML += `<div class="msg bot" id="${id}"><details class="toolbox" open><summary>Research trace</summary><div class="steps"></div></details><span class="ans"><span class="spin"></span></span></div>`;
  const wrap = $("#" + id), trace = wrap.querySelector(".toolbox"), steps = wrap.querySelector(".steps"), ans = wrap.querySelector(".ans");
  $("#assistantStatus").textContent = "Researching";
  log.scrollTop = log.scrollHeight;

  const res = await fetch("/api/chat/stream", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message: msg, history: CHAT_HISTORY }),
  });
  const reader = res.body.getReader(), dec = new TextDecoder();
  let buf = "", answer = "";
  while (true) {
    const { done, value } = await reader.read(); if (done) break;
    buf += dec.decode(value, { stream: true });
    const parts = buf.split("\n\n"); buf = parts.pop();
    for (const p of parts) {
      const line = p.replace(/^data:\s*/, "").trim(); if (!line) continue;
      let ev; try { ev = JSON.parse(line); } catch { continue; }
      if (ev.type === "tool") steps.innerHTML += `<div class="step">🔍 ${ev.name.replace(/_/g, " ")} <em>${esc(JSON.stringify(ev.input))}</em></div>`;
      else if (ev.type === "chart") steps.innerHTML += chartHTML(ev.chart || {});
      else if (ev.type === "tool_result") steps.innerHTML += `<div class="step result">${esc(ev.summary)}</div>`;
      else if (ev.type === "note" && !answer) steps.innerHTML += `<div class="step note">💭 ${esc(ev.text).slice(0, 200)}</div>`;
      else if (ev.type === "answer") answer = ev.text;
      else if (ev.type === "error") answer = "Error: " + ev.text;
      log.scrollTop = log.scrollHeight;
    }
  }
  ans.innerHTML = mdLite(answer);
  if (!steps.innerHTML.trim()) trace.remove();
  else trace.open = false;
  $("#assistantStatus").textContent = "Ready";
  CHAT_HISTORY.push({ role: "user", content: msg }, { role: "assistant", content: answer });
  log.scrollTop = log.scrollHeight;
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
      ${p.investor_signature ? `<span class="lbl">What the brain has learned about you</span><p><strong>${esc(p.investor_signature)}</strong></p>` : ""}
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
function closeModal() { $("#modal").classList.add("hidden"); }
window.closeModal = closeModal;
$("#modal").addEventListener("click", (e) => { if (e.target.id === "modal") closeModal(); });

// ---------- boot ----------
loadState().then(() => {
  loadScore();
  setTimeout(() => refreshLive({ quiet: true }), 250);
  setTimeout(loadFeed, 800);
});
setInterval(loadState, 60000);
