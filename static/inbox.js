"use strict";
// Merchant inbox. Cards show the five facts so a one-tap Approve is never blind.
// Buyer text is untrusted: it is only ever placed with textContent.

function card(v) {
  const c = el("article", { class: "card", "data-attention": String(v.attention) });
  c.append(el("span", { class: "stage", "data-stage": v.stage, text: v.stage_label }));
  c.append(el("p", { class: "headline", text: v.headline }));
  if (v.facts && v.facts.length && (v.actions.length || v.stage === "PENDING_APPROVAL")) {
    const dl = el("dl");
    for (const f of v.facts) {
      dl.append(el("dt", { text: f.label }));
      const dd = el("dd");
      if (f.untrusted) dd.append(el("blockquote", { text: f.value })); else dd.textContent = f.value;
      dl.append(dd);
    }
    c.append(dl);
  } else {
    c.append(el("blockquote", { text: v.quote }));
  }
  for (const f of v.fx || []) c.append(el("p", { class: "note", text: f.label }));       // a figure in another currency, counted in dollars at the demo rate
  if (v.hold) {
    c.append(el("p", { class: "hold-headline", text: v.hold.headline }));
    if (v.hold.clause) c.append(el("blockquote", { text: v.hold.clause }));
    if (v.hold.model_verdict) c.append(el("p", { class: "note", text: t("hold.model_label") + ": " + v.hold.model_verdict + ((v.hold.flags || []).length ? " (" + v.hold.flags.join(", ") + ")" : "") + (v.hold.model_reason ? " - " + v.hold.model_reason : "") }));
  }
  for (const f of (v.evidence_flags || [])) c.append(el("p", { class: "flag", role: "note", text: t("text.flag." + f) }));
  if (v.helper_note) { c.append(el("p", { class: "note", text: t("inbox.helper_note") + (v.reader ? " · " + t("desk.reader_label", { name: v.reader.label || v.reader.source }) : "") }), el("blockquote", { text: v.helper_note })); }
  for (const f of (v.flags || [])) c.append(el("p", { class: "flag", text: f }));
  if (v.amount_note && v.actions.length) c.append(el("p", { class: "note", text: v.amount_note }));
  if (v.approval) c.append(el("p", { class: "note", text: t("inbox.expires", { when: new Date(v.approval.expires_at).toLocaleString() }) }));

  const act = el("div", { class: "actions" });
  let note = null;
  if (v.actions.includes("approve") || v.actions.includes("deny")) {
    const id = "note-" + v.request_id;
    note = el("input", { type: "text", id, maxlength: "280" });
    if (v.approval && v.approval.stale) c.append(el("p", { class: "flag", text: t("desk.owner_stale") }));
    c.append(el("label", { for: id, text: t("inbox.note_label") }), note);
    if (v.actions.includes("approve")) {
      act.append(el("button", { type: "button", text: t("inbox.approve"), onclick: (e) => run(e.target, v, "approve", note) }));
    }
    act.append(el("button", { type: "button", class: "deny", text: t("inbox.deny"), onclick: (e) => run(e.target, v, "deny", note) }));
  }
  if (v.actions.includes("recheck")) {
    act.append(el("button", { type: "button", class: "secondary", text: t("inbox.recheck"), onclick: (e) => run(e.target, v, "recheck") }));
  }
  if (v.actions.includes("reconcile")) {
    act.append(el("button", { type: "button", class: "secondary", text: t("inbox.reconcile"), onclick: (e) => run(e.target, v, "reconcile") }));
  }
  if (v.actions.includes("retry")) {
    act.append(el("button", { type: "button", class: "secondary", text: t("inbox.retry"), onclick: (e) => run(e.target, v, "retry") }));
    c.append(el("p", { class: "note", text: t("inbox.retry_explain") }));
  }
  if (v.actions.includes("approve")) act.append(el("a", { class: "button secondary", href: "/approve.html?id=" + encodeURIComponent(v.request_id), text: t("inbox.open_card") }));
  if (v.receipt_id) act.append(el("a", { class: "button secondary", href: "/receipt.html?id=" + encodeURIComponent(v.receipt_id), text: t("inbox.receipt") }));
  c.append(act);
  return c;
}

async function run(btn, v, action, noteEl) {
  const row = btn.parentElement.querySelectorAll("button");
  row.forEach((b) => { b.disabled = true; });
  const toast = document.getElementById("toast");
  toast.textContent = t("inbox.working");
  try {
    const body = { note: noteEl ? noteEl.value : "", intent_id: v.intent_id, amount_minor: v.amount_minor };   // the exact binding shown on the card
    if (action === "approve") { body.nonce = v.approval.nonce; body.digest = v.approval.digest; }
    await api("/api/requests/" + encodeURIComponent(v.request_id) + "/" + action, "POST", body);
    toast.textContent = t("inbox.recorded");
  } catch (e) {
    toast.textContent = errMessage(e);
  }
  await load();
}

async function load() {
  const d = await api("/api/inbox", "GET");
  document.getElementById("rule").textContent = d.rule_sentence;
  document.getElementById("summary").textContent = t("inbox.summary", { waiting: d.waiting, sent_count: d.sent_count, sent_total: d.sent_total });
  const list = document.getElementById("list");
  list.replaceChildren(...d.items.map(card));
  document.getElementById("empty").hidden = d.items.length > 0;
}

(async () => {
  await boot();
  await ensureSession(() => load());
})();
