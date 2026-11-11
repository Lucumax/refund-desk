"use strict";
(async () => {
  const mode = await boot();
  if (mode !== "fixture") return;
  // Simulation only: the demo hands out the order link that a real shop would email.
  try {
    const d = await (await fetch("/api/demo/links")).json();
    if (d.links && d.links.length) document.getElementById("buyer-link").href = d.links[0].url;
  } catch (e) { /* the link stays as is */ }
})();
