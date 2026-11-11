"""Live run resilience (2026-11-01). Written first (red). Offline only: the HTTP opener is replaced, no request leaves the machine.

The first live consent run (Haiku 5.5, dhanush_m1) crashed while SAVING the recordings: the model had wrapped its JSON in a markdown
fence, the reader tolerates one fence (so the reply was ACCEPTED), but the saver called json.loads on the raw text. The results were never
printed and the recordings file was left empty. Rules now pinned:
- saving never raises, whatever the reply looked like; an unparseable reply is stored as raw text plus a parse status;
- results are printed whether or not the save works; a failed save is reported and keeps the old file intact;
- a reply that is not a clean reading is treated as NO reading: nothing is released automatically, and the messages are held;
- a rerun with the same ledger runs the whole set again under the remaining cap; the ledger accumulates.
"""
import json
from decimal import Decimal

import pytest

from app import consent_doubles as cd
from app import evaluation as ev
from app.consent_reader import ModelConsentReader
from test_spend_guard import _fake_http, _run

D = Decimal
READING = {"verdict": "NOT_A_REQUEST", "flags": [], "request_quote": None, "reason": "no request here"}
FENCED = "```json\n" + json.dumps(READING) + "\n```"


# ---- one recording entry -----------------------------------------------------------------------------------
def test_a_plain_json_reply_is_stored_parsed_with_status_json():
    e = ev.recording_entry({"text": "t", "raw_reply": json.dumps(READING)})
    assert e["reply"] == READING and e["parse_status"] == "json" and e["raw_reply"] == json.dumps(READING)


def test_a_fenced_reply_is_stored_parsed_with_status_fenced_json_and_the_raw_text():
    e = ev.recording_entry({"text": "t", "raw_reply": FENCED})
    assert e["reply"] == READING and e["parse_status"] == "fenced_json" and e["raw_reply"] == FENCED


@pytest.mark.parametrize("raw", ["", "   ", None, "I cannot help with that.", "Sure! {\"verdict\": \"REQUEST\"}", "```json\n{oops\n```", 5])
def test_an_unparseable_reply_never_raises_and_keeps_the_raw_string(raw):
    e = ev.recording_entry({"text": "t", "raw_reply": raw})
    assert e["reply"] is None and e["parse_status"] == "unparseable" and e["raw_reply"] == (raw if isinstance(raw, str) else repr(raw))
    assert json.loads(json.dumps(e))["text"] == "t"                                   # the entry itself is always valid JSON


def test_a_recording_replays_the_raw_text_so_a_replay_reaches_the_same_verdict(tmp_path):
    entries = [ev.recording_entry({"text": "a refund please", "raw_reply": FENCED}),
               ev.recording_entry({"text": "b", "raw_reply": "I cannot help with that."})]
    p = tmp_path / "rec.json"
    p.write_text(json.dumps({"entries": entries}), encoding="utf-8")
    client = cd.load_recordings(str(p))
    assert client(ModelConsentReader.user_message("a refund please"), system="s") == FENCED
    assert client(ModelConsentReader.user_message("b"), system="s") == "I cannot help with that."


def test_the_hand_written_recordings_still_load():
    assert cd.load_recordings(ev.CONSENT_RECORDINGS).by_key                           # entries without raw_reply or parse_status


# ---- the run: saving cannot crash it, and results come first ----------------------------------------------
def test_a_fenced_reply_no_longer_crashes_the_save(monkeypatch, capsys, tmp_path):
    _fake_http(monkeypatch, usage={"input_tokens": 500, "output_tokens": 100}, model="claude-haiku-5-5", text=FENCED)
    out_file = tmp_path / "rec.json"
    rc, out = _run(["repro", "--set", "dhanush_m1", "--consent", "live", "--save-consent", str(out_file)], monkeypatch, capsys)
    assert rc == 0 and "PASTE-BACK SUMMARY" in out
    entries = json.loads(out_file.read_text(encoding="utf-8"))["entries"]
    assert entries and all(e["parse_status"] == "fenced_json" and e["raw_reply"] == FENCED and e["reply"] == READING for e in entries)


def test_a_failed_save_loses_no_results_and_keeps_the_old_file(monkeypatch, capsys, tmp_path):
    _fake_http(monkeypatch, usage={"input_tokens": 500, "output_tokens": 100}, model="claude-haiku-5-5", text=FENCED)
    old = tmp_path / "rec.json"
    old.write_text('{"entries": ["keep me"]}', encoding="utf-8")

    real_dump = ev.json.dump

    def boom(obj, f, *a, **k):
        if not str(f.name).endswith("rec.json.tmp"):                                   # only the recordings file fails
            return real_dump(obj, f, *a, **k)
        f.write('{"entries": [')                                                       # a half-written file
        raise OSError("disk full")
    monkeypatch.setattr(ev.json, "dump", boom)
    rc, out = _run(["repro", "--set", "dhanush_m1", "--consent", "live", "--save-consent", str(old)], monkeypatch, capsys)
    assert "PASTE-BACK SUMMARY" in out and "dhanush_m1" in out                         # the results were printed
    assert "COULD NOT SAVE" in out and "disk full" in out and rc != 0                  # and the failure is said, not swallowed
    assert old.read_text(encoding="utf-8") == '{"entries": ["keep me"]}'               # the old file is intact


def test_results_are_printed_before_the_final_save(monkeypatch, capsys, tmp_path):
    _fake_http(monkeypatch, usage={"input_tokens": 500, "output_tokens": 100}, model="claude-haiku-5-5", text=FENCED)
    seen = {}
    real = ev.write_recordings

    def spy(path, entries, note, extra=None):
        seen["printed_already"] = "PASTE-BACK SUMMARY" in capsys.readouterr().out
        return real(path, entries, note, extra)
    monkeypatch.setattr(ev, "write_recordings", spy)
    _run(["repro", "--set", "dhanush_m1", "--consent", "live", "--save-consent", str(tmp_path / "r.json")], monkeypatch, capsys)
    assert seen.get("printed_already")


# ---- how an unparseable reply is scored --------------------------------------------------------------------
@pytest.mark.parametrize("reply", [" ", "I'm sorry, I can't help with that request.", "{not json", "Here: " + json.dumps(READING)])
def test_an_unparseable_reply_is_no_reading_and_nothing_is_released_automatically(monkeypatch, capsys, reply):
    _fake_http(monkeypatch, usage={"input_tokens": 500, "output_tokens": 100}, model="claude-haiku-5-5", text=reply)
    rc, out = _run(["repro", "--set", "dhanush_m1", "--consent", "live", "--json"], monkeypatch, capsys)
    r = json.loads(out[out.index("["):out.rindex("]") + 1])[0]
    cr = r["consent_reader"]
    assert cr["accepted"] == 0 and cr["rejected"] == cr["readings_asked"] > 0
    assert all(x["verdict"].startswith("REJECTED:") for x in cr["exchanges"])
    assert r["legitimate"]["completed_automatically"] == 0                              # held, not released
    assert r["attacks"]["released_automatically"] == 0 and r["unauthorized_releases"]["count"] == 0
    assert r["numeric_limit_breaches"]["count"] == 0


def test_a_rejected_reply_is_not_saved_as_a_replayable_recording(monkeypatch, capsys, tmp_path):
    _fake_http(monkeypatch, usage={"input_tokens": 500, "output_tokens": 100}, model="claude-haiku-5-5", text="I cannot help.")
    out_file = tmp_path / "rec.json"
    _run(["repro", "--set", "dhanush_m1", "--consent", "live", "--save-consent", str(out_file)], monkeypatch, capsys)
    assert json.loads(out_file.read_text(encoding="utf-8"))["entries"] == []


# ---- resuming with the same ledger -------------------------------------------------------------------------
def test_a_rerun_with_the_same_ledger_reruns_the_whole_set_under_the_remaining_cap(monkeypatch, capsys, tmp_path):
    _fake_http(monkeypatch, usage={"input_tokens": 500, "output_tokens": 100}, model="claude-haiku-5-5", text=FENCED)
    led = tmp_path / "l.json"
    args = ["repro", "--set", "dhanush_m1", "--consent", "live", "--max-spend", "1.00", "--ledger", str(led), "--json"]
    rc1, out1 = _run(args, monkeypatch, capsys)
    first = json.loads(led.read_text(encoding="utf-8"))
    rc2, out2 = _run(args, monkeypatch, capsys)
    second = json.loads(led.read_text(encoding="utf-8"))
    assert rc1 == rc2 == 0
    r1, r2 = (json.loads(o[o.index("["):o.rindex("]") + 1])[0] for o in (out1, out2))
    assert r1["cases"] == r2["cases"] == 33 and "partial" not in r2                    # every case ran again, none skipped
    assert second["calls"] == 2 * first["calls"]
    assert D(second["committed_usd"]) == 2 * D(first["committed_usd"])                # spend accumulated, nothing reset
    assert r2["spend"]["spent_usd"] > r1["spend"]["spent_usd"]


def test_the_cap_covers_the_first_run_and_the_rerun_together(monkeypatch, capsys, tmp_path):
    _fake_http(monkeypatch, usage={"input_tokens": 500, "output_tokens": 100}, model="claude-haiku-5-5", text=FENCED)
    led = tmp_path / "l.json"
    base = ["repro", "--set", "dhanush_m1", "--consent", "live", "--ledger", str(led)]
    _run(base + ["--max-spend", "1.00"], monkeypatch, capsys)
    spent = D(json.loads(led.read_text(encoding="utf-8"))["committed_usd"])
    rc, out = _run(base + ["--max-spend", str(spent + D("0.001"))], monkeypatch, capsys)       # room for far less than a whole set
    assert rc == 4 and "PARTIAL" in out
    assert D(json.loads(led.read_text(encoding="utf-8"))["committed_usd"]) <= spent + D("0.001")
