"use strict";
// Runs the shared approval summary and audit strip from static/common.js against a stand-in document (no browser) with the
// real copy file loaded, and prints what they rendered, as JSON. Used by tests/test_desk_summary_and_audit_strip.py.
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const root = path.join(__dirname, "..", "..");
const src = fs.readFileSync(path.join(root, "static", "common.js"), "utf8");
const copy = JSON.parse(fs.readFileSync(path.join(root, "copy", "strings.draft.json"), "utf8")).strings;
function mk(tag) {
  return { tag, className: "", textContent: "", attrs: {}, kids: [],
    setAttribute(k, v) { this.attrs[k] = v; }, addEventListener() {}, append(...k) { this.kids.push(...k); } };
}
const ctx = vm.createContext({ URLSearchParams, Date, JSON, Promise, console, document: { createElement: mk }, location: { search: "" } });
vm.runInContext(src, ctx);
vm.runInContext("STR = " + JSON.stringify(copy) + ";", ctx);
const text = (n) => (n && typeof n === "object") ? [n.textContent, ...(n.kids || []).map(text)].filter(Boolean).join(" | ") : String(n || "");
const classes = (n, out = []) => { if (n && typeof n === "object") { if (n.className) out.push(n.className); (n.kids || []).forEach((k) => classes(k, out)); } return out; };

const v = { request_id: "REQ-0004", intent_id: "I-0007", receipt_id: "I-0007", amount_minor: 12000, order_ref: "ORD-1002", amount: "$120.00",
  reason: "This is $120.00, more than the $25.00 you allow for one automatic refund. It waits for you.",
  facts: [{ label: "Who gets the refund", value: "Sample buyer B, ORD-1002" }],
  gate: { record_id: "D-000012", policy_version: 1, route: "OWNER" },
  approval: { nonce: "N1", digest: "D1", policy_version: 1, expires_at: "2026-11-02T09:00:00+00:00", stale: false } };
const out = {};
const s1 = vm.runInContext("approvalSummary", ctx)(v);
out.summary = { tag: s1.tag, cls: s1.className, text: text(s1) };
const s2 = vm.runInContext("approvalSummary", ctx)({ ...v, facts: [] });
out.summary_no_who = text(s2);
const a1 = vm.runInContext("auditStrip", ctx)(v, "sim");
out.audit_sim = { cls: classes(a1), text: text(a1) };
out.audit_sandbox = text(vm.runInContext("auditStrip", ctx)(v, "sandbox"));
out.audit_none = vm.runInContext("auditStrip", ctx)({ ...v, gate: null, receipt_id: null }, "sim");
console.log(JSON.stringify(out));
