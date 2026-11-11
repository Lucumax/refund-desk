"use strict";
const $ = (id) => document.getElementById(id);
let STATE = null;
let PRESET = null;

function renderScore(sc) {
  $("tiles").replaceChildren(...scoreTiles(sc));
  $("score").replaceChildren(...scoreCards(sc));
}

function renderBudget(b) {
  $("budget").replaceChildren(
    el("p", { class: "headline", text: t("arena.budget_line", { cap: money(b.per_order_allowance_minor), daily: money(b.daily_budget_minor) }) }),
    el("p", { class: "note", text: t("arena.budget_used", { used: money(b.daily_automatic_minor), daily: money(b.daily_budget_minor) }) }),
    el("p", { class: "note", text: t("arena.budget_note") }));
}

function renderPolicy(p) {
  $("policy").replaceChildren(
    el("p", { class: "headline", text: t("arena.policy_version", { version: p.version, hash: shortHash(p.hash) }) }),
    el("p", { text: t("arena.policy_summary", { cap: money(p.per_order_cap_minor), ceiling: money(p.escalate_ceiling_minor),
      daily: money(p.daily_budget_minor), evidence: t(p.evidence_required ? "policy.yes" : "policy.no") }) }),
    el("p", { class: "note", text: p.eligible_reasons.map((r) => t("issue." + r)).join(", ") }),
    el("a", { class: "button secondary", href: "/policy.html", text: t("arena.policy_edit") }));
}

function renderRecent(list) {
  if (!list.length) { $("recent").replaceChildren(el("li", { class: "note", text: t("arena.recent_empty") })); return; }
  $("recent").replaceChildren(...list.map((a) => el("li", {}, el("button", { type: "button", class: "secondary preset",
    onclick: () => openAttempt(a.id) },
  el("span", { class: "decision small", "data-d": a.decision, text: a.decision }),
  el("span", { text: "  " + t("arena.recent_line", { id: a.id, order: a.order, decision: a.decision, rule: a.rule_id }) })))));
}

function renderOrders(orders) {
  const keep = $("order").value;
  $("order").replaceChildren(...orders.map((o) => el("option", { value: o.order_ref,
    text: t("arena.order_option", { order: o.order_ref, item: o.item, total: money(o.total_minor), remaining: money(o.remaining_minor) }) })));
  if (keep) $("order").value = keep;
}

function presetButton(a) {
  return el("button", { type: "button", class: "secondary preset", "data-id": a.id, "aria-pressed": "false",
    onclick: () => loadPreset(a) }, el("span", { class: "mono", text: a.id }), el("span", { text: "  " + a.title }));
}

function renderLibrary(p) {
  const parts = [];
  for (const cat of p.categories) {
    parts.push(el("h3", { text: t("arena.cat." + cat) }));
    for (const a of p.attacks.filter((x) => x.category === cat)) parts.push(presetButton(a));
  }
  parts.push(el("h3", { text: t("arena.controls_title") }));
  for (const c of p.controls) parts.push(presetButton(c));
  $("library").replaceChildren(...parts);
}

function markPreset(id) {
  document.querySelectorAll("button.preset[data-id]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.id === id)));
}

function loadPreset(a) {
  PRESET = a.id;
  $("text").value = a.text;
  $("order").value = a.order;
  markPreset(a.id);
}

function paint(s) {
  STATE = s;
  $("reader-line").textContent = t("arena.reader_in_use", { name: s.reader.name });
  setReaderChip(s.reader.name);
  renderScore(s.scoreboard); renderBudget(s.budget); renderPolicy(s.policy); renderRecent(s.recent);
  renderOrders(s.orders);
}

function valueText(key, v) {
  if (v === null || v === undefined) return t("arena.diff.dash");
  if (key === "amount") return typeof v === "string" ? t("arena.act." + v) : money(v);
  if (key === "reason") return t("issue." + v);
  return t("arena.act." + v);
}

function showAttempt(a) {
  const p = a.reader.proposal, g = a.gate;
  const fooled = g.anomaly && p.action === "REFUND" && (p.amount_minor || 0) > 0 && g.decision !== "ALLOW";
  const readerBody = [el("p", { class: "note", text: t("arena.reader_in_use", { name: a.reader.name }) })];
  if (p.action === "REFUND") {
    readerBody.push(el("p", { class: "headline", text: t("arena.reader_proposes", { amount: money(p.amount_minor),
      reason: p.reason_code ? t("issue." + p.reason_code) : t("arena.reader_no_reason") }) }));
  } else readerBody.push(el("p", { class: "headline", text: t("arena.reader_" + p.action) }));
  const quotes = Object.values(p.quotes || {});
  if (quotes.length) {
    readerBody.push(el("p", { class: "note", text: t("arena.reader_quote") + ": " + quotes.map((q) => "“" + q + "”").join("  ") }));
  }
  if (fooled) readerBody.push(el("p", { class: "badge", text: t("arena.fooled_badge") }));

  const decidedBody = [
    el("p", { class: "headline", text: t("arena.rule." + g.rule_id) }),
    el("p", { class: "note", text: t("arena.gloss." + g.decision) }),
  ];

  const flagBody = [el("h3", { text: t("arena.flags_title") })];
  if (!g.flags.length) flagBody.push(el("p", { class: "note", text: t("arena.flags_none") }));
  for (const f of g.flags) {
    flagBody.push(el("p", {}, el("strong", { text: f.rule + " " + t("arena.flag." + f.category) + (f.obfuscated ? " (" + t("arena.flag.obfuscated") + ")" : "") + ": " }),
      el("span", { class: "mono", text: "“" + f.phrase + "”" })));
  }
  if (g.flags.length) {
    flagBody.push(el("p", { class: a.without_flags.decision_changed ? "flag" : "note", text: a.without_flags.decision_changed
      ? t("arena.flag_changed", { decision: a.without_flags.decision, rule: a.without_flags.rule_id }) : t("arena.flag_same") }));
  }

  const rows = a.diff.map((r) => el("tr", {}, el("th", { scope: "row", text: t("arena.diff." + r.key) }),
    el("td", { text: valueText(r.key, r.proposed) }),
    el("td", { class: r.changed ? "cut" : "kept", text: valueText(r.key, r.allowed) + (r.changed ? " (" + t("arena.diff.cut") + ")" : " (" + t("arena.diff.kept") + ")") })));
  const diff = el("table", {}, el("thead", {}, el("tr", {}, el("th", { text: t("arena.diff.field") }), el("th", { text: t("arena.diff.proposed") }), el("th", { text: t("arena.diff.allowed") }))),
    el("tbody", {}, ...rows));

  const seen = g.trace.filter((x) => x.result !== "NOT_REACHED");
  const later = g.trace.length - seen.length;
  const trace = el("ul", { class: "chips" }, ...seen.map((x) => el("li", { class: "chip", "data-r": x.result, "data-d": g.decision, text: x.rule + " " + t("arena.trace." + x.result) })),
    later ? el("li", { class: "chip", text: t("arena.trace.more", { n: later }) }) : null);

  const rc = a.receipt;
  const receipt = el("div", {},
    el("h3", { text: t("arena.receipt_title") }),
    el("p", { class: "headline mono", text: t("arena.receipt_line", { id: rc.id, hash: shortHash(rc.hash) }) }),
    el("p", { class: "note", text: t(rc.signature_ok ? "arena.receipt_sig_ok" : "arena.receipt_sig_bad") }),
    el("div", { class: "actions" },
      el("button", { type: "button", class: "secondary", onclick: verifyChain, text: t("arena.verify_button") }),
      el("a", { class: "button secondary", href: "/court.html?id=" + encodeURIComponent(a.id), text: t("arena.court_link") })),
    el("div", { id: "verify-result", class: "result", hidden: "", role: "status", "aria-live": "polite" }));

  $("result").replaceChildren(
    stampEl(a.stamp),
    el("section", { class: "card buyer", "aria-label": t("panel.buyer_title") }, el("h3", { text: t("panel.buyer_title") }), el("blockquote", { text: a.text }),
      ...(a.fx || []).map((f) => el("p", { class: "note", text: f.label }))),
    el("div", { class: "two" }, sidePanel("suggested", ...readerBody), sidePanel("decided", ...decidedBody)),
    el("details", { class: "disclosure" }, el("summary", { text: t("arena.details") }),
      el("h3", { text: t("arena.diff_title") }), diff, el("h3", { text: t("arena.trace_title") }), trace, ...flagBody, receipt));
  $("result").hidden = false;
  $("result-empty").hidden = true;
  if (a.scoreboard) { renderScore(a.scoreboard); renderBudget(a.budget); }
}

async function verifyChain() {
  const box = $("verify-result");
  box.hidden = false; box.className = "result"; box.textContent = t("verify.running");
  const v = await api("/api/arena/verify", "GET");
  box.className = "result " + (v.ok ? "good" : "bad");
  box.textContent = v.ok ? t("verify.intact", { records: v.records })
    : t("verify.broken", { record: v.first_broken_id || "?", reason: t("verify.code." + v.reason_code) });
}

async function refresh() { paint(await api("/api/arena/state", "GET")); }

async function openAttempt(id) { showAttempt(await api("/api/arena/attempts/" + encodeURIComponent(id), "GET")); }

async function send() {
  const btn = $("send"), msg = $("msg");
  msg.textContent = ""; btn.disabled = true; btn.textContent = t("arena.sending");
  try {
    const a = await api("/api/arena/attempt", "POST", { text: $("text").value, order: $("order").value, preset: PRESET || undefined });
    showAttempt(a);
    await refresh();
  } catch (e) { msg.textContent = errMessage(e); }
  btn.disabled = false; btn.textContent = t("arena.send");
}

(async () => {
  await boot();
  setBanner("sim", t("banner.arena"));      // the arena always uses the simulated PayPal, even when the desk runs on a sandbox
  await refresh();
  renderLibrary(STATE.presets);
  $("send").addEventListener("click", send);
  $("text").addEventListener("input", () => { PRESET = null; markPreset(null); });
  const q = new URLSearchParams(location.search);
  if (q.get("preset")) {
    const a = STATE.presets.attacks.concat(STATE.presets.controls).find((x) => x.id === q.get("preset"));
    if (a) loadPreset(a);
  }
  if (q.get("show")) { try { await openAttempt(q.get("show")); } catch (e) { /* unknown id: stay empty */ } }
})();
