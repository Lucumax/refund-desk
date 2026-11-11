"use strict";
// The owner's phone card, a decision packet read top to bottom: the amount and what is being approved (summary), why it
// waits, what the buyer wrote, what the yes covers and until when, then Approve and Deny, then the audit strip.
// Buyer text is untrusted and only ever placed with textContent.
const $ = (id) => document.getElementById(id);
const ID = new URLSearchParams(location.search).get("id") || "";

function nav(...kids) { return el("div", { class: "actions" }, ...kids); }

// Why it waits, from the signed decision: our sentence first, then the untrusted words (placed with textContent only).
function holdEls(h) {
  if (!h) return [];
  const out = [el("p", { class: "hold-headline", text: h.headline })];
  if (h.clause) out.push(el("p", { class: "note", text: t("hold.clause_label") }), el("blockquote", { text: h.clause }));
  if (h.model_verdict) {
    out.push(el("p", { class: "note", text: t("hold.model_label") + ": " + h.model_verdict + ((h.flags || []).length ? " (" + t("hold.flags_label") + ": " + h.flags.join(", ") + ")" : "") }));
    if (h.model_reason) out.push(el("blockquote", { text: h.model_reason }));
  }
  return out;
}

function paintDone(v) {
  // after a decision the server names the next request that waits (v.next_waiting): one tap to it
  const next = v.next_waiting
    ? el("a", { class: "button", id: "next", href: "/approve.html?id=" + encodeURIComponent(v.next_waiting), text: t("approve.next") })
    : (v.next_waiting === null ? el("p", { class: "note", text: t("approve.next_none") }) : null);
  $("card").replaceChildren(stampEl(v.stamp), next,
    nav(el("a", { class: "button secondary", href: "/inbox.html", text: t("approve.back") }),
      v.receipt_id ? el("a", { class: "button secondary", href: "/receipt.html?id=" + encodeURIComponent(v.receipt_id), text: t("inbox.receipt") }) : null));
}

function paint(v) {
  const r = v.reader;
  const waiting = v.approval && v.actions && (v.actions.includes("approve") || v.actions.includes("deny"));
  if (!waiting) {
    if (v.stamp) paintDone(v);
    else $("card").replaceChildren(el("p", { text: t("approve.nothing") }));
    return;
  }
  const note = el("input", { type: "text", id: "owner-note", maxlength: "280" });
  const why = [el("h2", { text: t("approve.why") }), el("p", { text: v.reason }), ...holdEls(v.hold)];
  const buyer = [el("h2", { text: t("panel.buyer_title") }), el("blockquote", { text: v.quote }),
    ...(v.fx || []).map((f) => el("p", { class: "note", text: f.label })),
    ...(v.evidence_flags || []).map((f) => el("p", { class: "flag", role: "note", text: t("text.flag." + f) })),
    el("p", { class: "note", text: t("desk.buyer_line", { id: v.request_id, order: v.order_ref }) })];
  const suggested = r && r.action === "REFUND"
    ? [el("p", { class: "note", text: t("panel.suggest_title") + ": " + t("arena.reader_proposes", { amount: money(r.amount_minor),
      reason: r.reason_code ? t("issue." + r.reason_code) : t("arena.reader_no_reason") }) }),
    el("p", { class: (r.notes || []).includes("MODEL_OUTPUT_REJECTED") ? "flag" : "note", text: t("desk.reader_label", { name: r.label || r.source }) })] : [];
  const approveText = CHIPS.world === "sim" ? t("approve.button_sim", { amount: v.amount }) : t("inbox.approve") + " " + v.amount;
  $("card").replaceChildren(
    el("div", { class: "approve-card" },
      el("h1", { text: t("approve.title") }),
      el("p", { class: "approve-amount", text: v.amount }),
      approvalSummary(v)),
    el("div", { class: "card" }, ...why, ...suggested),
    el("div", { class: "card" }, ...buyer),
    el("div", { class: "approve-card decide" },
      el("p", { class: "bound", text: t("approve.binding", { amount: v.amount, order: v.order_ref, version: v.approval.policy_version || "?" }) }),
      el("p", { class: "expires", text: t("inbox.expires", { when: new Date(v.approval.expires_at).toLocaleString() }) }),
      el("label", { for: "owner-note", text: t("inbox.note_label") }), note,
      el("div", { class: "actions" },
        v.actions.includes("approve") ? el("button", { type: "button", id: "approve", text: approveText, onclick: (e) => decide(e.target, v, "approve", note) }) : null,
        v.actions.includes("deny") ? el("button", { type: "button", id: "deny", class: "deny", text: t("inbox.deny"), onclick: (e) => decide(e.target, v, "deny", note) }) : null),
      auditStrip(v, CHIPS.world)),
    el("p", {}, el("a", { href: "/desk.html", text: t("approve.desk") })));
}

async function decide(btn, v, action, noteEl) {
  document.querySelectorAll("#card button").forEach((b) => { b.disabled = true; });
  try {
    const body = { note: noteEl ? noteEl.value : "", intent_id: v.intent_id, amount_minor: v.amount_minor };   // the exact binding shown on the card
    if (action === "approve") { body.nonce = v.approval.nonce; body.digest = v.approval.digest; }
    paint(await api("/api/requests/" + encodeURIComponent(v.request_id) + "/" + action, "POST", body));
  } catch (e) {
    $("card").prepend(el("p", { class: "flag", role: "alert", text: errMessage(e) }));
    document.querySelectorAll("#card button").forEach((b) => { b.disabled = false; });
  }
}

async function load() {
  if (!ID) { $("card").replaceChildren(el("p", { text: t("approve.missing") })); return; }
  try { paint(await api("/api/requests/" + encodeURIComponent(ID), "GET")); }
  catch (e) { $("card").replaceChildren(el("p", { text: t("approve.missing") })); }
}

(async () => {
  await boot();
  await ensureSession(() => load());
})();
