/* TradingAgents Pipeline dashboard (specs/webui-pages.md pages 1-5).
   Vanilla JS, renders exclusively from the local /api/pipeline/* endpoints.
   The markdown helpers are copied from app.js (each page's script is
   standalone: app.js boots the trading SPA and would crash on this DOM). */
"use strict";

const $ = (sel) => document.querySelector(sel);
const api = async (path, opts) => {
  const res = await fetch(path, opts);
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || res.statusText);
  }
  return res.json();
};

/* ---------------- markdown (minimal, escape-first — same rules as app.js) */
function esc(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
                  .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}
function safeUrl(u) {
  return /^https?:\/\/[^\s"'<>]+$/.test(u) ? u : null;
}
function mdInline(s) {
  const spans = [];
  let out = s.replace(/`([^`]+)`/g, (_, code) => {
    spans.push(code);
    return `\u0000${spans.length - 1}\u0000`;
  });
  out = out
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/\*([^*]+)\*/g, "<em>$1</em>")
    .replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (whole, text, url) => {
      const raw = url.replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&amp;/g, "&");
      const safe = safeUrl(raw);
      return safe
        ? `<a href="${esc(safe)}" target="_blank" rel="noopener noreferrer">${text}</a>`
        : whole;
    });
  return out.replace(/\u0000(\d+)\u0000/g, (m, i) =>
    spans[Number(i)] === undefined ? m : `<code>${spans[Number(i)]}</code>`);
}
function mdToHtml(src) {
  const lines = esc(src).split(/\r?\n/);
  const out = [];
  let list = null, table = null, code = false;
  const closeList = () => { if (list) { out.push(`</${list}>`); list = null; } };
  const closeTable = () => { if (table) { out.push("</tbody></table>"); table = null; } };
  for (const line of lines) {
    if (line.trim().startsWith("```")) {
      closeList(); closeTable();
      out.push(code ? "</pre>" : "<pre>"); code = !code; continue;
    }
    if (code) { out.push(line); continue; }
    const h = line.match(/^(#{1,4})\s+(.*)/);
    if (h) { closeList(); closeTable(); out.push(`<h${h[1].length}>${mdInline(h[2])}</h${h[1].length}>`); continue; }
    if (/^\s*(---|\*\*\*)\s*$/.test(line)) { closeList(); closeTable(); out.push("<hr>"); continue; }
    const ul = line.match(/^\s*[-*]\s+(.*)/);
    const ol = line.match(/^\s*\d+[.)]\s+(.*)/);
    if (ul || ol) {
      closeTable();
      const kind = ul ? "ul" : "ol";
      if (list !== kind) { closeList(); out.push(`<${kind}>`); list = kind; }
      out.push(`<li>${mdInline((ul || ol)[1])}</li>`); continue;
    }
    closeList(); closeTable();
    if (line.trim() === "") continue;
    out.push(`<p>${mdInline(line)}</p>`);
  }
  closeList(); closeTable(); if (code) out.push("</pre>");
  return out.join("\n");
}

/* ---------------- shared formatting ---------------- */
const STATUS_LABEL = {
  ok: "ok", warn: "warn", failed: "failed", timeout: "timeout",
  skipped: "skipped", running: "running",
};
function statusBadge(status) {
  const cls = STATUS_LABEL[status] ? `st-${status}` : "st-unknown";
  return `<span class="badge ${cls}">${esc(status || "?")}</span>`;
}
function verdictBadge(verdict) {
  const map = { pass: "st-ok", warn: "st-warn", fail: "st-failed", missing: "st-skipped" };
  return `<span class="badge ${map[verdict] || "st-unknown"}">${esc(verdict || "missing")}</span>`;
}
function pct(x) { return x == null ? "—" : `${(x * 100).toFixed(1)}%`; }
function num(x, digits = 4) { return x == null ? "—" : Number(x).toFixed(digits); }
function emptyCard(msg) { return `<div class="empty-state">${esc(msg)}</div>`; }

/* ---------------- halt banner + toggle (visible on every page) ------------ */
let halted = false;
function renderHalt() {
  $("#haltBanner").hidden = !halted;
  const btn = $("#haltToggle");
  btn.textContent = halted ? "解除 HALT" : "启用 HALT";
  btn.classList.toggle("halt-on", halted);
}
async function refreshHalt() {
  try {
    const h = await api("/api/pipeline/halt");
    halted = !!h.halted;
  } catch (e) { /* endpoint unreachable — leave the last known state */ }
  renderHalt();
}
async function toggleHalt() {
  const next = !halted;
  const prompt = next
    ? "启用 EXECUTION_HALT？所有后续提交将转为 dry-run。"
    : "解除 EXECUTION_HALT？执行适配器将恢复实际提交。";
  if (!window.confirm(prompt)) return;
  const h = await api("/api/pipeline/halt", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ halted: next }),
  });
  halted = !!h.halted;
  renderHalt();
}

/* ---------------- page 1: status ---------------- */
function componentRow(name, rec) {
  rec = rec || {};
  return `<tr>
    <td class="mono">${esc(name)}</td>
    <td>${statusBadge(rec.status)}</td>
    <td class="mono">${rec.duration_s == null ? "—" : esc(String(rec.duration_s)) + "s"}</td>
    <td class="err-cell">${esc(rec.error || "")}</td>
  </tr>`;
}
async function loadStatus() {
  const el = $("#statusCards");
  let data;
  try { data = await api("/api/pipeline/status"); }
  catch (e) { el.innerHTML = emptyCard(`状态读取失败：${e.message}`); return; }
  halted = !!data.halted; renderHalt();
  const cards = [];
  for (const [session, info] of Object.entries(data.sessions)) {
    const staleBadge = info.stale
      ? `<span class="badge st-failed">STALE</span><span class="stale-reason">${esc(info.stale_reason || "")}</span>`
      : "";
    let body;
    if (!info.present) {
      body = emptyCard(`尚未运行（not yet run）— 未找到 pipeline_status.${session}.json`);
    } else {
      const slot = (info.status || {}).slot || {};
      const comps = (info.status || {}).components || {};
      const running = slot.finished_at ? "" : `<span class="badge st-running">in progress</span>`;
      body = `
        <div class="slot-meta mono">slot ${esc(slot.date || "?")} · started ${esc(slot.started_at || "?")}
          · finished ${esc(slot.finished_at || "—")} ${running}</div>
        <table class="ptable"><tbody>
          <tr><th>component</th><th>status</th><th>duration</th><th>error</th></tr>
          ${Object.entries(comps).map(([n, r]) => componentRow(n, r)).join("")}
        </tbody></table>
        ${info.summary_line ? `<div class="summary-line mono">${esc(info.summary_line)}</div>` : ""}`;
    }
    cards.push(`<div class="card">
      <div class="status-head"><h3 class="sec-title">${session.toUpperCase()} 时段
        <span class="run-meta">slot ${esc(info.slot_time)} · ${esc(info.timezone)}</span></h3>${staleBadge}</div>
      ${body}
    </div>`);
  }
  el.innerHTML = cards.join("");
}

/* ---------------- page 2: pool ---------------- */
let poolSession = "us";
function boll(b) {
  if (!b) return "—";
  return `C ${num(b.close, 2)} · mid ${num(b.mid, 2)} · up ${num(b.upper, 2)} · low ${num(b.lower, 2)}`;
}
function poolRows(list, opts) {
  const { showCatalyst = true, showStreaks = false } = opts || {};
  return (list || []).map((e) => {
    const t = e.technical || {};
    return `<tr>
      <td class="mono">${esc(e.ticker || "")}</td>
      <td>${e.score == null ? "—" : esc(String(e.score))}</td>
      ${showCatalyst ? `<td>${esc(e.catalyst_type || "—")}</td>` : ""}
      <td>${t.gate ? verdictBadge(t.gate === "pass" ? "pass" : t.gate === "fail" ? "fail" : "warn") + " " + esc(t.gate) : "—"}</td>
      <td class="mono small">日线 ${boll(t.boll_daily)}<br>周线 ${boll(t.boll_weekly)}<br>
        ADV20 $${t.avg_dollar_volume_20d == null ? "—" : Number(t.avg_dollar_volume_20d).toLocaleString()}
        · V5/V20 ${num(t.volume_ratio_5d_20d, 2)}</td>
      ${showStreaks ? `<td class="mono">${esc(String(e.entered_on || "—"))} · low ${esc(String(e.low_score_streak ?? "—"))} · gate ${esc(String(e.gate_fail_streak ?? "—"))}</td>` : ""}
      <td class="rationale">${esc(e.rationale || e.note || "")}</td>
    </tr>`;
  }).join("");
}
async function loadPool() {
  const el = $("#poolBody");
  let data;
  try { data = await api(`/api/pipeline/pool/${poolSession}`); }
  catch (e) { el.innerHTML = emptyCard(`股票池读取失败：${e.message}`); return; }
  halted = !!data.halted; renderHalt();
  $("#poolCarried").hidden = !data.carried_forward;
  if (!data.present) {
    $("#poolMeta").textContent = "";
    el.innerHTML = emptyCard(`尚未运行（${data.message || "not yet run"}）`);
    return;
  }
  const p = data.pool;
  $("#poolMeta").textContent =
    `${data.file} · generated ${p.generated_at || "?"} · ${p.generator || ""}`;
  el.innerHTML = `
    <h3 class="sec-title">Core（核心持仓，分数仅供参考不作门槛）</h3>
    <table class="ptable"><tbody>
      <tr><th>ticker</th><th>score</th><th>gate</th><th>技术快照</th><th>note</th></tr>
      ${poolRows(p.core, { showCatalyst: false })}
    </tbody></table>
    <h3 class="sec-title">Opportunity（机会层）</h3>
    <table class="ptable"><tbody>
      <tr><th>ticker</th><th>score</th><th>catalyst</th><th>gate</th><th>技术快照</th><th>entered · streaks</th><th>rationale</th></tr>
      ${poolRows(p.opportunity, { showStreaks: true })}
    </tbody></table>
    <h3 class="sec-title">Watch（观察层）</h3>
    <table class="ptable"><tbody>
      <tr><th>ticker</th><th>score</th><th>catalyst</th><th>gate</th><th>技术快照</th><th>rationale</th></tr>
      ${poolRows(p.watch)}
    </tbody></table>
    <h3 class="sec-title">Removed（移出）</h3>
    <table class="ptable"><tbody>
      <tr><th>ticker</th><th>last_score</th><th>reason</th></tr>
      ${(p.removed || []).map((e) => `<tr><td class="mono">${esc(e.ticker || "")}</td>
        <td>${e.last_score == null ? "—" : esc(String(e.last_score))}</td>
        <td>${esc(e.reason || "")}</td></tr>`).join("")}
    </tbody></table>`;
}

/* ---------------- page 3: briefs ---------------- */
async function loadBriefs() {
  let data;
  const macroBody = $("#macroBriefTable").querySelector("tbody");
  const tickerBody = $("#tickerBriefTable").querySelector("tbody");
  try { data = await api("/api/pipeline/briefs"); }
  catch (e) { macroBody.innerHTML = `<tr><td>${esc(e.message)}</td></tr>`; return; }
  halted = !!data.halted; renderHalt();
  const row = (b) => `<tr class="brief-row" data-kind="${esc(b.kind)}" data-name="${esc(b.name)}">
    <td class="mono">${esc(b.date)}</td>
    <td>${esc(b.session || "—")}</td>
    ${b.kind === "ticker" ? `<td class="mono">${esc(b.ticker)}</td>` : ""}
    <td>${verdictBadge(b.verdict)}</td>
  </tr>`;
  macroBody.innerHTML = data.macro.length
    ? `<tr><th>date</th><th>session</th><th>eval</th></tr>` + data.macro.map(row).join("")
    : `<tr><td class="empty-state">暂无宏观简报（not yet run）</td></tr>`;
  tickerBody.innerHTML = data.ticker.length
    ? `<tr><th>date</th><th>session</th><th>ticker</th><th>eval</th></tr>` + data.ticker.map(row).join("")
    : `<tr><td class="empty-state">暂无个股简报（not yet run）</td></tr>`;
}
async function openBrief(kind, name) {
  const detail = $("#briefDetail");
  detail.hidden = false;
  $("#briefName").textContent = name;
  $("#briefBody").innerHTML = "载入中…";
  let b;
  try {
    b = await api(`/api/pipeline/brief?kind=${encodeURIComponent(kind)}&name=${encodeURIComponent(name)}`);
  } catch (e) { $("#briefBody").innerHTML = emptyCard(e.message); return; }
  $("#briefVerdict").outerHTML = `<span id="briefVerdict">${verdictBadge(b.verdict)}</span>`;
  const flags = b.flagged_claims || [];
  $("#briefFlags").innerHTML = flags.length
    ? `<h4 class="sec-title">评估标记的论断（flagged claims）</h4>
       <table class="ptable"><tbody><tr><th>section</th><th>severity</th><th>claim</th><th>issue</th></tr>
       ${flags.map((f) => `<tr><td>${esc(f.section || "")}</td><td>${verdictBadge(f.severity === "fabrication" ? "fail" : f.severity === "major" ? "warn" : "pass")} ${esc(f.severity || "")}</td><td>${esc(f.claim || "")}</td><td>${esc(f.issue || "")}</td></tr>`).join("")}
       </tbody></table>`
    : (b.eval ? `<div class="run-meta">评估未标记任何论断。</div>` : `<div class="run-meta">该简报尚无有效评估（missing）。</div>`);
  $("#briefBody").innerHTML = mdToHtml(b.markdown);
}

/* ---------------- page 4: A/B aggregates ---------------- */
function metricsTable(byArm, weighting) {
  const arms = ["brief", "feeds"];
  const m = (arm) => ((byArm[arm] || {})[weighting]) || {};
  const rowsOf = (label, fn) =>
    `<tr><td>${label}</td>${arms.map((a) => `<td class="mono">${fn(m(a))}</td>`).join("")}</tr>`;
  return `<table class="ptable"><tbody>
    <tr><th>指标</th><th>brief</th><th>feeds</th></tr>
    ${rowsOf("行数（BUY/SELL/HOLD/ERROR）", (x) => `${x.n_rows ?? 0}（${x.n_buy ?? 0}/${x.n_sell ?? 0}/${x.n_hold ?? 0}/${x.n_error ?? 0}）`)}
    ${["d1", "d5", "d20"].map((h) =>
      rowsOf(`hit rate @ ${h}（n）`, (x) => `${pct((x.hit_rate || {})[h])}（${(x.hit_n || {})[h] ?? 0}）`)
    ).join("")}
    ${["d1", "d5", "d20"].map((h) =>
      rowsOf(`excess return @ ${h}`, (x) => num((x.excess_return || {})[h]))
    ).join("")}
    ${rowsOf("entry-hit rate", (x) => pct((x.plan_quality || {}).entry_hit_rate))}
    ${rowsOf("mean realized R:R（非 ambiguous）", (x) => num((x.plan_quality || {}).mean_realized_rr, 2))}
    ${rowsOf("target-first share", (x) => pct((x.plan_quality || {}).target_first_share))}
    ${rowsOf("paper P&L（仅 filled，独立列）", (x) => num((x.paper || {}).realized_pnl_total, 2))}
  </tbody></table>`;
}
function groupedTables(groups, title) {
  const keys = Object.keys(groups || {});
  if (!keys.length) return "";
  return `<details class="ab-group"><summary>${esc(title)}</summary>
    ${keys.map((k) => `<h4 class="sec-title mono">${esc(k)}</h4>${metricsTable(groups[k], "all")}`).join("")}
  </details>`;
}
async function loadAB() {
  const el = $("#abBody");
  const preset = $("#abPreset").value;
  const paired = $("#abPaired").checked;
  let data;
  try {
    const qs = new URLSearchParams();
    if (preset) qs.set("preset", preset);
    if (paired) qs.set("paired_only", "true");
    data = await api(`/api/pipeline/ab?${qs}`);
  } catch (e) { el.innerHTML = emptyCard(`聚合计算失败：${e.message}`); return; }
  halted = !!data.halted; renderHalt();
  // Caveats render WITH the numbers, never buried (ledger contract).
  $("#abCaveats").innerHTML =
    `<h4 class="sec-title">统计注意事项（statistical caveats）</h4><ul>` +
    data.caveats.map((c) => `<li>${esc(c)}</li>`).join("") + `</ul>`;
  const presetSel = $("#abPreset");
  const known = new Set([...presetSel.options].map((o) => o.value));
  for (const p of data.presets || []) {
    if (!known.has(p)) presetSel.insertAdjacentHTML("beforeend", `<option>${esc(p)}</option>`);
  }
  if (!data.present) { el.innerHTML = emptyCard("账本为空（not yet run）"); return; }
  const agree = data.direction_agreement || {};
  el.innerHTML = `
    <div class="ab-summary mono">
      配对数（去重后的 (date, session, ticker) 最新完整 attempt）：${data.pair_count}
      · 剔除 eval=fail 后：${data.pair_count_excluding_fail}<br>
      direction agreement：${pct(agree.all)} · 剔除 fail：${pct(agree.excluding_fail)}
      · 使用行数：${data.row_counts.used}/${data.row_counts.ledger}${data.paired_only ? " · 仅配对行" : ""}
    </div>
    <h4 class="sec-title">全部行（all rows）</h4>
    ${metricsTable(data.arms, "all")}
    <h4 class="sec-title">剔除消费了 eval=fail 简报的行（warn 两侧都保留）</h4>
    ${metricsTable(data.arms, "excluding_fail")}
    ${groupedTables(data.by_ticker, "按 ticker 聚类（clustered per ticker）")}
    ${groupedTables(data.by_date, "按日期分层（stratified per date）")}`;
}

/* ---------------- page 5: decisions & orders ---------------- */
function execCell(ex) {
  ex = ex || { state: "none" };
  const map = { filled: "st-ok", submitted: "st-running", dry_run: "st-skipped", skipped: "st-warn", none: "st-skipped" };
  const extra = [];
  if (ex.broker_status) extra.push(esc(ex.broker_status));
  if (ex.filled_avg_price != null) extra.push(`@${esc(String(ex.filled_avg_price))}×${esc(String(ex.filled_qty ?? "?"))}`);
  if ((ex.skip_reasons || []).length) extra.push(esc(ex.skip_reasons.join("; ")));
  return `<span class="badge ${map[ex.state] || "st-unknown"}">${esc(ex.state)}</span> <span class="small">${extra.join(" · ")}</span>`;
}
function outcomeCell(out) {
  if (!out) return "—";
  const r = out.returns || {}, b = out.benchmark_returns || {};
  const h = (k) => (r[k] == null || b[k] == null) ? "—" : `${num(r[k] - b[k], 4)}`;
  const replay = out.plan_replay
    ? ` · replay:${out.plan_replay.ambiguous ? "ambiguous" : out.plan_replay.target_hit_first ? "target" : out.plan_replay.stop_hit_first ? "stop" : out.plan_replay.entry_hit ? "entry" : "no-entry"}`
    : "";
  return `<span class="mono small">exc d1 ${h("d1")} · d5 ${h("d5")} · d20 ${h("d20")}${replay}</span>`;
}
async function loadDecisions() {
  const el = $("#decisionsBody");
  const qs = new URLSearchParams();
  if ($("#fDate").value.trim()) qs.set("date", $("#fDate").value.trim());
  if ($("#fSession").value) qs.set("session", $("#fSession").value);
  if ($("#fTicker").value.trim()) qs.set("ticker", $("#fTicker").value.trim());
  if ($("#fArm").value) qs.set("arm", $("#fArm").value);
  let data;
  try { data = await api(`/api/pipeline/decisions?${qs}`); }
  catch (e) { el.innerHTML = emptyCard(`账本读取失败：${e.message}`); return; }
  halted = !!data.halted; renderHalt();
  if (!data.rows.length) {
    el.innerHTML = emptyCard(data.present ? "无匹配行" : "账本为空（not yet run）");
    return;
  }
  el.innerHTML = `<table class="ptable"><tbody>
    <tr><th>run_id</th><th>decision</th><th>plan</th><th>trigger</th><th>eval</th><th>execution</th><th>outcome</th></tr>
    ${data.rows.map((row) => {
      const plan = row.plan
        ? `${esc(String((row.plan.entry_zone || []).join("–")))} / stop ${esc(String(row.plan.stop ?? "—"))} / tgt ${esc(String((row.plan.targets || []).join(",")))}${row.plan_valid ? "" : " ⚠invalid"}`
        : "—";
      const decisionCls = { BUY: "st-ok", SELL: "st-failed", HOLD: "st-skipped", ERROR: "st-warn" }[row.decision] || "st-unknown";
      return `<tr>
        <td class="mono small">${esc(row.run_id || "")}</td>
        <td><span class="badge ${decisionCls}">${esc(row.decision || "?")}</span></td>
        <td class="mono small">${plan}</td>
        <td>${esc(row.trigger || "")}</td>
        <td>M:${verdictBadge(row.macro_eval_verdict)} T:${verdictBadge(row.ticker_eval_verdict)}</td>
        <td>${execCell(row.execution)}</td>
        <td>${outcomeCell(row.outcome)}</td>
      </tr>`;
    }).join("")}
  </tbody></table>`;
}
async function runTrigger() {
  const ticker = $("#trigTicker").value.trim().toUpperCase();
  const msg = $("#trigMsg");
  if (!ticker) { msg.textContent = "请输入 ticker"; return; }
  const body = { ticker };
  if ($("#trigDate").value) body.date = $("#trigDate").value;
  try {
    const r = await api("/api/pipeline/trigger", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    msg.textContent = `已启动：${r.argv.join(" ")}`;
    pollTrigger(r.trigger_id);
  } catch (e) { msg.textContent = `启动失败：${e.message}`; }
}
function pollTrigger(id) {
  const msg = $("#trigMsg");
  const timer = setInterval(async () => {
    try {
      const r = await api(`/api/pipeline/trigger/${id}`);
      if (r.status !== "running") {
        clearInterval(timer);
        msg.textContent = r.status === "done"
          ? `完成：${r.ticker} ${r.date}（结果见下方账本）`
          : `失败：${r.error || `exit ${r.returncode}`}`;
        loadDecisions();
      }
    } catch (e) { clearInterval(timer); msg.textContent = e.message; }
  }, 3000);
}

/* ---------------- tabs + init ---------------- */
const LOADERS = {
  status: loadStatus, pool: loadPool, briefs: loadBriefs, ab: loadAB, decisions: loadDecisions,
};
function showPage(page) {
  document.querySelectorAll(".pipeline-page").forEach((s) => { s.hidden = s.id !== `page-${page}`; });
  document.querySelectorAll("#pageTabs button").forEach((b) => {
    b.classList.toggle("active", b.dataset.page === page);
  });
  (LOADERS[page] || (() => {}))();
}
function init() {
  $("#pageTabs").addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (b) showPage(b.dataset.page);
  });
  $("#haltToggle").onclick = () => toggleHalt().catch((err) => window.alert(err.message));
  $("#poolSession").addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    document.querySelectorAll("#poolSession button").forEach((x) => x.classList.remove("active"));
    b.classList.add("active");
    poolSession = b.dataset.session;
    loadPool();
  });
  document.addEventListener("click", (e) => {
    const row = e.target.closest(".brief-row");
    if (row) openBrief(row.dataset.kind, row.dataset.name);
  });
  $("#abPreset").onchange = loadAB;
  $("#abPaired").onchange = loadAB;
  $("#fApply").onclick = loadDecisions;
  $("#trigBtn").onclick = runTrigger;
  refreshHalt();
  setInterval(refreshHalt, 30000);
  showPage("status");
}
init();
