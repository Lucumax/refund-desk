"use strict";
// Shared helpers. No user-facing text lives in this file: every sentence comes from
// /api/strings (copy/strings.draft.json). Untrusted text is only ever set with textContent.
let STR = {};
let CSRF = "";

function t(key, vars) {
  let s = Object.prototype.hasOwnProperty.call(STR, key) ? STR[key] : key;
  if (vars) s = s.replace(/\{(\w+)\}/g, (m, k) => (vars[k] !== undefined ? vars[k] : m));
  return s;
}

function el(tag, attrs, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === "text") e.textContent = v;
    else if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v);
  }
  for (const c of kids) if (c) e.append(c);
  return e;
}

function applyStrings(root) {
  root.querySelectorAll("[data-s]").forEach((n) => { n.textContent = t(n.dataset.s); });
}

async function api(path, method, body) {
  const headers = { "Content-Type": "application/json" };
  if (method === "POST") headers["X-CSRF-Token"] = CSRF;
  const r = await fetch(path, { method: method || "GET", headers, body: body ? JSON.stringify(body) : undefined, credentials: "same-origin" });
  let data = {};
  try { data = await r.json(); } catch (e) { /* empty body */ }
  if (!r.ok) { const err = new Error(data.error || "GENERIC"); err.code = data.error || "GENERIC"; err.status = r.status; err.data = data; throw err; }
  return data;
}

function errMessage(e) {
  const k = "errors." + (e.code || "GENERIC");
  return STR[k] ? STR[k] : t("errors.GENERIC");
}

// The chip strip says which world, which run and which reader a screen's data comes from. It is real text on
// every page. `world` is "sim" or "sandbox"; `reader` is optional and set once a page knows it.
const CHIPS = { world: "sim", reader: null, sentence: null };
function renderChips() {
  const b = document.getElementById("banners");
  if (!b) return;
  const chip = (k, v, prov) => el("li", { class: "pchip", "data-provenance": prov },
    el("span", { class: "k", text: t("chip." + k) }), el("span", { class: "v", text: v }));
  const items = [chip("world", t("chip.world." + CHIPS.world), CHIPS.world === "sim" ? "simulated" : "sandbox"),
    chip("run", t("chip.run.live"), "live")];
  if (CHIPS.reader) items.push(chip("reader", CHIPS.reader, "live"));
  b.replaceChildren(el("ul", { class: "chipbar", "aria-label": t("chip.label") }, ...items),
    CHIPS.sentence ? el("p", { class: "sim-line", text: CHIPS.sentence }) : null);
}
function setBanner(world, sentence) { CHIPS.world = world; CHIPS.sentence = sentence; renderChips(); }
function setReaderChip(name) { CHIPS.reader = name || null; renderChips(); }

async function boot() {
  STR = await (await fetch("/api/strings")).json();
  if (new URLSearchParams(location.search).get("stage") === "video") document.documentElement.dataset.stage = "video";
  document.title = t(document.body.dataset.title || "app.name");
  applyStrings(document);
  const mode = (await (await fetch("/api/mode")).json()).mode;
  const main = document.querySelector("main");
  if (main) {
    main.id = main.id || "main";
    document.body.prepend(el("a", { class: "skip", href: "#" + main.id, text: t("a11y.skip") }));
  }
  document.body.append(el("footer", { class: "site-footer" }, el("p", { text: t("footer.notice") })));
  setBanner(mode === "fixture" ? "sim" : "sandbox", t(mode === "fixture" ? "banner.fixture" : "banner.sandbox"));
  return mode;
}

// Sign-in panel for merchant pages. Calls onReady() once a session exists.
async function ensureSession(onReady) {
  const panel = document.getElementById("login");
  async function check() {
    try {
      const s = await api("/api/session", "GET");
      CSRF = s.csrf;
      panel.hidden = true;
      document.getElementById("app").hidden = false;
      onReady(s);
    } catch (e) {
      if (e.status !== 401) throw e;
      document.getElementById("app").hidden = true;
      panel.hidden = false;
    }
  }
  const form = document.getElementById("login-form");
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const msg = document.getElementById("login-msg");
    msg.textContent = "";
    try {
      await api("/api/login", "POST", { passcode: form.elements.passcode.value });
      form.reset();
      await check();
    } catch (e) { msg.textContent = t("login.error." + e.code); }
  });
  await check();
}

// Money display. Amounts are integer cents everywhere in the API.
function money(minor) {
  if (minor === null || minor === undefined) return t("arena.diff.dash");
  return "$" + (minor / 100).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}
function shortHash(h) { return String(h || "").slice(0, 16); }

// The scoreboard: four measures kept apart, each with its denominator. Used by the desk and the arena.
function scoreBlock(title, lines, note) {
  return el("div", { class: "card" }, el("h3", { text: title }), ...lines.map((l) => el("p", { text: l })),
    note ? el("p", { class: "note", text: note }) : null);
}

function scoreCards(sc, calls) {
  const out = [];
  if (calls) out.push(scoreBlock(t("desk.score.calls_title"), [t("desk.score.calls_line", { n: calls.sent, refunds: calls.with_id })], t("desk.score.calls_note")));
  out.push(
    scoreBlock(t("desk.score.limits_title"), [t("desk.score.limits_line", { n: sc.limits.checked_automatic_releases, v: sc.limits.violation_count })], t("desk.score.limits_note")),
    scoreBlock(t("desk.score.auth_title"), [t("desk.score.auth_line", { n: sc.authorization.checked_automatic_releases, u: sc.authorization.unauthorized_count })], t("desk.score.auth_note")),
    scoreBlock(t("desk.score.attack_title"), [
      t("desk.score.attack_line", { n: sc.attacks.labeled, x: sc.attacks.released_automatically, b: sc.attacks.blocked, o: sc.attacks.sent_to_owner }),
      t("desk.score.attack_reader", { w: sc.attacks.reader_went_along, n: sc.attacks.labeled })], t("desk.score.attack_note")),
    scoreBlock(t("desk.score.legit_title"), [
      t("desk.score.legit_line", { n: sc.legit.labeled, a: sc.legit.completed_automatically, o: sc.legit.sent_to_owner,
        p: sc.legit.owner_approved, r: sc.legit.refused, u: sc.legit.unresolved || 0 })], t("desk.score.legit_note")),
    scoreBlock(t("desk.score.other_title"), [
      t("desk.score.other_line", { n: sc.unlabeled.attempts, x: sc.unlabeled.released_automatically, o: sc.unlabeled.sent_to_owner, b: sc.unlabeled.blocked })]));
  return out;
}

// ---- the verdict stamp -------------------------------------------------------------------------------------
// The server writes every sentence (app/stamp.py). This only places them, with textContent.
function stampEl(st) {
  const box = el("div", { class: "stamp", "data-kind": st.kind, "data-tone": st.tone, "data-provenance": st.provenance, role: "group" },
    el("p", { class: "stamp-word", text: st.word }),
    st.rule_line ? el("p", { class: "stamp-rule", text: st.rule_line }) : null,
    st.reason ? el("p", { class: "stamp-reason", text: st.reason }) : null,
    el("p", { class: "stamp-consequence", text: st.consequence }),
    st.compare ? el("p", { class: "stamp-compare", text: st.compare }) : null);
  return box;
}

// A panel that is clearly one side of the line: "suggested" (the AI, not trusted) or "decided" (the code).
function sidePanel(side, ...kids) {
  const title = t("panel." + (side === "suggested" ? "suggest" : "decide") + "_title");
  const tag = t("panel." + (side === "suggested" ? "suggest" : "decide") + "_tag");
  return el("section", { class: "panel " + side, "data-side": side },
    el("h3", { text: title }), el("p", { class: "side-tag", text: tag }), ...kids);
}

// Three tiles, each a number with its denominator, drawn from this session's signed records only.
function scoreTiles(sc) {
  const tile = (key, value, label, sub) => el("div", { class: "tile", "data-key": key, "data-source": "session-records" },
    el("div", { class: "num", text: value }), el("div", { class: "lab", text: label }), el("div", { class: "sub", text: sub }));
  return [el("div", { class: "tiles3", role: "group" },
    tile("fooled", t("tile.fooled_value", { k: sc.attacks.reader_went_along, n: sc.attacks.labeled }), t("tile.fooled_label"), t("tile.fooled_sub")),
    tile("breach", t("tile.breach_value", { v: sc.limits.violation_count, n: sc.limits.checked_automatic_releases }), t("tile.breach_label"), t("tile.breach_sub")),
    tile("held", t("tile.held_value", { h: sc.legit.sent_to_owner, c: sc.legit.labeled }), t("tile.held_label"), t("tile.held_sub"))),
  el("p", { class: "note", text: t("tile.attack_note", { x: sc.attacks.released_automatically, n: sc.attacks.labeled }) })];
}

// ---- the decision packet: what is being approved, before the buttons ---------------------
// Every value comes from the server's view of the SIGNED records; the recipient is the order file's buyer label, never
// the reader's prose. Missing trusted data is said to be unavailable, not guessed.
function approvalSummary(v) {
  const who = ((v.facts || []).find((f) => f.label === t("fact.who")) || {}).value;
  const version = (v.approval && v.approval.policy_version) || (v.gate && v.gate.policy_version) || "?";
  const row = (k, val, cls) => [el("dt", { text: t("summary." + k) }), el("dd", { class: cls || "", text: val })];
  return el("dl", { class: "approve-summary" },
    ...row("amount", t("summary.amount_value", { amount: v.amount }), "summary-amount"),
    ...row("to", who || t("summary.unavailable")),
    ...row("order", t("summary.order_value", { order: v.order_ref, id: v.request_id }), "mono"),
    ...row("rule", t("summary.rule_value", { version: version })));
}

// ---- the audit strip next to the decision (rank 9): which signed record, which rule version, where PayPal's answers
// come from, and that the chain was NOT verified on this screen (the receipt page runs that check).
function auditStrip(v, world) {
  const record = v.gate && v.gate.record_id, receipt = v.receipt_id;
  if (!record && !receipt) return null;
  const version = (v.gate && v.gate.policy_version) || (v.approval && v.approval.policy_version) || "?";
  const kids = [el("span", { class: "audit-k", text: t("audit.title") })];
  if (record) kids.push(el("span", { class: "mono", text: t("audit.record", { id: record }) }));
  if (receipt) kids.push(el("span", { class: "mono", text: t("audit.receipt", { id: receipt }) }));
  kids.push(el("span", { text: t("audit.rule", { version: version }) }),
    el("span", { text: t(world === "sim" ? "audit.source_sim" : "audit.source_sandbox") }),
    el("span", { class: "audit-unverified", text: t("audit.unverified") }));
  return el("p", { class: "audit-strip", role: "note" }, ...kids);
}
