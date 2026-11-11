# Demo run on the PayPal sandbox

One full run of the Refund Desk against Xin's own PayPal sandbox app, captured step by step for the demo: a purchase, the transaction at PayPal, a refund held for the owner and approved, and the same PayPal transaction with the refund attached. Branch `legit-refunds` at commit `a93c994`, sandbox mode, rule-based reader (no model key was set). Sandbox money only. The app header in images 4 to 16 reads "Refund Desk (working name)": commit a93c994 is from before the product was renamed, which is the only change a user can see in the app since then (see NOT_DONE.md).

The PayPal pages are from the shop's own sandbox account (Test Store), so their transaction ids are the same ids the app records. Names, addresses and emails are blurred; before publishing, the blurred areas on the PayPal pages were also covered with solid blocks, and the session id in the raw record on image 15 was removed. PayPal shows times in MYT (UTC+8).

## What happened (2026-11-11, UTC)

| Time (UTC) | Step | Record |
|---|---|---|
| 04:52:31 | The buyer pays $100.00 at PayPal checkout | capture `18M52409SM456912Y`, COMPLETED |
| 05:15:34 | From the order link, the buyer asks for $30.00; the request is held for the owner | PENDING_APPROVAL, rule R7 ($30.00 is over the $25.00 automatic limit) |
| 05:16:24 | The owner approves | AUTHORIZED |
| 05:16:25 | PayPal creates the refund | refund `88F340648J861040S`, COMPLETED |
| after | Read-back from PayPal's refund resource | refund id, status, amount, currency and reference all match |

The receipt page then verified the signed records ("Chain intact"), and at PayPal the payment `18M52409SM456912Y` shows as Partially refunded with the refund `88F340648J861040S` listed under it.

## Screenshots

Each image is the whole page.

The purchase
1. [01-paypal-checkout-pay-100.png](01-paypal-checkout-pay-100.png): the buyer pays $100.00 at PayPal checkout.
2. [02-capture-terminal.txt](02-capture-terminal.txt): the payment is captured, `18M52409SM456912Y`, $100.00, COMPLETED.
3. [03-paypal-shop-payment-received-100-before-refund.png](03-paypal-shop-payment-received-100-before-refund.png): at PayPal, Payment received, $100.00, Completed, no refund yet.

The refund request
4. [04-home-sandbox.png](04-home-sandbox.png): the app runs connected to the sandbox app.
5. [05-buyer-order-link.png](05-buyer-order-link.png): the buyer opens the page from the order link, so the order comes from the link.
6. [06-buyer-message-typed.png](06-buyer-message-typed.png): the buyer asks for $30.00 because the order never arrived.
7. [07-buyer-sent-held-for-owner.png](07-buyer-sent-held-for-owner.png): the buyer is told a person at the shop is looking at the request.

The owner decides
8. [08-desk-owner-sign-in.png](08-desk-owner-sign-in.png): the owner signs in to the Refund Desk.
9. [09-desk-request-waiting.png](09-desk-request-waiting.png): the request waits under Recent requests.
10. [10-desk-held-for-owner-rule-R7.png](10-desk-held-for-owner-rule-R7.png): held under rule R7; nothing was sent.
11. [11-phone-approval-card.png](11-phone-approval-card.png): the same request on the owner's phone, where one tap approves exactly this refund.
12. [12-desk-approved-refunded-30.png](12-desk-approved-refunded-30.png): approved, and the $30.00 refund is sent to PayPal.
13. [13-desk-read-back-matches-and-rule-trace.png](13-desk-read-back-matches-and-rule-trace.png): the read-back matches, and the rule trace shows R7.

Confirmation
14. [14-buyer-refund-went-through.png](14-buyer-refund-went-through.png): the buyer sees that the refund went through.
15. [15-receipt-chain-verified.png](15-receipt-chain-verified.png): the receipt, with PayPal's own record and every step timed.
16. [16-merchant-inbox-refunded.png](16-merchant-inbox-refunded.png): the merchant inbox, one refund of $30.00 confirmed by PayPal.

The refund attached to the transaction at PayPal
17. [17-paypal-shop-payment-partially-refunded-refund-attached.png](17-paypal-shop-payment-partially-refunded-refund-attached.png): the same payment is now Partially refunded, with the $30.00 refund under Transaction Activity.
18. [18-paypal-shop-refund-30-transaction-id.png](18-paypal-shop-refund-30-transaction-id.png): the refund itself, `88F340648J861040S`, Completed, linked back to the $100.00 payment.

## All sandbox refunds on `legit-refunds` so far

Every run used ORD-2001, a $30.00 request held under R7 and approved by the owner.

| Date (UTC) | Capture | Refund | Read-back |
|---|---|---|---|
| 2026-11-08 20:44 | `8B932071A70770501` ($50.00) | `07V97493U1245522M`, COMPLETED | not recorded |
| 2026-11-11 00:42 | `1T878701US3110704` ($100.00) | `61L560487V4315809`, COMPLETED | not recorded |
| 2026-11-11 03:33 | `00L01267R2109042J` ($100.00) | `6UH46758T3763373F`, COMPLETED | matched |
| 2026-11-11 03:39 | `00L01267R2109042J` ($100.00) | `86196657E4675643L`, COMPLETED | matched |
| 2026-11-11 05:16 | `18M52409SM456912Y` ($100.00) | `88F340648J861040S`, COMPLETED | matched (the screenshots above) |

"Not recorded" means the read-back was not run in the app for that refund; it does not mean a mismatch. The two 03:33 and 03:39 runs refunded the same capture from two separate data folders (two takes of the screenshots). Earlier runs (2026-10-25 and 2026-10-28) are listed in `docs/SANDBOX_TESTING.md`.

Not shown here: a real model as the reader, a lost PayPal answer, and a PENDING refund. The sandbox cannot force the last two.
