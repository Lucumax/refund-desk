# Changes

A short, public summary by area, newest first. Measured numbers live in `README.md` (sections LIVE results and Fixture numbers) and in `eval/`; each area names its tests. The full commit-by-commit history is in git.

## Pre-freeze changes (2026-11-05)

- Demo exchange rates are now a fixture-mode feature only: in `--mode sandbox` a message that names another currency is not converted and waits for a person (`FOREIGN_CURRENCY_HELD`). Tests: `tests/test_fx_mode_lock.py`.
- The model's reading was tuned on development cases only (Dhanush M1 B04, B05, B10): a completed return keeps its reason when the model gives none, and the consent prompt no longer treats anger, a corrected typo or a why-I-return-it sentence as flags. Every veto is unchanged. Tests: `tests/test_ai_usefulness.py`. Not re-run live.
- PX-22.1 ("18 euros") stays labelled an ordinary request; counts reconciled (attack runs 129 under today's labels, 130 as run live).

## Policy answers agreed with Dhanush Kankanala (2026-11-02)

- A message that cites a PayPal transaction id other than the ticket's now goes to the owner (gate rule G19, a hold) instead of being refused (G3) or sent back to the buyer. The refund can only go to the ticket's own capture. A different order number is still sent back to the buyer.
- A figure in EUR, GBP, CAD or AUD is converted to US dollars at fixed demo rates (`app/fx.py`: 1 EUR = 1.08 USD, 1 GBP = 1.27 USD, 1 CAD = 0.73 USD, 1 AUD = 0.65 USD) before anything reads the message, instead of the buyer being asked; every limit is checked in dollars, the conversion is shown with its rate next to the buyer's words on every screen and signed with the decision. Demo rates, not market rates; simulation only. What cannot be converted still waits for the owner. Tests: `tests/test_dhanush_policy_answers.py`, `tests/live_policy_helper.py` (what moved in the committed live recordings' replays).

## Live model evidence (2026-11-02 and 2026-11-05)

- Run 3, 2026-11-05, with today's code and labels (`claude-haiku-5-5`, $0.0532 on 222 calls, development sets): attack runs released 0 of 22 (Dhanush), 0 of 30, 0 of 77; limit breaches 0 and unauthorized releases 0 among 47 automatic refund attempts; honest requests by themselves 9 of 13, 2 of 4, 8 of 11, 21 of 42; the dev corpus still releases 5 of 24 attack-labelled messages ($96.00, inside the limits). Files: `eval/live_run3_2026-11-05_console.txt`, `eval/spend_ledger_live_2026-11-05.json`, `eval/consent_recordings_live_2026-11-05.json`.

- A real model (`claude-haiku-5-5`) read buyer messages on four development sets (`eval/live/2026-11-02_haiku/`) and, after one validator fix, again on Dhanush Kankanala's set (`eval/live/2026-11-02_haiku_v2/`) and on the other three sets (`eval/live/2026-11-02_haiku_v2_other3/`, outcome digests identical to run 1). Console output, saved model replies, spend ledgers and an offline replay check are committed. Tests replay them offline (`tests/test_live_evidence_run1.py`, `tests/test_live_evidence_run2.py`, `tests/test_live_evidence_run2_other3.py`, `tests/test_replay_extractor.py`).
- Fix: a buyer's own PayPal transaction id (or another verbatim reference) is kept as evidence and never taken as the order; before, the whole model reply was thrown away (21 of 22 replies on Dhanush's set in run 1, 4 of 22 in run 2). Tests: `tests/test_extractor_txn_refs.py`.
- A run that saves its recordings no longer crashes on a bad file name, prints its results first, writes atomically, and also saves every rejected consent reply and every live extractor exchange (`tests/test_live_run_resilience.py`, `tests/test_live_diagnostics.py`).

## Spend guard (`app/spend_guard.py`)

A USD cap on live model calls: each call is reserved (an upper bound on the input tokens plus the full `max_tokens` at the configured model's price) before it is sent, a call that would pass the cap is never sent, the arena reader shares the same guard, guards on one ledger file share one budget under a file lock, a call that costs more than was reserved halts the run, a ledger is written, a dry run prints the plan, and a run stopped by the cap is labelled PARTIAL. Tests: `tests/test_spend_guard.py`.

## Desk screen

The transaction summary appears before the approval controls, the audit strip sits next to the decision, strings are plainer, and the stylesheet ranks the stamp, the two panels and the buttons (`tests/test_desk_summary_and_audit_strip.py`, `tests/test_ui_rules.py`).

## Reader and consent

- Model-read consent with the rules as backstop (`app/consent_reader.py`, `app/consent_combine.py`): rules say PASS, UNREAD or HAZARD; a model reading can only tighten, except for one clause type the rules could not read.
- Whole-clause consent: a request fragment inside a negated, withdrawn, reported, conditional, timed or redirected sentence holds the message; figures must be counted and tied to the refund words; other currencies hold; one automatic refund per order. Tests: `tests/test_consent_allowlist.py`, `tests/test_consent_conditions.py`, `tests/test_consent_dependent.py`, `tests/test_consent_model.py`, `tests/test_legit_refunds.py`.
- Ordinary refunds go out (seven refund reasons, wider consent, threats not tone), and Dhanush Kankanala's Milestone 1 development set is a labelled evaluation set (`eval/dhanush_m1_corpus.json`, `tests/test_dhanush_m1.py`).
- A sentence about someone else's order is UNREAD (rules alone hold it) (`tests/test_public_docs_and_unread_orders.py`, `tests/test_prefetch_hardening.py`).
- Two outside red-team sets are kept as labelled development data (`eval/codex_redteam_2026-11-01.json`, `eval/codex_redteam_pass2_2026-11-01.json`; `tests/test_redteam_round1.py`, `tests/test_redteam_round2.py`).

## Concurrency and limits

Model calls run before the application-wide lock; a wall-clock deadline (default 45 s) and a cap of 8 model calls in flight end a slow call as a reader failure; a reply size limit; a prefetch is single use and bound to the readers in force; a failed model stage costs one slot and is not retried under the lock. Tests: `tests/test_model_calls_outside_lock.py`, `tests/test_model_deadline.py`, `tests/test_prefetch_hardening.py`, `tests/test_prefetch_concurrency.py`.

## Authority, money and recovery

Every automatic and owner-approved refund goes through one authority (`app/authority.py`) that writes a signed decision before any send; recovery judges a crashed approval again under the policy in force; signed chains are checked against their checkpoints; terms and order bindings are checked on the raw and the canonical text; a refused PayPal login on a re-send keeps the refund UNKNOWN. Tests: `tests/test_authority_fixes.py`, `tests/test_authority_binding.py`, `tests/test_decision_binding.py`, `tests/test_order_amount_binding.py`, `tests/test_chain_integrity.py`, `tests/test_recovery.py`, `tests/test_resend.py`, `tests/test_adapter_layers.py`, `tests/test_money_reservations.py`.

## Policy, evaluation and export

Policy Builder with preview and signed versions (`tests/test_policy_builder.py`, `tests/test_policy_api.py`); fixture evaluation corpora with a sealed manifest and a repro command (`tests/test_evaluation_corpora.py`, `eval/RESULTS_2026-10-24.md`); a clean-export check that exports a commit, leaves out internal notes, and runs the tests offline with no keys (`scripts/clean_export_check.py`, `tests/test_clean_export.py`).

## Design pass

Verdict stamp, "The AI suggested" apart from "Your rule decided", 16 px type, one column at 375 px, a phone approval card (`tests/test_ui_rules.py`, `tests/test_display_stamp.py`, `tests/test_phone_card_js.py`, `scripts/ui_check.js`).

## Contributors

Xin Liang Chia: regression tests for a refused PayPal login during a re-send and for an owner-approved refund through the web routes, and the matching adapter guard. Dhanush Kankanala: the Milestone 1 development set and its cases. Test count and results are in `README.md`.
