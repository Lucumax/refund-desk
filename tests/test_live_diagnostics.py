"""Live run diagnostics (2026-11-02). Written first (red). Offline only: the HTTP opener is replaced, no request leaves the machine.

The first live consent run (Haiku 5.5) left two things unexplained because the evidence was thrown away:
- one consent reading was REJECTED (dhanush_m1, the base64 message) and its raw reply was never saved, only accepted replies were;
- 21 of the 22 live extractor reads on Dhanush's set were rejected (MODEL_OUTPUT_REJECTED) and the extractor's raw reply was never kept.
Rules pinned: a live run with --save-consent also saves (a) every rejected consent reply, with the step and the reader's verdict, and
(b) every LIVE extractor exchange, with its verdict. Both go in extra top-level keys of the recordings file; `entries` still holds
accepted consent replies only, so a replay with --consent recorded is unchanged."""
import json

from app import consent_doubles as cd
from app import evaluation as ev
from test_spend_guard import _fake_http, _run

USAGE = {"input_tokens": 500, "output_tokens": 100}
PROSE = "I cannot help with that."
READING = {"verdict": "NOT_A_REQUEST", "flags": [], "request_quote": None, "reason": "no request here"}


def _live(monkeypatch, capsys, tmp_path, text, name="dhanush_m1"):
    _fake_http(monkeypatch, usage=USAGE, model="claude-haiku-5-5", text=text)
    f = tmp_path / "rec.json"
    rc, out = _run(["repro", "--set", name, "--consent", "live", "--save-consent", str(f)], monkeypatch, capsys)
    assert rc == 0, out
    return json.loads(f.read_text(encoding="utf-8")), out


def test_rejected_consent_replies_are_saved_with_step_verdict_and_raw_text(monkeypatch, capsys, tmp_path):
    data, _ = _live(monkeypatch, capsys, tmp_path, PROSE)
    assert data["entries"] == []                                                       # nothing was accepted
    rej = data["rejected_readings"]
    assert rej and all(set(r) >= {"step", "text", "verdict", "raw_reply"} for r in rej)
    assert all(r["verdict"].startswith("REJECTED:") and r["raw_reply"] == PROSE for r in rej)


def test_accepted_replies_stay_in_entries_only_and_replay_is_unchanged(monkeypatch, capsys, tmp_path):
    data, _ = _live(monkeypatch, capsys, tmp_path, json.dumps(READING))
    assert data["entries"] and all(e["parse_status"] == "json" for e in data["entries"])
    assert not data["rejected_readings"]
    client = cd.load_recordings(str(tmp_path / "rec.json"))                            # extra keys do not disturb the loader
    assert client.by_key


def test_live_extractor_exchanges_are_saved_with_their_verdict(monkeypatch, capsys, tmp_path):
    data, _ = _live(monkeypatch, capsys, tmp_path, json.dumps(READING))                # a reading is not an extraction: BAD_KEYS
    ex = data["extractor_exchanges"]
    assert len(ex) == 22                                                               # Dhanush's 22 scripted-model steps run on the live model
    assert all(set(x) >= {"step", "verdict", "raw_reply"} for x in ex)
    assert all(x["verdict"] == "REJECTED:BAD_KEYS" and x["raw_reply"] == json.dumps(READING) for x in ex)


def test_the_counts_of_readings_do_not_change(monkeypatch, capsys, tmp_path):
    _, out = _live(monkeypatch, capsys, tmp_path, PROSE)
    assert "consent readings 24 (accepted 0, rejected 24)" in out       # was 22 until 2026-11-02: M1-A11 and M1-B08 now reach the consent reader


def test_no_extractor_exchanges_without_a_live_extractor(tmp_path):
    r = ev.evaluate(ev.SETS["dhanush_m1"], None, "ideal")
    assert "extractor_exchanges" not in r
