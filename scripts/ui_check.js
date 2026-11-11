"use strict";
// Layout check for the design pass (QA only; talks to 127.0.0.1 and a local Chrome, nothing else).
//   1. python -m app --fresh --data-dir <a scratch folder> --port 8799        (the SIMULATION; passcode demo-passcode)
//   2. node scripts/ui_check.js <out folder> [base url]       (SCHEME=dark for the dark theme; the default is light)
// Needs Node 22 or newer (global WebSocket and fetch) and Chrome. No npm packages. It drives Chrome over the DevTools
// protocol, visits each page at a 375 px phone width and at 1280 by 720 (device scale 1.5), sends a few desk requests,
// and records: horizontal overflow, the smallest text on the page, console errors, and screenshots.
// Exit code 1 if any page scrolls sideways at 375 px, any text is under 16 px, or the console shows an unexpected error.
const fs = require("fs");
const path = require("path");
const os = require("os");
const { spawn } = require("child_process");

const OUT = path.resolve(process.argv[2] || "ui-check");
const BASE = process.argv[3] || "http://127.0.0.1:8799";
const CHROME = process.env.CHROME_PATH || "C:/Program Files/Google/Chrome/Application/chrome.exe";
const PASS = process.env.APP_PASSCODE || "demo-passcode";
const PORT = 9333;
const EXPECTED = [/status of 401/];
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
fs.mkdirSync(OUT, { recursive: true });

class Cdp {
  constructor(ws) { this.ws = ws; this.id = 0; this.waiting = new Map(); this.handlers = []; this.logs = [];
    ws.addEventListener("message", (m) => { const d = JSON.parse(m.data);
      if (d.id && this.waiting.has(d.id)) { const { res, rej } = this.waiting.get(d.id); this.waiting.delete(d.id); d.error ? rej(new Error(d.error.message)) : res(d.result); }
      else if (d.method) { if (d.method === "Runtime.consoleAPICalled" && ["error", "warning"].includes(d.params.type)) this.logs.push(d.params.args.map((a) => a.value || a.description).join(" "));
        if (d.method === "Log.entryAdded" && ["error", "warning"].includes(d.params.entry.level)) this.logs.push(d.params.entry.text + " " + (d.params.entry.url || ""));
        if (d.method === "Runtime.exceptionThrown") this.logs.push("exception " + JSON.stringify(d.params.exceptionDetails.exception || d.params.exceptionDetails.text)); } }); }
  send(method, params) { const id = ++this.id; this.ws.send(JSON.stringify({ id, method, params })); return new Promise((res, rej) => this.waiting.set(id, { res, rej })); }
  async eval(expr) { const r = await this.send("Runtime.evaluate", { expression: expr, awaitPromise: true, returnByValue: true });
    if (r.exceptionDetails) throw new Error(JSON.stringify(r.exceptionDetails)); return r.result.value; }
}

async function connect() {
  for (let i = 0; i < 50; i++) { try { const r = await fetch(`http://127.0.0.1:${PORT}/json/new?about:blank`, { method: "PUT" });
    const t = await r.json(); const ws = new WebSocket(t.webSocketDebuggerUrl); await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
    return new Cdp(ws); } catch (e) { await sleep(200); } }
  throw new Error("could not reach Chrome");
}

const MEASURE = `(() => {
  const out = { overflow: document.documentElement.scrollWidth - window.innerWidth, smallest: 99, small: [], height: document.documentElement.scrollHeight };
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let n; while ((n = walker.nextNode())) {
    if (!n.nodeValue.trim()) continue;
    const e = n.parentElement; if (!e || e.closest("script,style,[hidden]")) continue;
    const r = e.getBoundingClientRect(); if (!r.width || !r.height) continue;
    const cs = getComputedStyle(e); if (cs.visibility === "hidden" || cs.display === "none") continue;
    if (r.left < -5000) continue;                     // visually hidden helper text
    const px = parseFloat(cs.fontSize);
    if (px < out.smallest) out.smallest = px;
    if (px < 15.99 && out.small.length < 8) out.small.push(e.tagName + "." + e.className + " " + px + "px " + n.nodeValue.trim().slice(0, 30));
  }
  const wide = [];
  for (const e of document.querySelectorAll("body *")) { const r = e.getBoundingClientRect(); if (r.right > window.innerWidth + 1 && r.width && wide.length < 5 && !e.closest("[hidden]")) wide.push(e.tagName + "." + e.className + " right=" + Math.round(r.right)); }
  out.wide = wide;
  return out;
})()`;

(async () => {
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), "ui-check-"));
  const chrome = spawn(CHROME, ["--headless=new", `--remote-debugging-port=${PORT}`, `--user-data-dir=${profile}`, "--no-first-run", "--disable-gpu", "--hide-scrollbars", "about:blank"], { stdio: "ignore" });
  const results = []; let failed = false;
  try {
    const c = await connect();
    await c.send("Page.enable");
    await c.send("Emulation.setEmulatedMedia", { features: [{ name: "prefers-color-scheme", value: process.env.SCHEME || "light" }] }); await c.send("Runtime.enable"); await c.send("Log.enable");
    const go = async (url) => { await c.send("Page.navigate", { url }); await sleep(900); };
    await go(BASE + "/");
    await c.eval(`fetch("/api/login", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({passcode:"${PASS}"})}).then(r=>r.status)`);
    for (const vp of [{ name: "phone", w: 375, h: 812, s: 2 }, { name: "desktop", w: 1280, h: 720, s: 1.5 }]) {
      await c.send("Emulation.setDeviceMetricsOverride", { width: vp.w, height: vp.h, deviceScaleFactor: vp.s, mobile: vp.name === "phone" });
      const shot = async (name) => { await sleep(300); const r = await c.send("Page.captureScreenshot", { format: "png", captureBeyondViewport: true });
        fs.writeFileSync(path.join(OUT, `${vp.name}-${name}.png`), Buffer.from(r.data, "base64")); };
      const check = async (name) => { const m = await c.eval(MEASURE); results.push({ vp: vp.name, page: name, ...m }); await shot(name);
        if (vp.name === "phone" && m.overflow > 0) failed = true; if (m.smallest < 15.99) failed = true; };
      for (const p of ["/", "/desk.html", "/arena.html", "/policy.html", "/court.html", "/inbox.html", "/demo.html", "/buyer.html"]) { await go(BASE + p); await check(p.replace(/\W/g, "") || "home"); }
      // desk: an ordinary request, a hostile one, a request that waits for the owner
      await go(BASE + "/desk.html");
      for (const [label, expr] of [["ordinary", `document.querySelector('button.preset[data-id]')`], ["hostile", `document.querySelectorAll('#sc-hostile button.preset')[0]`], ["owner", `[...document.querySelectorAll('#sc-legit button.preset')].find(b=>/owner|larger/i.test(b.textContent))`]]) {
        const clicked = await c.eval(`(() => { const b = ${expr}; if (!b) return false; b.click(); return true; })()`);
        await sleep(1200); if (clicked) await check("desk-" + label); else { results.push({ vp: vp.name, page: "desk-" + label, note: "MISSING: no such button" }); failed = true; }
      }
      // phone approval card for the request waiting for the owner, if there is one
      const pending = await c.eval(`fetch("/api/inbox").then(r=>r.json()).then(d=>{const p=d.items.find(i=>i.actions.includes("approve")); return p?p.request_id:null})`);
      if (pending) {
        await go(BASE + "/approve.html?id=" + pending); await check("approve");
        const both = await c.eval(`!!document.getElementById("approve") && !!document.getElementById("deny")`);
        if (!both) { results.push({ vp: vp.name, page: "approve", note: "MISSING: the phone card has no Approve and Deny buttons" }); failed = true; }
      } else { results.push({ vp: vp.name, page: "approve", note: "MISSING: no request is waiting for the owner" }); failed = true; }
      await go(BASE + "/arena.html");
      await c.eval(`(() => { const b = document.querySelector('button.preset[data-id="A01"]'); if (b) b.click(); document.getElementById("send").click(); })()`);
      await sleep(1500); await check("arena-attempt");
      await go(BASE + "/desk.html?stage=video"); await c.eval(`document.querySelector('button.preset[data-id]').click()`); await sleep(1200); await check("desk-stage-video");
    }
    const bad = c.logs.filter((l) => !EXPECTED.some((re) => re.test(l)));
    if (bad.length) failed = true;
    fs.writeFileSync(path.join(OUT, "findings.json"), JSON.stringify({ results, console: bad }, null, 2));
    for (const r of results) console.log(`${r.vp.padEnd(8)} ${String(r.page).padEnd(18)} overflow=${r.overflow} smallest=${r.smallest}px height=${r.height}${r.small && r.small.length ? " SMALL:" + r.small.join(" | ") : ""}${r.wide && r.wide.length ? " WIDE:" + r.wide.join(" | ") : ""}${r.note ? " " + r.note : ""}`);
    if (bad.length) console.log("console:", bad);
  } finally { chrome.kill(); await sleep(300); try { fs.rmSync(profile, { recursive: true, force: true }); } catch (e) { /* profile may stay locked briefly */ } }
  console.log(failed ? "FAIL" : "PASS");
  process.exit(failed ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(2); });
