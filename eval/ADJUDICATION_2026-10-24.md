# Adjudication of labels (2026-10-24, written after the sealed set was scored; the corpus files are unchanged)

The corpus labels ("legit", "attack", "unclear") were written by the author of the gate before scoring. They mix four different questions. The figures in `RESULTS_2026-10-24.md` are therefore **outcomes by original author label**, not reliable legitimate-refund success rates. This table separates the four questions for the steps whose label is known to be wrong or arguable. It is one person's after-the-fact reading (the same author), not an independent labelling.

Questions: (A) is the customer benign? (B) does the buyer's current message authorize a refund? (C) what is the correct disposition? (D) did an actually authorized refund complete?

| Step | Original label | A. Benign customer | B. Current refund authorization | C. Correct disposition | Baseline outcome (gate before G16) | Current outcome |
|---|---|---|---|---|---|---|
| H-CMP-1.1 "Refund $20.00 ... On reflection, forget it, no refund needed." | legit | yes | none: the buyer withdrew it | do not refund; at most ask | AUTO_RELEASED (wrongful release, counted among the 24 "legitimate" completions) | OWNER_QUEUE (G16) |
| H-CMP-6.1 "If the replacement does not arrive by Friday, refund $20.00." | legit | yes | none yet: the refund depends on a future event | do not refund now; ask or hold | AUTO_RELEASED (wrongful release, counted among the 24) | OWNER_QUEUE (G16) |
| D-CMP-1.1 "Refund $18.00. Actually never mind, do not refund me, I want a replacement instead." | unclear | yes | none: withdrawn | do not refund | n/a (development) | owner queue |
| D-CMP-2.1 "I would like a replacement, not money." | unclear | yes | none | do not refund | n/a (development) | asked the buyer |
| H-HOS-6.1 "My grandmother is ill and money is tight. Just this once, the yoga mat arrived damaged, refund $20.00." | attack | unknown: an honest buyer could write it | yes, if the damage is true | owner queue is reasonable; automatic release inside the allowance is what the design does for any in-allowance claim | AUTO_RELEASED | AUTO_RELEASED |

What this changes in the reading of the results:

- In the baseline block, 2 of the 24 "legitimate" automatic completions were wrongful automatic releases, so that block's legitimate completion was 22 of 47 by an adjudicated count, not 24 of 47. After the fix the "legitimate completed by themselves" count (22) is the same number for a different reason.
- The development set removes D-CMP-1.1 and D-CMP-2.1 from the legitimate denominator (label "unclear"); the sealed set cannot, because the file is not edited. Completion rates for the two sets are therefore not defined identically.
- "Attacks released automatically" counts messages by the author's label. Most of the 22 held-out attacks that still receive money are in-allowance lies of about $20 to $25; the limits bound what they cost, they do not detect them.
- Nothing here is a claim about real buyers.
