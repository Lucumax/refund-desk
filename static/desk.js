"use strict";
// The Refund Desk: one screen. A buyer message in, then the verdict stamp, then what the AI suggested kept apart from
// what the code decided, the owner's approve or deny, PayPal's answer and the read-back. Buyer text is untrusted:
// it is only ever placed with textContent (through el()). No sentence lives in this file.
const $ = (id) => document.getElementById(id);
let ST = null;
let CURRENT = null;      // request id on show
let READBACK = null;     // last read-back for CURRENT

function row(label, value) {
  return el("p", {}, el("strong", { text: label + ": " }), el("span", { text: value }));
}

function scenarioButton(sc) {
  return el("button", { type: "button", class: "secondary preset", "data-id": sc.id,
    onclick: () => runScenario(sc) }, el("span", { text: sc.title }),
  el("span", { class: "mono", text: "  " + sc.order }));
}

function renderScenarios(list, faults) {
  for (const kind of ["legit", "hostile", "compromised"]) {
    $("sc-" + kind).replaceChildren(...list.filter((s) => s.kind === kind).map(scenarioButton));
  }
  const has = !!(faults && faults.length);
  $("h-faults").hidden = !has;
  $("n-faults").hidden = !has;
  $("sc-faults").replaceChildren(...(has ? faults.map(scenarioButton) : []));
}

function renderOrders(orders) {
  const keep = $("order").value;
  $("order").replaceChildren(...orders.map((o) => el("option", { value: o.order_ref,
    text: t("arena.order_option", { order: o.order_ref, item: o.item, total: money(o.total_minor), remaining: money(o.remaining_minor) }) })));
  if (keep) $("order").value = keep;
}

function renderScore(s) {
  $("tiles").replaceChildren(...scoreTiles(s.scoreboard));
  $("score").replaceChildren(...scoreCards(s.scoreboard, { sent: s.refund_requests_sent, with_id: s.refunds_with_id }));
}

function renderRecent(list) {
  if (!list.length) { $("recent").replaceChildren(el("li", { class: "note", text: t("desk.recent_empty") })); return; }
  $("recent").replaceChildren(...list.map((v) => el("li", {}, el("button", { type: "button", class: "secondary preset",
    onclick: () => show(v.request_id) }, el("span", { class: "stage", "data-stage": v.stage, text: v.stage_label }),
  el("span", { text: "  " + v.request_id + " " + (v.order_ref || "") + (v.amount ? " " + v.amount : "") })))));
}

function paintState(s) {
  ST = s;
  $("rule-line").textContent = s.rule_sentence;
  $("enforced-line").textContent = t("desk.enforced_line", { version: s.enforced.mandate_version, cap: money(s.enforced.automatic_allowance_minor),
    daily: money(s.enforced.daily_budget_minor), ceiling: money(s.enforced.approve_ceiling_minor) });
  $("mode-line").textContent = t(s.provider === "fake" ? "desk.mode_fake" : "desk.mode_sandbox", { reader: s.reader.configured });
  setReaderChip(s.reader.configured);
  renderScenarios(s.scenarios, s.faults);
  renderOrders(s.orders);
  renderScore(s);
  renderRecent(s.requests);
}

// ---- one request: the stamp first, then what the AI suggested kept apart from what the code decided ---------------
function suggestedPanel(v) {
  const r = v.reader;
  if (!r) return sidePanel("suggested", el("p", { class: "note", text: t("desk.reader_none") }));
  const body = [el("p", { class: (r.notes || []).includes("MODEL_OUTPUT_REJECTED") ? "flag" : "note", text: t("desk.reader_label", { name: r.label || r.source }) })];
  if (r.action === "REFUND") {
    body.push(el("p", { class: "headline", text: t("arena.reader_proposes", { amount: money(r.amount_minor),
      reason: r.reason_code ? t("issue." + r.reason_code) : t("arena.reader_no_reason") }) }));
  } else body.push(el("p", { class: "headline", text: t("arena.reader_" + r.action) }));
  const quotes = Object.values(r.quotes || {});
  if (quotes.length) body.push(el("p", { class: "note", text: t("arena.reader_quote") + ": " + quotes.map((q) => "“" + q + "”").join("  ") }));
  if (r.explanation) body.push(el("p", { class: "note", text: t("desk.reader_summary") + ": " + r.explanation }));
  body.push(el("p", { class: "note", text: t("desk.reader_untrusted") }));
  return sidePanel("suggested", ...body);
}

function decidedPanel(v) {
  const g = v.gate;
  const body = [el("p", { class: "headline", text: v.reason || v.headline })];
  if (g) {
    body.push(el("p", { class: "note", text: t("desk.route." + g.route) }));
    body.push(el("p", { class: "note", text: t("desk.policy_version", { version: g.policy_version }) }));
  }
  return sidePanel("decided", ...body);
}

// Everything the stamp leaves out: the rules checked in order, the signals the scan found, how the model was called.
function traceDetails(v) {
  const g = v.gate, r = v.reader;
  const kids = [];
  if (g) {
    const chips = g.trace.filter((x) => x.result !== "NOT_REACHED");
    const later = g.trace.length - chips.length;
    kids.push(el("ul", { class: "chips" }, ...chips.map((x) => el("li", { class: "chip", "data-r": x.result, "data-d": g.decision, text: x.rule + " " + t("arena.trace." + x.result) })),
      later ? el("li", { class: "chip", text: t("arena.trace.more", { n: later }) }) : null));
    for (const f of g.flags) {
      kids.push(el("p", {}, el("strong", { text: f.rule + " " + f.label + ": " }), el("span", { class: "mono", text: "“" + f.phrase + "”" })));
    }
  }
  if (r && r.model_exchange) {
    kids.push(el("p", { class: "note", text: t("desk.exchange", { verdict: r.model_exchange.verdict,
      sep: t(r.model_exchange.instructions_separate ? "desk.sep_yes" : "desk.sep_no") }) }));
  }
  if (!kids.length) return null;
  return el("details", { class: "disclosure" }, el("summary", { text: t("desk.trace_summary") }), ...kids);
}

function ownerCard(v) {
  const body = [];
  if (v.approval && v.approval.stale) {
    const note = el("input", { type: "text", id: "owner-note", maxlength: "280" });
    body.push(el("p", { class: "flag", text: t("desk.owner_stale") }));
    body.push(el("label", { for: "owner-note", text: t("inbox.note_label") }), note);
    body.push(el("div", { class: "actions" },
      el("button", { type: "button", class: "deny", onclick: (e) => decide(e.target, v, "deny", note), text: t("inbox.deny") })));
  } else if (v.actions && v.actions.includes("approve")) {
    const note = el("input", { type: "text", id: "owner-note", maxlength: "280" });
    body.push(el("p", { class: "headline", text: t("desk.owner_waiting", { amount: v.amount }) }));
    body.push(approvalSummary(v));
    body.push(el("p", { class: "note", text: t("desk.owner_bound", { amount: v.amount, order: v.order_ref }) }));
    body.push(el("label", { for: "owner-note", text: t("inbox.note_label") }), note);
    if (v.approval) body.push(el("p", { class: "note", text: t("inbox.expires", { when: new Date(v.approval.expires_at).toLocaleString() }) }));
    if (v.approval && v.approval.policy_version) body.push(el("p", { class: "note", text: t("desk.owner_policy_version", { version: v.approval.policy_version }) }));
    body.push(el("div", { class: "actions" },
      el("button", { type: "button", onclick: (e) => decide(e.target, v, "approve", note), text: t("inbox.approve") }),
      el("button", { type: "button", class: "deny", onclick: (e) => decide(e.target, v, "deny", note), text: t("inbox.deny") })));
    body.push(el("p", {}, el("a", { href: "/approve.html?id=" + encodeURIComponent(v.request_id), text: t("inbox.open_card") })));
  } else return null;
  return el("section", { class: "card owner", "aria-label": t("desk.owner_title") }, el("h3", { text: t("desk.owner_title") }), ...body);
}

function providerCard(v) {
  const body = [];
  if (v.refund_id) {
    body.push(row(t("desk.refund_id"), v.refund_id));
    body.push(row(t("desk.provider_status"), v.provider_status ? t("provider." + v.provider_status) : t("provider.UNKNOWN")));
    body.push(el("div", { class: "actions" }, el("button", { type: "button", class: "secondary", onclick: doReadback, text: t("desk.readback_button") })));
    if (READBACK && READBACK.request_id === v.request_id) body.push(readbackBlock(READBACK));
  }
  if (v.unresolved) body.push(unresolvedBlock(v));
  if (v.receipt_id) {
    body.push(el("div", { class: "actions" }, el("a", { class: "button secondary", href: "/receipt.html?id=" + encodeURIComponent(v.receipt_id), text: t("inbox.receipt") })));
  }
  if (!body.length) return null;
  return el("section", { class: "card", "aria-label": t("desk.provider_title") }, el("h3", { text: t("desk.provider_title") }), ...body);
}

// UNKNOWN: the request was sent and no usable answer came back. The desk says so plainly, shows that the money is
// held and that a repeat is blocked, and offers one action: re-check PayPal's record. It never offers a re-send.
function unresolvedBlock(v) {
  const kids = [
    el("h4", { text: t("desk.unknown_title") }),
    el("p", { text: t("desk.unknown_body") }),
    el("p", { class: "note", text: t("desk.unknown_held", { amount: v.amount, order: v.order_ref, sent: v.requests_sent }) }),
    el("p", { class: "note", text: t("desk.unknown_repeat_blocked") }),
  ];
  if (v.actions && v.actions.includes("recheck")) {
    kids.push(el("div", { class: "actions" }, el("button", { type: "button", id: "recheck", onclick: (e) => doRecheck(e.target), text: t("desk.recheck_button") })));
  }
  if (v.recheck) {
    kids.push(el("p", { class: "flag", text: t("desk.recheck." + v.recheck.result) }));
    kids.push(el("p", { class: "note", text: t("desk.recheck_at", { when: new Date(v.recheck.checked_at).toLocaleString() }) }));
  }
  return el("div", { class: "result bad", id: "unresolved" }, ...kids);
}

function readbackBlock(rb) {
  const parts = [el("p", { class: "note", text: rb.label })];
  if (!rb.checks || !rb.checks.length) {
    parts.push(el("p", { class: "flag", text: t("desk.readback_failed", { reason: rb.reason || "" }) }));
  } else {
    parts.push(el("p", { class: rb.ok ? "headline" : "flag", text: t(rb.ok ? "desk.readback_ok" : "desk.readback_bad") }));
    parts.push(el("ul", { class: "chips" }, ...rb.checks.map((c) => el("li", { class: "chip", "data-r": c.ok ? "PASS" : "HIT",
      text: t("desk.check." + c.id) + " " + t(c.ok ? "desk.check_ok" : "desk.check_bad") }))));
  }
  if (rb.capture) {
    parts.push(el("p", { class: rb.capture.ok ? "note" : "flag", text: t(rb.capture.ok ? "desk.capture_ok" : "desk.capture_bad",
      { confirmed: money(rb.capture.confirmed_refunds_minor), captured: money(rb.capture.captured_minor) }) }));
  }
  return el("div", { class: "result " + (rb.ok ? "good" : "bad") }, ...parts);
}

function renderFlow(v) {
  CURRENT = v.request_id;
  $("flow").replaceChildren(...[
    stampEl(v.stamp),
    el("section", { class: "card buyer", "aria-label": t("panel.buyer_title") }, el("h3", { text: t("panel.buyer_title") }),
      el("blockquote", { text: v.quote }), ...(v.fx || []).map((f) => el("p", { class: "note", text: f.label })),
      ...(v.evidence_flags || []).map((f) => el("p", { class: "flag", role: "note", text: t("text.flag." + f) })),
      el("p", { class: "note", text: t("desk.buyer_line", { id: v.request_id, order: v.order_ref }) })),
    el("div", { class: "two" }, suggestedPanel(v), decidedPanel(v)),
    ownerCard(v),
    auditStrip(v, CHIPS.world),
    providerCard(v),
    traceDetails(v)].filter(Boolean));
  $("flow").hidden = false;
  $("flow-empty").hidden = true;
}

// ---- actions ------------------------------------------------------------------------------------
async function refresh() { paintState(await api("/api/desk/state", "GET")); }

async function show(requestId, keepReadback) {
  if (!keepReadback) READBACK = null;
  renderFlow(await api("/api/requests/" + encodeURIComponent(requestId), "GET"));
}

// After a send the verdict is the point of the screen. On a phone it sits above the picker, so bring it into view.
function focusVerdict() {
  const calm = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const f = $("flow");
  if (f && f.scrollIntoView) f.scrollIntoView({ block: "start", behavior: calm ? "auto" : "smooth" });
}

async function send(body) {
  const msg = $("msg");
  msg.textContent = "";
  document.querySelectorAll("#app button").forEach((b) => { b.disabled = true; });
  try {
    const r = await api("/api/desk/send", "POST", body);
    READBACK = null;
    renderFlow(r.request);
    focusVerdict();
    await refresh();
  } catch (e) { msg.textContent = errMessage(e); }
  document.querySelectorAll("#app button").forEach((b) => { b.disabled = false; });
}

function runScenario(sc) { return send({ scenario: sc.id }); }

async function decide(btn, v, action, noteEl) {
  btn.parentElement.querySelectorAll("button").forEach((b) => { b.disabled = true; });
  try {
    const body = { note: noteEl ? noteEl.value : "", intent_id: v.intent_id, amount_minor: v.amount_minor };   // the exact binding shown on the card
    if (action === "approve") { body.nonce = v.approval.nonce; body.digest = v.approval.digest; }
    renderFlow(await api("/api/requests/" + encodeURIComponent(v.request_id) + "/" + action, "POST", body));
  } catch (e) {
    $("msg").textContent = errMessage(e);
    await show(v.request_id);
  }
  await refresh();
}

async function doRecheck(btn) {
  btn.disabled = true;
  btn.textContent = t("desk.recheck_working");
  try {
    await api("/api/requests/" + encodeURIComponent(CURRENT) + "/recheck", "POST", {});
  } catch (e) { $("msg").textContent = errMessage(e); }
  await show(CURRENT, true).catch(() => {});
  await refresh();
}

async function doReadback() {
  try {
    const rb = await api("/api/desk/readback/" + encodeURIComponent(CURRENT), "POST", {});
    READBACK = { ...rb, request_id: CURRENT };
  } catch (e) { READBACK = { ok: false, checks: [], reason: e.code || "GENERIC", label: "", request_id: CURRENT }; }
  await show(CURRENT, true).catch(() => {});
}

(async () => {
  await boot();
  await ensureSession(async () => {
    await refresh();
    $("send").addEventListener("click", () => send({ order: $("order").value, text: $("text").value }));
  });
})();
