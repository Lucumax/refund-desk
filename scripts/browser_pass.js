"use strict";
// Real-browser pass over the Refund Desk pages (QA only; no keys, no network beyond 127.0.0.1).
//   1. python -m app --fresh --data-dir <a scratch folder> --port 8799      (the SIMULATION; passcode demo-passcode)
//   2. PUPPETEER_PATH=<path to a puppeteer-core folder> CHROME_PATH=<chrome.exe> node scripts/browser_pass.js <out folder> [base url]
// It loads the pages at desktop and phone width, drives the desk (an ordinary request, a hostile one, a taken-over
// reader, a request that needs the owner, the cut-the-line fault and its re-check), the Policy Builder (rule,
// preview, save, refusal), the arena, and the inbox. It saves screenshots and writes findings.json: console
// errors, failed requests, horizontal overflow, small tap targets, unlabeled controls.
const fs = require("fs");
const path = require("path");
const puppeteer = require(process.env.PUPPETEER_PATH || "puppeteer-core");

const OUT = path.resolve(process.argv[2] || "browser-pass");
const BASE = process.argv[3] || "http://127.0.0.1:8799";
const CHROME = process.env.CHROME_PATH || "C:/Program Files/Google/Chrome/Application/chrome.exe";
const PASS = process.env.APP_PASSCODE || "demo-passcode";
const findings = [];
// Known, harmless lines: a page asks /api/session before sign-in and the browser logs the 401 as a console error.
const EXPECTED = [/console\.error Failed to load resource: the server responded with a status of 401/];
fs.mkdirSync(OUT, { recursive: true });

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
function note(page, kind, detail) { findings.push({ page, kind, detail }); }

async function open(browser, name, w, h) {
  const page = await browser.newPage();
  await page.setViewport({ width: w, height: h, deviceScaleFactor: 1 });
  page.on("console", (m) => { if (["error", "warning"].includes(m.type())) note(name, "console." + m.type(), m.text()); });
  page.on("pageerror", (e) => note(name, "pageerror", String(e)));
  page.on("requestfailed", (r) => note(name, "requestfailed", r.url() + " " + (r.failure() || {}).errorText));
  page.on("response", (r) => { if (r.status() >= 400 && !r.url().endsWith("/api/session")) note(name, "http." + r.status(), r.url()); });
  return page;
}

async function shot(page, file, full = true) {
  await sleep(250);
  await page.screenshot({ path: path.join(OUT, file), fullPage: full });
}

async function audit(page, name, phone) {
  const r = await page.evaluate((phone) => {
    const out = { overflow: document.documentElement.scrollWidth - window.innerWidth, small: [], unlabeled: [], noname: [] };
    const label = (e) => (e.getAttribute("aria-label") || e.textContent || "").trim();
    for (const b of document.querySelectorAll("button, a.button, a[href]")) {
      const rc = b.getBoundingClientRect();
      if (!rc.width || !rc.height) continue;
      if (!label(b)) out.noname.push(b.outerHTML.slice(0, 80));
      if (phone && (rc.height < 40) && b.tagName === "BUTTON") out.small.push(label(b).slice(0, 40) + " " + Math.round(rc.width) + "x" + Math.round(rc.height));
    }
    for (const i of document.querySelectorAll("input, select, textarea")) {
      const rc = i.getBoundingClientRect();
      if (!rc.width) continue;
      const id = i.id;
      const has = (id && document.querySelector('label[for="' + id + '"]')) || i.getAttribute("aria-label");
      if (!has) out.unlabeled.push(i.outerHTML.slice(0, 80));
    }
    const lang = document.documentElement.lang;
    out.lang = lang;
    out.title = document.title;
    return out;
  }, phone);
  if (r.overflow > 1) note(name, "horizontal-overflow", r.overflow + "px wider than the window");
  r.small.forEach((s) => note(name, "small-tap-target", s));
  r.unlabeled.forEach((s) => note(name, "unlabeled-control", s));
  r.noname.forEach((s) => note(name, "control-without-name", s));
  if (!r.title || r.title === "") note(name, "missing-title", "");
  return r;
}

async function login(page, target) {
  await page.goto(BASE + target, { waitUntil: "networkidle0" });
  if (await page.$("#login:not([hidden])")) {
    await page.type("#passcode", PASS);
    await Promise.all([page.waitForSelector("#app:not([hidden])"), page.click("#login-form button[type=submit]")]);
    await sleep(500);
  }
}

async function clickText(page, selector, includes) {
  const handles = await page.$$(selector);
  for (const h of handles) {
    const t = await page.evaluate((e) => e.textContent, h);
    if (t.includes(includes)) { await h.click(); return true; }
  }
  note("script", "button-not-found", selector + " containing '" + includes + "'");
  return false;
}

(async () => {
  const browser = await puppeteer.launch({ executablePath: CHROME, headless: true, args: ["--no-sandbox"] });
  const want = (process.env.VIEWPORTS || "desktop,phone").split(",");
  const sizes = [["desktop", 1920, 1080], ["phone", 390, 844]].filter((x) => want.includes(x[0]));
  for (const [label, w, h] of sizes) {
    const phone = label === "phone";
    // ---- home ----
    let page = await open(browser, label + "/index", w, h);
    await page.goto(BASE + "/", { waitUntil: "networkidle0" });
    await shot(page, `${label}-01-home.png`);
    await audit(page, label + "/index", phone);
    await page.close();

    // ---- desk ----
    page = await open(browser, label + "/desk", w, h);
    await page.goto(BASE + "/desk.html", { waitUntil: "networkidle0" });
    await shot(page, `${label}-02-desk-login.png`);
    await login(page, "/desk.html");
    await shot(page, `${label}-03-desk-start.png`);
    await audit(page, label + "/desk", phone);
    await clickText(page, "#sc-legit button", "Ordinary request");
    await page.waitForSelector("#flow:not([hidden])");
    await shot(page, `${label}-04-desk-ordinary-auto.png`);
    await clickText(page, "button", "Read it back");
    await sleep(600);
    await shot(page, `${label}-05-desk-readback.png`);
    await clickText(page, "#sc-legit button", "Larger request");
    await sleep(700);
    await shot(page, `${label}-06-desk-owner-card.png`);
    await clickText(page, "#flow button", "Approve");
    await sleep(900);
    await shot(page, `${label}-07-desk-owner-approved.png`);
    await clickText(page, "#sc-hostile button", "Message tries");
    await sleep(700);
    await shot(page, `${label}-08-desk-hostile-blocked.png`);
    await clickText(page, "#sc-compromised button", "invents a refund");
    await sleep(700);
    await shot(page, `${label}-09-desk-compromised.png`);
    await clickText(page, "#sc-faults button", "Cut the line");
    await sleep(900);
    await shot(page, `${label}-10-desk-unresolved.png`);
    await clickText(page, "#flow button", "Re-check");
    await sleep(900);
    await shot(page, `${label}-11-desk-recheck.png`);
    await audit(page, label + "/desk-after", phone);
    await page.close();

    // ---- policy builder ----
    page = await open(browser, label + "/policy", w, h);
    await login(page, "/policy.html");
    await shot(page, `${label}-12-policy-start.png`);
    await page.type("#rule", "refund up to $30 per refund");
    await page.click("#preview");
    await sleep(600);
    await shot(page, `${label}-13-policy-refused.png`);
    await page.$eval("#rule", (e) => { e.value = ""; e.dispatchEvent(new Event("input")); });
    await page.type("#rule", "refund up to $30 per order; daily budget of $90");
    await page.click("#preview");
    await sleep(600);
    await shot(page, `${label}-14-policy-preview.png`);
    await audit(page, label + "/policy", phone);
    if (!phone) {
      await page.click("#save");
      await sleep(800);
      await shot(page, `${label}-15-policy-saved.png`);
    }
    await page.close();

    // ---- arena (public) ----
    page = await open(browser, label + "/arena", w, h);
    await page.goto(BASE + "/arena.html", { waitUntil: "networkidle0" });
    await shot(page, `${label}-16-arena-start.png`);
    const preset = await page.$("button.preset");
    if (preset) { await preset.click(); await sleep(300); await page.click("#send"); await sleep(1000); }
    await shot(page, `${label}-17-arena-attempt.png`);
    await audit(page, label + "/arena", phone);
    await page.close();

    // ---- inbox, court, demo, buyer ----
    page = await open(browser, label + "/inbox", w, h);
    await login(page, "/inbox.html");
    await sleep(600);
    await shot(page, `${label}-18-inbox.png`);
    await audit(page, label + "/inbox", phone);
    await page.close();
    page = await open(browser, label + "/court", w, h);
    await page.goto(BASE + "/court.html?id=A-0001", { waitUntil: "networkidle0" });
    await shot(page, `${label}-19-court.png`);
    await audit(page, label + "/court", phone);
    await page.close();
    page = await open(browser, label + "/demo", w, h);
    await page.goto(BASE + "/demo.html", { waitUntil: "networkidle0" });
    await shot(page, `${label}-20-demo.png`);
    await page.close();
  }
  // keyboard: can the desk be driven without a mouse? (first tab stops)
  const page = await open(browser, "desktop/keyboard", 1920, 1080);
  if (want.includes("desktop")) {
  await login(page, "/desk.html");
  const stops = [];
  for (let i = 0; i < 6; i++) {
    await page.keyboard.press("Tab");
    stops.push(await page.evaluate(() => { const a = document.activeElement; const cs = getComputedStyle(a); return (a.textContent || a.id || a.tagName).trim().slice(0, 40) + " | outline:" + cs.outlineStyle + " " + cs.outlineWidth; }));
  }
  fs.writeFileSync(path.join(OUT, "keyboard-tab-stops.txt"), stops.join("\n") + "\n");
  }
  await page.close();
  await browser.close();
  fs.writeFileSync(path.join(OUT, "findings.json"), JSON.stringify(findings, null, 1));
  // A finding FAILS the run unless it is on the short list of expected ones below. Set BROWSER_PASS_ALLOW_FINDINGS=1 to
  // record findings without failing (for example when looking at a page that is known to be broken).
  const unexpected = findings.filter((f) => !EXPECTED.some((rx) => rx.test(f.kind + " " + f.detail)));
  fs.writeFileSync(path.join(OUT, "unexpected-findings.json"), JSON.stringify(unexpected, null, 1));
  console.log(findings.length + " findings (" + unexpected.length + " unexpected) written to " + path.join(OUT, "findings.json"));
  if (unexpected.length && !process.env.BROWSER_PASS_ALLOW_FINDINGS) {
    console.error("FAIL: " + unexpected.length + " unexpected finding(s); see unexpected-findings.json");
    process.exit(2);
  }
})().catch((e) => { console.error(e); process.exit(1); });
