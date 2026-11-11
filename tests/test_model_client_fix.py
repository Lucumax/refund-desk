"""Sprint 3j, the model client. Written first (red). Offline only: the HTTP
opener is replaced and no request leaves the machine.

- Current Claude models reject `temperature` with HTTP 400, so it is not sent (nor top_p or top_k).
- max_tokens is 2,000 to 4,000: thinking counts toward it, and 400 cut replies off before their JSON.
- A reply that stopped at max_tokens, or was declined (stop_reason "refusal"), is a rejection: the rule-based extractor answers.
- When that happens the screen says so: "rule-based fallback (model reply rejected)", never "model"."""
import json
import os

import pytest

import app.model_client as mc
from app.arena import Arena
from app.arena_reader import ModelReader
from app.extractor import ModelExtractor, FALLBACK_LABEL
from app.scripted import model_reply
from conftest import ROOT, build_env

GOOD = model_reply(None, None, "refund_damaged", "cracked handle", 18, "refund $18.00", "refund", "Cracked handle; $18.00.")
TEXT = "The mug set arrived damaged, a cracked handle. Please refund $18.00."


def _opener(monkeypatch, reply: dict, sent: dict):
    class FakeResp:
        def __init__(self, body): self.body = body
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self, n=-1): return self.body

    class FakeOpener:
        def open(self, req, timeout=None):
            sent["body"] = json.loads(req.data)
            return FakeResp(json.dumps(reply).encode())
    monkeypatch.setattr(mc, "no_redirect_opener", lambda *a: FakeOpener())
    monkeypatch.setenv("APP_MODEL_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    monkeypatch.setenv("APP_MODEL_ID", "claude-opus-5-5")


def _anthropic(text, stop="end_turn"):
    return {"content": [{"type": "thinking", "thinking": ""}, {"type": "text", "text": text}], "stop_reason": stop}


def test_no_sampling_parameters_and_room_for_the_reply(monkeypatch):
    sent = {}
    _opener(monkeypatch, _anthropic(GOOD), sent)
    assert mc.make_completer()("BUYER WORDS", system="FIXED") == GOOD
    body = sent["body"]
    assert not {"temperature", "top_p", "top_k"} & set(body)
    assert 2000 <= body["max_tokens"] <= 4000


@pytest.mark.parametrize("stop", ["max_tokens", "refusal"])
def test_a_cut_off_or_declined_reply_is_rejected(monkeypatch, stop):
    _opener(monkeypatch, _anthropic(GOOD, stop), {})
    with pytest.raises(mc.ModelReplyRejected, match=stop):
        mc.make_completer()("BUYER WORDS", system="FIXED")
    ex = ModelExtractor(mc.make_completer())
    f = ex.extract(TEXT)
    assert f.source == "rules" and "MODEL_OUTPUT_REJECTED" in f.notes and stop in ex.last["verdict"]


def test_the_desk_labels_a_rejected_model_reply_as_the_rule_based_fallback(monkeypatch, tmp_path):
    _opener(monkeypatch, _anthropic(GOOD, "max_tokens"), {})
    env = build_env(tmp_path / "d", extractor=ModelExtractor(mc.make_completer()))
    env.svc.reader_name = "model (anthropic, claude-opus-5-5)"
    env.msg(TEXT, "ORD-1001")
    req = env.latest_req()
    reader = env.item(req["id"])["reader"]
    assert reader["label"] == FALLBACK_LABEL == "rule-based fallback (model reply rejected)"
    assert "MODEL_OUTPUT_REJECTED" in reader["notes"] and "model (" not in reader["label"]


def test_the_desk_keeps_the_model_label_when_the_reply_is_accepted(monkeypatch, tmp_path):
    _opener(monkeypatch, _anthropic(GOOD), {})
    env = build_env(tmp_path / "d", extractor=ModelExtractor(mc.make_completer()))
    env.svc.reader_name = "model (anthropic, claude-opus-5-5)"
    env.msg(TEXT, "ORD-1001")
    reader = env.item(env.latest_req()["id"])["reader"]
    assert reader["label"] == "model (anthropic, claude-opus-5-5)" and reader["source"] == "model"


def test_the_arena_records_the_fallback_label_too(monkeypatch, tmp_path):
    _opener(monkeypatch, _anthropic(GOOD, "refusal"), {})
    env = build_env(tmp_path / "d")
    ar = Arena(env.svc, ModelReader(ModelExtractor(mc.make_completer()), "model reader (claude-opus-5-5)"))
    ar.attempt(TEXT, "ORD-1001")
    assert ar.authority.decisions()[-1]["reader"]["name"] == FALLBACK_LABEL


def test_the_desk_and_phone_card_show_the_fallback_label_as_a_flag():
    for f in ("static/desk.js", "static/approve.js"):
        src = open(os.path.join(ROOT, f), encoding="utf-8").read()
        assert 'includes("MODEL_OUTPUT_REJECTED") ? "flag" : "note"' in src, f
