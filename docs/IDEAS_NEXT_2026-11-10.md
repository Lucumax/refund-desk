# Next ideas from the Nov 10 team call (not built; not in the submitted build)

## 1. Buyer chat with an order card (Xin Liang Chia's sketch, `ideas_xin_buyer_chat_sketch_2026-11-10.png`)
Buyer: "I want to refund $30." Agent: "Which order?" Buyer: "My bicycle, $500." Agent shows the order as a card ("Is it this order?") with a box to confirm. Buyer: "Yes." Agent: "Here you go, refund success."
How it fits the current design:
- The card must come from the buyer's own orders (the order link or a signed-in buyer), never from the words typed; the buyer picks it with a tap, so the model still has no field that picks an order.
- The tap on "Yes" is a structured consent, so fewer honest requests need the model's consent reading (today 9 of 42 ordinary requests end in a question).
- The rule still decides: under the default rule a $30 request is over the $25 automatic limit, so the last bubble would say it went to the shop owner, not "refund success".

## 2. Agent to agent
The buyer's own AI agent asks the shop's Refund Desk for the refund. Nothing in the authority changes: the shop's limits cap what any buyer-side agent can get, however it words the request.

## 3. Other money flows
The same pattern (AI suggests, owner-set limits decide, owner approves the rest) for disputes, partial refunds on subscriptions, and payouts.
