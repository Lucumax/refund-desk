"use strict";
const out = () => document.getElementById("result");

async function act(path, body) {
  out().className = "result"; out().textContent = t("inbox.working");
  try { return await api(path, "POST", body || {}); }
  catch (e) { out().className = "result bad"; out().textContent = errMessage(e); return null; }
}

async function load() {
  const d = await api("/api/demo/scenes", "GET");
  const ul = document.getElementById("scenes");
  ul.replaceChildren(...d.scenes.map((s) => el("li", {}, el("button", { type: "button", text: s.label, onclick: async () => {
    const r = await act("/api/demo/scene", { scene: s.id });
    if (r) { out().className = "result good"; out().textContent = r.request.stage_label + ": " + r.request.headline; }
  } }))));
}

(async () => {
  await boot();
  await ensureSession(() => load());
  document.getElementById("webhook").addEventListener("click", async () => {
    const r = await act("/api/demo/webhook", {});
    if (r) { out().className = "result good"; out().textContent = r.result === "NO_REFUND_YET" ? t("demo.no_refund") : t("demo.webhook_result", { result: r.result }); }
  });
  document.getElementById("replay").addEventListener("click", async () => {
    const r = await act("/api/demo/webhook", { replay: true });
    if (r) { out().className = "result good"; out().textContent = r.result === "NO_REFUND_YET" ? t("demo.no_refund") : t("demo.webhook_result", { result: r.result }); }
  });
  const ls = await (await fetch("/api/demo/links")).json();
  document.getElementById("links").replaceChildren(...ls.links.map((l) =>
    el("li", {}, el("a", { href: l.url, text: l.order_ref + ": " + l.item }))));
  document.getElementById("poll").addEventListener("click", async () => {
    const r = await act("/api/demo/poll", {});
    if (r) { out().className = "result good"; out().textContent = t("demo.poll_result", { touched: r.touched }); }
  });
  document.getElementById("counts").addEventListener("click", async () => {
    const r = await act("/api/demo/counts", {});
    if (r) { out().className = "result good"; out().textContent = t("demo.counts_result", { posts: r.posts, created: r.created }); }
  });
  document.getElementById("tamper").addEventListener("click", async () => {
    const r = await act("/api/demo/tamper", {});
    if (r) { out().className = "result " + (r.ok ? "good" : "bad"); out().textContent = r.message; }
  });
})();
