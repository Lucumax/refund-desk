"use strict";
const $ = (id) => document.getElementById(id);

function lineEl(l) {
  const kids = [el("div", { class: "who", text: t("arena.court.role." + l.role) })];
  if (l.kind === "quote") kids.push(el("blockquote", { text: l.text }));
  else if (l.kind === "flag") {
    kids.push(el("div", { text: l.text }));
    kids.push(el("blockquote", { text: l.quote }));
  } else if (l.kind === "rule") {
    kids.push(el("div", {}, el("span", { class: "chip", "data-r": "HIT", text: l.rule + " " + t("arena.trace.HIT") }), el("span", { text: "  " + l.text })));
  } else if (l.kind === "verdict") {
    kids.push(el("div", {}, el("span", { class: "decision", "data-d": l.decision, text: l.decision }), el("span", { text: "  " + l.text })));
  } else if (l.kind === "receipt") {
    kids.push(el("div", { class: "mono", text: l.text }));
  } else kids.push(el("div", { text: l.text }));
  const attrs = { class: "line", "data-role": l.role };
  if (l.decision) attrs["data-d"] = l.decision;
  return el("div", attrs, ...kids);
}

async function show(id) {
  const c = await api("/api/arena/court/" + encodeURIComponent(id), "GET");
  $("transcript").replaceChildren(...c.lines.map(lineEl));
  document.querySelectorAll("#cases button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.id === id)));
}

(async () => {
  await boot();
  setBanner("sim", t("banner.arena"));
  const s = await api("/api/arena/state", "GET");
  if (!s.recent.length) { $("none").hidden = false; return; }
  $("cases").replaceChildren(...s.recent.map((a) => el("li", {}, el("button", { type: "button", class: "secondary preset", "data-id": a.id,
    "aria-pressed": "false", onclick: () => show(a.id) },
  el("span", { class: "decision small", "data-d": a.decision, text: a.decision }),
  el("span", { text: t("arena.recent_line", { id: a.id, order: a.order, decision: a.decision, rule: a.rule_id }) })))));
  const want = new URLSearchParams(location.search).get("id");
  await show(want || s.recent[0].id);
})();
