"""Replay the recorded LIVE consent replies of a live run through the gate, offline (no key, no network, no model call).

    python scripts/replay_live_recordings.py eval/live/2026-11-02_haiku/consent_recordings.json
    python scripts/replay_live_recordings.py --extractor eval/live/2026-11-02_haiku_diag/consent_recordings.json   (also re-validates the
    recorded live extractor replies of the diagnostic run with the current code)
    python scripts/replay_live_recordings.py --extractor FILE --set dev   (the same for one other set of the file)

Prints, per set, the outcome digest. The extractor is the scripted test model here, not the live one, so a digest that equals the
live run's digest shows that the live extractor changed no outcome. A step with no recording is read as a rejected reply (the rules
answer alone), exactly as in the live run where that reply was rejected."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from app import evaluation as ev  # noqa: E402
from app import fx  # noqa: E402
from app.scripted import ScriptedCompleter, _BOUND  # noqa: E402
from app.arena_text import normalize  # noqa: E402

SETS = ("dhanush_m1", "codex_redteam", "codex_pass2", "dev")


def replay(path: str) -> dict:
    old, ev.CONSENT_RECORDINGS = ev.CONSENT_RECORDINGS, path
    try:
        return {name: ev.evaluate(ev.SETS[name], None, "recorded") for name in SETS}
    finally:
        ev.CONSENT_RECORDINGS = old


class RecordedExtractorReplies(ScriptedCompleter):
    """The saved raw replies of a LIVE extractor, keyed by the buyer's message. A call that failed live (raw reply None, for example
    a refusal) fails again here. Not a model. The reply goes through the CURRENT ModelExtractor validator, so this re-judges the live
    replies with today's code (a different validator than the live run used, if the code changed since)."""
    model = "recorded live extractor replies (not a model)"

    def __init__(self, exchanges: list, texts: dict):
        super().__init__({texts[x["step"]]: x["raw_reply"] for x in exchanges if x["step"] in texts})

    def __call__(self, user, system=None):
        m = _BOUND.search(user)
        raw = self.replies.get(normalize(m.group(1) if m else user), self.default)
        if raw is None:
            raise RuntimeError("the recorded live call had no reply")
        return raw


def replay_extractor(path: str, name: str = "dhanush_m1") -> dict:
    """Replay one set with BOTH the recorded consent replies and the recorded live extractor replies (the file's
    `extractor_exchanges`), validated by the current extractor code. Offline, no key."""
    import json
    rec = json.load(open(path, encoding="utf-8"))
    corpus = ev.load_corpus(ev.SETS[name])
    # the text the extractor is asked about is the WORKING text (a figure in another currency already in dollars, app/fx.py): a
    # live reply recorded on the buyer's own words is offered for that working text, and the validator judges it as it would live
    texts = {s["id"]: fx.convert_text(s["text"])[0] for c in corpus["cases"] for s in c["steps"]}
    ex = [x for x in rec.get("extractor_exchanges", []) if x.get("set") == name]
    old, ev.CONSENT_RECORDINGS = ev.CONSENT_RECORDINGS, path
    try:
        return ev.evaluate(ev.SETS[name], RecordedExtractorReplies(ex, texts), "recorded")
    finally:
        ev.CONSENT_RECORDINGS = old


if __name__ == "__main__":
    if len(sys.argv) in (3, 5) and sys.argv[1] == "--extractor":
        # optional: --extractor FILE --set NAME (any of SETS; default dhanush_m1)
        setname = sys.argv[4] if len(sys.argv) == 5 and sys.argv[3] == "--set" else "dhanush_m1"
        if setname not in SETS or len(sys.argv) == 5 and sys.argv[3] != "--set":
            raise SystemExit(__doc__)
        r = replay_extractor(sys.argv[2], setname)
        print(f"{setname} with recorded consent AND recorded live extractor replies, validated by the current code: "
              f"digest {r['outcome_digest']}  attacks released {r['attacks']['released_automatically']}  "
              f"honest auto {r['legitimate']['completed_automatically']} of {r['legitimate']['n']}  "
              f"limit breaches {r['numeric_limit_breaches']['count']}  unauthorized {r['unauthorized_releases']['count']}")
        raise SystemExit(0)
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    for name, r in replay(sys.argv[1]).items():
        print(f"{name}: digest {r['outcome_digest']}  attacks released {r['attacks']['released_automatically']}  "
              f"honest auto {r['legitimate']['completed_automatically']} of {r['legitimate']['n']}  "
              f"limit breaches {r['numeric_limit_breaches']['count']}  unauthorized {r['unauthorized_releases']['count']}")
