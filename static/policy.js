"use strict";
const $ = (id) => document.getElementById(id);
const MONEY_FIELDS = ["per_order_cap_minor", "escalate_ceiling_minor", "daily_budget_minor"];
let LAST_OK = false;
let LAST_PREVIEW = null;   // {base_version, result_hash}: what the owner was shown, sent back with the save

function fieldValue(f, v) {
  if (MONEY_FIELDS.includes(f)) return money(v);
  if (f === "evidence_required") return t(v ? "policy.yes" : "policy.no");
  return v.map((r) => t("issue." + r)).join(", ");
}

function diffTable(changes) {
  const rows = changes.map((c) => el("tr", {},
    el("th", { scope: "row", text: t("policy.field." + c.field) }),
    el("td", { text: fieldValue(c.field, c.before) }),
    el("td", { class: c.changed ? "cut" : "", text: fieldValue(c.field, c.after) }),
    el("td", { text: t(c.changed ? "policy.changed" : "policy.same") })));
  return el("table", {}, el("thead", {}, el("tr", {}, el("th", { text: t("policy.col_field") }), el("th", { text: t("policy.col_before") }),
    el("th", { text: t("policy.col_after") }), el("th", { text: t("policy.col_state") }))), el("tbody", {}, ...rows));
}

function errorList(errors) {
  return el("ul", {}, ...errors.map((e) => {
    const kids = [el("strong", { text: t("policy.err." + e.code) })];
    if (e.code === "UNKNOWN_PHRASE") {
      kids.push(el("blockquote", { text: e.clause }));
      if (e.left && e.left.length) kids.push(el("div", { class: "note", text: t("policy.err_left", { left: e.left.join(", ") }) }));
    }
    return el("li", {}, ...kids);
  }));
}

function renderCurrent(p) {
  const rows = ["per_order_cap_minor", "escalate_ceiling_minor", "daily_budget_minor", "evidence_required", "eligible_reasons"]
    .map((f) => el("tr", {}, el("th", { scope: "row", text: t("policy.field." + f) }), el("td", { text: fieldValue(f, p[f]) })));
  $("current").replaceChildren(
    el("p", { class: "headline", text: t("arena.policy_version", { version: p.version, hash: shortHash(p.hash) }) }),
    el("table", {}, el("tbody", {}, ...rows)));
}

function renderVersions(state) {
  $("versions").replaceChildren(...state.versions.slice().reverse().map((v) => el("li", { class: "card" },
    el("div", { class: "headline", text: t("policy.version_line", { version: v.version, receipt: v.receipt_id, hash: shortHash(v.hash) }) }),
    el("div", { class: "note", text: t("policy.version_source") }),
    v.source_text ? el("blockquote", { text: v.source_text }) : el("div", { text: t("policy.version_default") }))));
  const opts = state.versions.map((v) => el("option", { value: String(v.version), text: String(v.version) }));
  $("vfrom").replaceChildren(...opts.map((o) => o.cloneNode(true)));
  $("vto").replaceChildren(...opts.map((o) => o.cloneNode(true)));
  $("vfrom").value = String(Math.max(1, state.versions.length - 1));
  $("vto").value = String(state.versions.length);
  $("examples").replaceChildren(...state.examples.map((x) => el("li", {}, el("button", { type: "button", class: "secondary preset mono",
    onclick: () => { const box = $("rule"); box.value = (box.value.trim() ? box.value.trim().replace(/[.;]*$/, "") + "; " : "") + x; box.dispatchEvent(new Event("input")); box.focus(); },
    text: x }))));
}

function renderEnforced(e) {
  $("enforced").replaceChildren(
    el("p", { text: t("policy.enforced_cap", { value: money(e.automatic_allowance_minor) }) }),
    el("p", { text: t("policy.enforced_daily", { value: money(e.daily_budget_minor) }) }),
    el("p", { text: t("policy.enforced_ceiling", { value: money(e.approve_ceiling_minor) }) }),
    el("p", { class: "note", text: t("policy.enforced_version", { version: e.mandate_version }) }),
    el("p", { class: e.matches_policy ? "note" : "flag", text: t(e.matches_policy ? "policy.enforced_match" : "policy.enforced_mismatch") }));
}

async function load() {
  const s = await api("/api/policy", "GET");
  renderCurrent(s.active);
  renderEnforced(s.enforced);
  renderVersions(s);
}

function showResult(r) {
  LAST_OK = r.ok && !r.no_change;
  LAST_PREVIEW = LAST_OK ? { base_version: r.base_version, result_hash: r.result_hash } : null;
  $("save").disabled = !LAST_OK;
  if (!r.ok) {
    $("out").replaceChildren(el("div", { class: "card" }, el("h2", { text: t("policy.reject_title") }), errorList(r.errors)));
    return;
  }
  $("out").replaceChildren(el("div", { class: "card" }, el("h2", { text: t("policy.ok_title") }),
    el("p", { class: "note", text: t("policy.compare_note", { version: r.base_version }) }),
    r.no_change ? el("p", { text: t("policy.diff_none") }) : diffTable(r.changes),
    r.no_change ? null : el("p", { class: "note", text: t("policy.preview_fingerprint", { hash: shortHash(r.result_hash) }) }),
    r.no_change ? null : el("p", { class: "note", text: t("policy.save_binds", { version: r.base_version }) })));
}

async function preview() {
  try { showResult(await api("/api/policy/preview", "POST", { text: $("rule").value })); }
  catch (e) { $("out").replaceChildren(el("p", { class: "result bad", text: errMessage(e) })); }
}

async function save() {
  try {
    if (!LAST_PREVIEW) return;
    const r = await api("/api/policy/save", "POST", { text: $("rule").value, base_version: LAST_PREVIEW.base_version,
      result_hash: LAST_PREVIEW.result_hash });
    $("out").replaceChildren(el("div", { class: "card" }, el("h2", { text: t("policy.saved", { version: r.version, receipt: r.receipt_id, hash: shortHash(r.hash) }) }),
      diffTable(r.changes), el("p", { class: "note", text: t("policy.saved_note") })));
    $("save").disabled = true;
    LAST_PREVIEW = null;
    await load();
  } catch (e) {
    if (e.data && e.data.errors) {
      $("out").replaceChildren(el("div", { class: "card" }, el("h2", { text: t("policy.reject_title") }), errorList(e.data.errors)));
      $("save").disabled = true;
      LAST_PREVIEW = null;
      await load();
    } else {
      $("out").replaceChildren(el("p", { class: "result bad", text: errMessage(e) }));
    }
  }
}

async function compare() {
  const a = $("vfrom").value, b = $("vto").value;
  const d = await api("/api/policy/diff?from=" + encodeURIComponent(a) + "&to=" + encodeURIComponent(b), "GET");
  $("vdiff").replaceChildren(diffTable(d.changes));
}

(async () => {
  await boot();
  $("banners").replaceChildren(el("p", { text: t("banner.arena") }));
  await ensureSession(async () => {
    await load();
    const q = new URLSearchParams(location.search);
    if (q.get("text")) $("rule").value = q.get("text");
    if (q.get("run") && $("rule").value) await preview();
    if (q.get("compare")) await compare();
  });
  $("preview").addEventListener("click", preview);
  $("save").addEventListener("click", save);
  $("vgo").addEventListener("click", compare);
  $("rule").addEventListener("input", () => { $("save").disabled = true; });
})();
