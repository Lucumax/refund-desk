"use strict";
async function showStatus(token) {
  const r = await fetch("/api/buyer/requests/" + encodeURIComponent(token));
  if (!r.ok) return;
  const v = await r.json();
  document.getElementById("status").hidden = false;
  document.getElementById("status-msg").textContent = v.message;
}
(async () => {
  await boot();
  const form = document.getElementById("form"), err = document.getElementById("error");
  const q = new URLSearchParams(location.search);
  let token = q.get("t");
  const order = q.get("o") || "", cap = q.get("c") || "";
  if (token) showStatus(token);
  if (!order || !cap) {            // no order link: the page cannot send anything
    err.textContent = t("buyer.no_link");
    document.getElementById("send").disabled = true;
  }
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    err.textContent = "";
    const btn = document.getElementById("send");
    btn.disabled = true; btn.textContent = t("buyer.sending");
    try {
      const r = await fetch("/api/buyer/messages", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: form.elements.text.value, order: order, cap: cap }) });
      const d = await r.json();
      if (!r.ok) {
        const k = "buyer.error." + d.error;
        err.textContent = STR[k] ? t(k) : t("buyer.error.GENERIC");
      } else {
        token = d.token;
        history.replaceState(null, "", "?o=" + encodeURIComponent(order) + "&c=" + encodeURIComponent(cap) + "&t=" + encodeURIComponent(token));
        form.reset();
        document.getElementById("status").hidden = false;
        document.getElementById("status-msg").textContent = d.status.message;
      }
    } catch (e) { err.textContent = t("buyer.error.GENERIC"); }
    btn.disabled = !order || !cap; btn.textContent = t("buyer.send");
  });
  document.getElementById("refresh").addEventListener("click", () => token && showStatus(token));
})();
