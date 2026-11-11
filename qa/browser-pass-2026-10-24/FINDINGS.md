# Real-browser pass, 2026-10-24 (simulation, Chrome headless via puppeteer-core)

Script: `scripts/browser_pass.js`. Ran against `python -m app --fresh` (synthetic PayPal, no keys) at 1920x1080 and 390x844. Only a selection of the screenshots is kept here (`findings-*.json` has the raw automated findings of the 2026-10-24 run). The 13 screenshots were re-taken on 2026-11-09 with the same script from the current app (header reads Refund Desk); the findings and the fixes above are from the 2026-10-24 run and were not repeated, and the 2026-11-09 run stopped early at the phone inbox page (a navigation abort), so the later phone pages were not re-shot.

Found and fixed in this pass
1. Scenario buttons: the title and the order id ran together ("...ruleORD-1001") and the order id broke across lines at the hyphen. Fixed in `static/app.css` (flex, no wrap on the id).
2. Recent-requests list: the status chip touched the text next to it. Fixed (spacing).
3. The rule that allowed a request ("R9 hit") was drawn in the alarm color. It is now green when the decision is ALLOW (desk and arena).
4. Policy Builder at phone width scrolled sideways by 5 px (grid column was wider than the screen). Fixed (`minmax(0, 1fr)` and `min-width: 0`).
5. `/favicon.ico` returned 404 and put an error in the console. It now answers 204.

Not defects, noted
- Two "401 Unauthorized" lines in the console of the desk page before sign-in: the page asks `/api/session` to find out whether you are signed in. Expected.
- The arena page does not send a message when a preset is clicked; it loads it into the box and the visitor presses Send. By design.
- Keyboard: Tab reaches the navigation and the scenario buttons in order and every focused control shows a 3 px outline (`keyboard-tab-stops.txt`). Not tested with a screen reader.
- Automated checks found no unlabeled inputs, no unnamed buttons, no tap target under 40 px at phone width and no horizontal overflow after the fix.

Not covered
- Safari and Firefox, a real phone, a screen reader, color-blind simulation, print, and the sandbox mode (no keys).
