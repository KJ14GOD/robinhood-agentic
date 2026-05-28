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
const mdLite = (s) => esc(s).replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
  .replace(/^#+\s*(.+)$/gm, "<strong>$1</strong>").replace(/\n/g, "<br>");
const toast = (m) => { const t = $("#toast"); t.textContent = m; t.classList.remove("hidden"); setTimeout(() => t.classList.add("hidden"), 2200); };
const busy = (b, on) => { b.disabled = on; b.dataset.t = b.dataset.t || b.innerHTML; b.innerHTML = on ? '<span class="spin"></span>' : b.dataset.t; };

let STATE = null;

// tabs
$$(".tab").forEach((t) => t.addEventListener("click", () => {
  $$(".tab").forEach((x) => x.classList.remove("active"));
  $$(".panel").forEach((x) => x.classList.remove("active"));
  t.classList.add("active"); $("#" + t.dataset.tab).classList.add("active");
}));

// ---------- state / header ----------
async function loadState() {
  STATE = await api("state");
  $("#srcTag").textContent = STATE.source.toUpperCase();
  $("#totVal").textContent = money0(STATE.portfolio.total_value);
  const n = STATE.portfolio.holdings.length;
  $("#totMeta").textContent = `${n} position${n === 1 ? "" : "s"} · cash ${money0(STATE.portfolio.cash)}`;
  renderHoldings(); renderProfile(); renderEditor();
}

function renderHoldings() {
  const hs = STATE.portfolio.holdings;
  const maxw = Math.max(1, ...hs.map((h) => h.weight));
  $("#holdRows").innerHTML = hs.map((h) => `
    <div class="hrow" onclick="analyze('${h.ticker}')">
      <div><div class="sym">${h.ticker}</div><div class="sub2">${h.quantity}@$${h.current_price.toFixed(2)}</div></div>
      <div class="hbar"><i style="width:${(h.weight / maxw) * 100}%"></i></div>
      <div class="val">${money0(h.market_value)}<div class="sub2">${h.weight.toFixed(1)}%</div></div>
      <div class="chg ${cls(h.unrealized_pct)}">${pct(h.unrealized_pct)}</div>
    </div>`).join("") || `<p class="muted">No holdings yet.${STATE.source === "manual" ? " Add some below." : ""}</p>`;
  $("#editor").classList.toggle("hidden", STATE.source !== "manual");
  $("#holdNote").textContent = STATE.source === "manual" ? "" : "read-only · Robinhood";
}

// ---------- findings feed ----------
const FIC = { opportunity: "🟢", risk: "🔴", news: "🟡", concentration: "⚠️" };
async function loadFeed() {
  const box = $("#feed");
  box.innerHTML = `<div class="loading"><span class="spin"></span> Scanning your portfolio and the market…</div>`;
  const r = await api("feed");
  const f = r.findings || [];
  box.innerHTML = f.length ? f.map((x) => `
    <div class="finding">
      <div class="ic">${FIC[x.kind] || "•"}</div>
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
  showModal(`<h2>${ticker}</h2><div class="loading"><span class="spin"></span> Analyzing…</div>`);
  const t = await api("analyze", { ticker });
  showModal(`<div class="card" style="border:none;box-shadow:none;padding:0">${ticketHTML(t)}
    <div class="fb"><button class="yes" onclick="fb('${ticker}',true)">👍 Good idea</button>
    <button class="no" onclick="fb('${ticker}',false)">👎 Pass</button></div>
    <p class="muted" style="margin-top:10px;font-size:12px">Logged to shadow mode. Execute manually if you act on it.</p></div>`);
}
window.analyze = analyze;
function ticketHTML(t) {
  return `<div class="head"><span class="tkr">${t.ticker}</span><span class="pill ${t.action}">${t.action}</span></div>
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
async function sendChat() {
  const input = $("#chatInput"); const msg = input.value.trim(); if (!msg) return;
  const log = $("#chatLog");
  log.innerHTML += `<div class="msg user"><span>${esc(msg)}</span></div>`;
  input.value = ""; log.scrollTop = log.scrollHeight;
  const id = "b" + Date.now();
  log.innerHTML += `<div class="msg bot" id="${id}"><div class="steps"></div><span class="ans"><span class="spin"></span></span></div>`;
  const wrap = $("#" + id), steps = wrap.querySelector(".steps"), ans = wrap.querySelector(".ans");
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
      else if (ev.type === "tool_result") steps.innerHTML += `<div class="step result">${esc(ev.summary)}</div>`;
      else if (ev.type === "note" && !answer) steps.innerHTML += `<div class="step note">💭 ${esc(ev.text).slice(0, 200)}</div>`;
      else if (ev.type === "answer") answer = ev.text;
      else if (ev.type === "error") answer = "⚠ " + ev.text;
      log.scrollTop = log.scrollHeight;
    }
  }
  ans.innerHTML = mdLite(answer);
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
loadState().then(() => { loadFeed(); loadScore(); });
