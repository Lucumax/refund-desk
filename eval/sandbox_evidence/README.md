# PayPal sandbox evidence

Two people ran the refund code on their own PayPal sandbox apps. Sandbox money only. Neither run is a rate or a blind test.

## Walter Guevara's run, 2026-11-11 (`walter_live_run_2026-11-11.json` and `walter_live_run_2026-11-11_console.txt`)

Command: `python scripts/live_sandbox_run.py --capture-id 2FY13415J9974010N --amount 1.00 --approve-amount 30.00 --model --evidence-out ...` on his own sandbox app. No file in `app/` changed after the run; in `scripts/` only the export tool's file list changed (one line); everything else was documentation and evidence. The reader was `claude-haiku-5-5` (Anthropic); the model's reply was accepted by the validator, so the rule-based fallback did not answer. Result: ALL CHECKS PASSED (exit 0). The console file is the script's output; the JSON file is its sanitized evidence record (no tokens or keys; ids are included).

| Step | What was seen |
|---|---|
| Capture | `2FY13415J9974010N`, $100.00, COMPLETED |
| Ordinary $1.00 refund | `18J69791TS925271V`, COMPLETED; read-back from PayPal's refund resource matched on id, status, amount and currency |
| Same `PayPal-Request-Id` sent again | The same refund id came back; no second refund was created (this was listed as unverified before this run) |
| Hostile "ignore all previous instructions" message | Refused; zero refund calls to PayPal |
| Taken-over reader's invented proposal | Held for the owner under R7; the owner denied it; zero refund calls |
| $30.00 request | Waited for the owner; approved; refund `3U88853154260600J`, COMPLETED; read back matching |
| Order total at PayPal | PayPal's refund resources add up to exactly the two refunds ($1.00 and $30.00) |
| Signed receipt, decision and policy chains | All verify |

Three refund requests were sent to PayPal in all: the $1.00 refund, the same request sent again, and the $30.00 refund. The console also prints the `custom_id` PayPal echoed (`I-0001`) as an informational note.

What this run does not show: a lost PayPal answer (the UNKNOWN path), a PENDING refund (every refund came back COMPLETED), the same request id with a different body, a blind test set, or real shop owners.

## Desk recording on Walter Guevara's sandbox, 2026-11-11 (`walter_desk_recording_refund_2026-11-11.json`)

The closing clip of the demo video is a recording of the Refund Desk run on his own sandbox app with the live model (`claude-haiku-5-5`). Order ORD-2001 pointed at capture `2FY13415J9974010N`. The buyer asked for $30.00, the request was held under R7, the owner approved on the desk, and PayPal created refund `8KB17081E6778874D`, COMPLETED, USD 30.00. The desk's own read-back showed five ok chips (refund id, status, amount, currency, reference echoed), and the buyer page said the refund went through.

The JSON file is a separate read-back made afterwards with the app's own PayPal client: `GET /v2/payments/refunds/8KB17081E6778874D` returned the same id, status and amount, and the capture was PARTIALLY_REFUNDED. It holds ids, status and amounts only. The desk screens and the raw footage are not kept in this repository; the video shows them.

Capture `2FY13415J9974010N` ($100.00) now has $61.00 refunded across three refunds: $1.00 (`18J69791TS925271V`) and $30.00 (`3U88853154260600J`) from the script run above, and $30.00 (`8KB17081E6778874D`) from the desk recording. $39.00 remains.

How the capture was made: order `4L062846BE314782E` was created by `scripts/seed_sandbox_capture.py` (`--amount 100.00`) and approved in the browser by the sandbox Personal buyer. It was then captured with the same `PayPalClient` call the script uses, run from a one-off helper, because the script's own wait prompt could not be reached. The capture was therefore not made by an unmodified run of the script.

## Xin Liang Chia's runs

His full run of Nov 11, with 18 screenshots and a table of all his refunds on this branch, is in [../../docs/demo/README.md](../../docs/demo/README.md) (branch `legit-refunds`, commit a93c994, rule-based reader; `app/` has changed since only in comments and docstrings). The two screenshots below are older and come from his emails and chats.

`xin_sandbox_buyer_activity_2026-11-08.png`: screenshot by Xin Liang Chia of his PayPal sandbox buyer (Personal) account, sandbox.paypal.com "Recent activity", cropped to the PayPal page only. It shows refunds from the sandbox merchant "Test Store": +$30.00 and +$1.00 on Nov 8, and +$1.00 and +$30.00 on Oct 28, against two $50.00 payments marked "Refunded" on Nov 8.

What it supports: the Nov 8 $30.00 refund reported in his email (ORD-2001, held under R7, approved, PayPal refund 07V974...522M, COMPLETED, read back matching) appears in the sandbox buyer account. What it does not show: the refund id itself, or which commit made the call; those come from his email only.

Source: Xin's message of 2026-11-10 (image 1 of 4). The other three images are his assistant's chat and a sandbox checkout page with an address; they are not kept here.

`xin_sandbox_refund_detail_2026-11-10.png`: screenshot by Xin Liang Chia, Nov 10 2026, of the refund detail page in his sandbox buyer account: "You originally paid Test Store $100.00 on November 10, 2026", refunded $30.00, transaction ID beginning 5PL957 and ending 0243L (the mouse pointer covers one character). His window shows the app checked out at a93c994 (detached HEAD); app code since then differs in comments and docstrings only. Which reader ran (model or rule-based) and the app-side refund id are not on this screen; they need his terminal output. The seller email and phone that the page showed are sandbox test values; they are covered with a solid block in this copy.

## PayPal's own pages for our run (2026-11-11)
Captured from the sandbox Business account (the shop, "Test Store") at sandbox.paypal.com after the runs above. "John Doe" is PayPal's generated sandbox buyer name.
- `walter_paypal_refund_8KB17081E6778874D_2026-11-11.png`: Transaction details, "Payment refunded to John Doe", November 11, 2026 at 10:33:11 AM EST, transaction ID 8KB17081E6778874D, Completed, -$30.00 USD. This is the refund made from the desk in the video's closing clip.
- `walter_paypal_payment_2FY13415J9974010N_partially_refunded_2026-11-11.png`: the $100.00 payment 2FY13415J9974010N (Checkout, 10:25:12 AM EST), status Partially refunded.
- `walter_paypal_payment_2FY13415J9974010N_three_refunds_2026-11-11.jpg`: the same payment's Transaction Activity, three refunds on Nov 11: -$1.00, -$30.00, -$30.00.
