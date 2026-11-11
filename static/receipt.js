"use strict";
async function load() {
  const id = new URLSearchParams(location.search).get("id") || "";
  const d = await api("/api/receipts/" + encodeURIComponent(id), "GET");
  document.getElementById("paragraph").textContent = d.paragraph;
  const p = document.getElementById("provider");
  if (!d.provider) p.textContent = t("receipt.provider_none");
  else if (!d.provider.ok) p.textContent = d.provider.label;
  else p.textContent = d.provider.id + " | " + d.provider.status + " | " + d.provider.amount + " | " + d.provider.label;
  document.getElementById("history").replaceChildren(...d.history.map((h) =>
    el("li", { text: h.id + " | " + h.outcome + " | " + h.at })));
  document.getElementById("raw").textContent = JSON.stringify(d.raw, null, 2);
}

(async () => {
  await boot();
  await ensureSession(() => load());
  document.getElementById("verify").addEventListener("click", async () => {
    const box = document.getElementById("verify-result");
    box.hidden = false; box.className = "result"; box.textContent = t("verify.running");
    try {
      const v = await api("/api/verify", "GET");
      box.className = "result " + (v.ok ? "good" : "bad");
      box.textContent = v.message;
    } catch (e) { box.className = "result bad"; box.textContent = errMessage(e); }
  });
})();
