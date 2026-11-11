"use strict";
// Runs static/approve.js against a tiny stand-in for the page (no browser) and prints what the phone card did, as JSON.
// Used by tests/test_phone_card_js.py. It exercises the real functions (paint, decide), not their source text.
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const src = fs.readFileSync(path.join(__dirname, "..", "..", "static", "approve.js"), "utf8");
const nodes = {};
const calls = [];
function node(id) { return nodes[id] || (nodes[id] = { id, kids: [], replaceChildren(...k) { this.kids = k; }, prepend(k) { this.kids.unshift(k); } }); }
function el(tag, attrs, ...kids) { return { tag, attrs: attrs || {}, kids: kids.flat().filter((k) => k !== null && k !== undefined) }; }
function walk(n, out = []) { if (n && typeof n === "object") { out.push(n); (n.kids || []).forEach((k) => walk(k, out)); } return out; }

const ctx = vm.createContext({
  URLSearchParams, Date, JSON, Promise, console,
  location: { search: "?id=REQ-0001" },
  document: { getElementById: node, querySelectorAll: () => [] },
  el, t: (k) => k, money: (v) => "$" + (v / 100).toFixed(2), stampEl: (s) => ({ tag: "stamp", attrs: { word: s.word }, kids: [] }),
  api: async (url, method, body) => { calls.push({ url, method, body }); return { stamp: { word: "DONE" }, actions: [], request_id: "REQ-0001" }; },
  errMessage: (e) => String(e), boot: async () => {}, ensureSession: async () => {},
  approvalSummary: () => ({ tag: "summary", attrs: {}, kids: [] }), auditStrip: () => ({ tag: "audit", attrs: {}, kids: [] }), CHIPS: { world: "sim" },
});
vm.runInContext(src, ctx);

const base = { request_id: "REQ-0001", intent_id: "I-0001", amount_minor: 12000, order_ref: "ORD-1002", amount: "$120.00", quote: "never arrived", reason: "over the limit", reader: null,
  approval: { nonce: "N1", digest: "D1", policy_version: 1, expires_at: "2026-11-02T09:00:00+00:00", stale: false }, stamp: { word: "HELD" } };
const buttons = () => walk(node("card")).filter((n) => n.tag === "button").map((n) => n.attrs.id);
const out = {};

ctx.paint({ ...base, actions: ["approve", "deny"] });
out.normal = buttons();
ctx.paint({ ...base, approval: { ...base.approval, stale: true }, actions: ["deny"] });
out.stale = buttons();
ctx.paint({ ...base, actions: [], approval: null });
out.done = { buttons: buttons(), stamp: walk(node("card")).some((n) => n.tag === "stamp") };

ctx.paint({ ...base, actions: ["approve", "deny"] });
// Order of the decision packet as rendered: summary, why it waits, the buyer's words, the scope line, the buttons, the audit strip
out.order = walk(node("card")).map((n) => n.tag === "summary" ? "summary" : n.tag === "audit" ? "audit"
  : (n.tag === "h2" && n.attrs.text === "approve.why") ? "why" : (n.tag === "blockquote" && n.attrs.text === "never arrived") ? "buyer"
  : (n.attrs && n.attrs.class === "bound") ? "scope" : (n.tag === "button") ? "button:" + n.attrs.id : null).filter(Boolean);
out.approve_text = walk(node("card")).find((n) => n.tag === "button" && n.attrs.id === "approve").attrs.text;
ctx.paint({ ...base, actions: ["approve", "deny"], evidence_flags: ["UNSIGNED_TEXT_DIFFERS"] });
out.flag_note = walk(node("card")).some((n) => n.attrs && n.attrs.text === "text.flag.UNSIGNED_TEXT_DIFFERS");
ctx.paint({ ...base, actions: ["approve", "deny"], evidence_flags: [] });
out.no_flag_note = walk(node("card")).some((n) => n.attrs && typeof n.attrs.text === "string" && n.attrs.text.startsWith("text.flag."));

(async () => {
  ctx.paint({ ...base, actions: ["approve", "deny"] });
  const find = (id) => walk(node("card")).find((n) => n.tag === "button" && n.attrs.id === id);
  await find("deny").attrs.onclick({ target: {} });
  out.deny_call = calls.pop();
  ctx.paint({ ...base, actions: ["approve", "deny"] });
  await find("approve").attrs.onclick({ target: {} });
  out.approve_call = calls.pop();
  console.log(JSON.stringify(out));
})();
