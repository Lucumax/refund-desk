"""Spend guard (2026-11-01). Written first (red). Offline only: the HTTP opener is replaced, no request leaves the machine, no key exists.

Before the owner runs the live consent evaluation with a real key, every model call is reserved against a hard USD cap:
- reservation = input size estimate + max_tokens at the configured model's price, made BEFORE the request is sent;
- a call that would exceed the cap is never sent; the run stops cleanly, the result is labelled PARTIAL, a case that was not run is not counted;
- a ledger file shares the cap across commands (the four sets run in sequence);
- --dry-run prints the number of calls and the worst-case cost and needs no key;
- the same guard sits under `python -m app --consent-reader model` (one cap per session).
"""
import io
import json
import os
import re
from decimal import Decimal

import pytest

import app.model_client as mc
from app import evaluation as ev
from app import spend_guard as sg
from app.spend_guard import SpendCapReached, SpendGuard, UnknownModelPrice
from conftest import ROOT

D = Decimal


# ---- the price table --------------------------------------------------------------------------------------
def test_prices_are_per_million_tokens_and_the_source_is_named():
    assert sg.price_for("claude-opus-5-5", 1000) == (D("4"), D("20"))
    assert sg.price_for("claude-sonnet-5-5", 1000) == (D("2"), D("10"))
    assert sg.price_for("claude-haiku-5-5", 1000) == (D("0.10"), D("0.50"))
    assert sg.price_for("claude-haiku-5-5", 150_000) == (D("0.50"), D("2.50"))      # the long-prompt tier
    assert "platform.claude.com" in sg.PRICE_SOURCE and "2026-11-01" in sg.PRICE_SOURCE


def test_an_unknown_model_has_no_price_and_the_guard_refuses_to_start():
    with pytest.raises(UnknownModelPrice):
        sg.price_for("some-model", 10)
    with pytest.raises(UnknownModelPrice):
        SpendGuard("some-model", D("1"))


def test_the_input_estimate_is_conservative():
    text = "refund please " * 100
    assert sg.estimate_input_tokens("SYSTEM " * 50, text) >= len((("SYSTEM " * 50) + text).encode()) // 4


# ---- reservation and settlement ---------------------------------------------------------------------------
def test_a_call_over_the_cap_is_refused_before_anything_is_sent():
    g = SpendGuard("claude-opus-5-5", D("0.05"))             # one 4,000 token reply at $20/MTok is already $0.08
    with pytest.raises(SpendCapReached):
        g.reserve("s", "u", 4000)
    assert g.spent == 0 and g.halted.startswith("CAP")


def test_a_call_that_fits_is_reserved_then_settled_at_the_real_usage():
    g = SpendGuard("claude-opus-5-5", D("1.00"))
    r = g.reserve("s", "u", 4000)
    assert g.spent == r.cost and r.cost >= D("0.08")
    g.settle(r, {"input_tokens": 1000, "output_tokens": 500})
    assert g.spent == (1000 * D("4") + 500 * D("20")) / 1_000_000 == D("0.014")
    assert g.calls == 1


def test_a_missing_usage_is_charged_at_the_full_reservation():
    g = SpendGuard("claude-opus-5-5", D("1.00"))
    r = g.reserve("s", "u", 4000)
    g.settle(r, None)
    assert g.spent == r.cost


def test_the_boundary_is_inclusive_and_the_next_call_is_refused():
    g = SpendGuard("claude-haiku-5-5", D("0.003"))
    r = g.reserve("s", "u", 4000)                            # 4,000 x $0.50/MTok = $0.002 plus a few input tokens
    g.settle(r, {"input_tokens": 10, "output_tokens": 4000})
    with pytest.raises(SpendCapReached):
        g.reserve("s", "u", 4000)                            # $0.002 + $0.002 > $0.003


def test_an_unbilled_failure_is_refunded_a_possibly_billed_one_is_charged():
    g = SpendGuard("claude-opus-5-5", D("1.00"))
    r1 = g.reserve("s", "u", 4000)
    g.fail(r1, billed=False)
    assert g.spent == 0
    r2 = g.reserve("s", "u", 4000)
    g.fail(r2, billed=True)
    assert g.spent == r2.cost


def test_three_failed_calls_in_a_row_halt_the_run():
    g = SpendGuard("claude-haiku-5-5", D("1.00"))
    for _ in range(3):
        g.fail(g.reserve("s", "u", 100), billed=False)
    assert g.halted and "3" in g.halted
    with pytest.raises(SpendCapReached):
        g.reserve("s", "u", 100)


def test_a_good_call_resets_the_failure_streak():
    g = SpendGuard("claude-haiku-5-5", D("1.00"))
    g.fail(g.reserve("s", "u", 100), billed=False)
    g.fail(g.reserve("s", "u", 100), billed=False)
    g.settle(g.reserve("s", "u", 100), {"input_tokens": 1, "output_tokens": 1})
    g.fail(g.reserve("s", "u", 100), billed=False)
    assert not g.halted


def test_can_afford_asks_for_a_whole_batch_of_calls():
    g = SpendGuard("claude-opus-5-5", D("0.50"))
    assert g.can_afford(D("0.50")) and not g.can_afford(D("0.51"))


# ---- the shared ledger -------------------------------------------------------------------------------------
def test_the_ledger_carries_the_spend_to_the_next_command(tmp_path):
    p = str(tmp_path / "ledger.json")
    a = SpendGuard("claude-opus-5-5", D("1.00"), ledger=p)
    a.set_label("dhanush_m1")
    a.settle(a.reserve("s", "u", 4000), {"input_tokens": 1000, "output_tokens": 1000})       # $0.024
    b = SpendGuard("claude-opus-5-5", D("1.00"), ledger=p)
    b.set_label("codex_redteam")
    assert b.spent == D("0.024")
    b.settle(b.reserve("s", "u", 4000), {"input_tokens": 1000, "output_tokens": 1000})
    data = json.load(open(p, encoding="utf-8"))
    assert D(data["committed_usd"]) == D("0.048") and data["calls"] == 2
    assert set(data["by_set"]) == {"dhanush_m1", "codex_redteam"}
    assert "key" not in json.dumps(data).lower()


def test_a_call_in_flight_when_the_process_died_counts_as_spent(tmp_path):
    p = str(tmp_path / "ledger.json")
    a = SpendGuard("claude-opus-5-5", D("1.00"), ledger=p)
    r = a.reserve("s", "u", 4000)                           # never settled: the process "dies" here
    b = SpendGuard("claude-opus-5-5", D("1.00"), ledger=p)
    assert b.spent == r.cost


def test_a_corrupt_ledger_stops_the_run_instead_of_resetting_the_spend(tmp_path):
    p = tmp_path / "ledger.json"
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="LEDGER"):
        SpendGuard("claude-opus-5-5", D("1.00"), ledger=str(p))


# ---- the model client --------------------------------------------------------------------------------------
def _fake_http(monkeypatch, usage=None, status=None, model="claude-opus-5-5", text="{}"):
    sent = []

    class Resp:
        def __init__(self, b): self.b = b
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self, n=-1): return self.b

    class Opener:
        def open(self, req, timeout=None):
            sent.append(json.loads(req.data))
            if status:
                import urllib.error
                raise urllib.error.HTTPError(req.full_url, status, "err", {}, io.BytesIO(b"{}"))
            body = {"content": [{"type": "text", "text": text}], "stop_reason": "end_turn"}
            if usage is not None:
                body["usage"] = usage
            return Resp(json.dumps(body).encode())
    monkeypatch.setattr(mc, "no_redirect_opener", lambda *a: Opener())
    monkeypatch.setenv("APP_MODEL_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    monkeypatch.setenv("APP_MODEL_ID", model)
    return sent


def test_the_completer_reserves_before_sending_and_records_the_usage(monkeypatch):
    sent = _fake_http(monkeypatch, usage={"input_tokens": 800, "output_tokens": 300})
    g = SpendGuard("claude-opus-5-5", D("1.00"))
    assert mc.make_completer(guard=g)("hello", system="rules") == "{}"
    assert len(sent) == 1 and g.calls == 1
    assert g.spent == (800 * D("4") + 300 * D("20")) / 1_000_000


def test_the_completer_sends_nothing_once_the_cap_is_reached(monkeypatch):
    sent = _fake_http(monkeypatch, usage={"input_tokens": 1, "output_tokens": 1})
    g = SpendGuard("claude-opus-5-5", D("0.01"))
    with pytest.raises(SpendCapReached):
        mc.make_completer(guard=g)("hello", system="rules")
    assert sent == []


def test_a_rejected_key_halts_at_once_and_is_not_charged(monkeypatch):
    sent = _fake_http(monkeypatch, status=401)
    g = SpendGuard("claude-opus-5-5", D("1.00"))
    c = mc.make_completer(guard=g)
    with pytest.raises(Exception):
        c("hello", system="rules")
    assert g.spent == 0 and g.halted and "401" in g.halted
    with pytest.raises(SpendCapReached):
        c("hello", system="rules")
    assert len(sent) == 1


def test_a_server_error_is_charged_because_it_may_have_been_billed(monkeypatch):
    _fake_http(monkeypatch, status=529)
    g = SpendGuard("claude-opus-5-5", D("1.00"))
    with pytest.raises(Exception):
        mc.make_completer(guard=g)("hello", system="rules")
    assert g.spent > 0


def test_the_guard_must_match_the_configured_model(monkeypatch):
    _fake_http(monkeypatch)
    with pytest.raises(RuntimeError, match="MODEL"):
        mc.make_completer(guard=SpendGuard("claude-haiku-5-5", D("1.00")))


def test_the_openai_provider_cannot_run_under_this_guard(monkeypatch):
    _fake_http(monkeypatch)
    monkeypatch.setenv("APP_MODEL_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    with pytest.raises(RuntimeError, match="anthropic"):
        mc.make_completer(guard=SpendGuard("claude-opus-5-5", D("1.00")))


def test_without_a_guard_nothing_changes(monkeypatch):
    sent = _fake_http(monkeypatch)
    assert mc.make_completer()("hello", system="rules") == "{}" and len(sent) == 1


# ---- the evaluation ----------------------------------------------------------------------------------------
def _run(argv, monkeypatch, capsys):
    rc = ev.main(argv)
    return rc, capsys.readouterr().out


def test_dry_run_counts_the_calls_and_the_worst_case_and_needs_no_key(monkeypatch, capsys):
    for k in ("ANTHROPIC_API_KEY", "MODEL_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("APP_MODEL_ID", "claude-opus-5-5")
    monkeypatch.setattr(mc, "make_completer", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no completer in a dry run")))
    rc, out = _run(["repro", "--set", "dhanush_m1", "--consent", "live", "--dry-run"], monkeypatch, capsys)
    assert rc == 0
    m = re.search(r"dhanush_m1\s*:\s*(\d+) cases, (\d+) steps, (\d+) model calls", out)
    assert m, out
    cases, steps, calls = map(int, m.groups())
    assert (cases, steps) == (33, 35) and steps <= calls <= 2 * steps
    assert "worst case" in out.lower() and "claude-opus-5-5" in out and "DRY RUN" in out
    assert re.search(r"worst case[^$]*\$\d+\.\d{2}", out, re.I)


def test_dry_run_over_the_four_sets_adds_them_up(monkeypatch, capsys):
    monkeypatch.setenv("APP_MODEL_ID", "claude-haiku-5-5")
    rc, out = _run(["repro", "--set", "dhanush_m1,codex_redteam,codex_pass2,dev", "--consent", "live", "--dry-run",
                    "--max-spend", "1.00"], monkeypatch, capsys)
    assert rc == 0
    for name in ("dhanush_m1", "codex_redteam", "codex_pass2", "dev"):
        assert re.search(name + r"\s*:\s*\d+ cases", out), name
    assert re.search(r"total\s*:\s*\d+ model calls", out, re.I) and "cap $1.00" in out


def test_dry_run_with_an_unpriced_model_stops(monkeypatch, capsys):
    monkeypatch.setenv("APP_MODEL_ID", "mystery-model")
    rc, out = _run(["repro", "--set", "dhanush_m1", "--consent", "live", "--dry-run"], monkeypatch, capsys)
    assert rc == 2 and "no price" in out.lower()


def test_the_default_cap_is_one_dollar(monkeypatch, capsys):
    monkeypatch.setenv("APP_MODEL_ID", "claude-haiku-5-5")
    _, out = _run(["repro", "--set", "dhanush_m1", "--consent", "live", "--dry-run"], monkeypatch, capsys)
    assert "cap $1.00" in out


def test_the_live_run_stops_cleanly_at_the_cap_and_is_labelled_partial(monkeypatch, capsys):
    _fake_http(monkeypatch, usage={"input_tokens": 500, "output_tokens": 3000}, model="claude-opus-5-5")   # $0.062 a call
    rc, out = _run(["repro", "--set", "dhanush_m1", "--consent", "live", "--max-spend", "0.50", "--json"], monkeypatch, capsys)
    assert rc == 4
    r = json.loads(out[out.index("["):out.rindex("]") + 1])[0]
    assert r["partial"]["status"] == "PARTIAL" and "cap" in r["partial"]["reason"].lower()
    assert 0 < r["partial"]["cases_run"] < r["partial"]["cases_total"] == 33
    assert r["messages"] < 35 and r["cases"] == r["partial"]["cases_run"]                  # an unrun case is never counted
    assert r["spend"]["spent_usd"] <= 0.50 and r["spend"]["model"] == "claude-opus-5-5"
    assert "PARTIAL" in r["run_kind"]
    assert r["consent_reader"]["readings_asked"] == len(r["consent_reader"]["exchanges"]) <= r["messages"] * 2


def test_the_text_report_says_partial_and_how_much_was_run(monkeypatch, capsys):
    _fake_http(monkeypatch, usage={"input_tokens": 500, "output_tokens": 3000}, model="claude-opus-5-5")
    rc, out = _run(["repro", "--set", "dhanush_m1", "--consent", "live", "--max-spend", "0.50"], monkeypatch, capsys)
    assert rc == 4 and "PARTIAL" in out and re.search(r"\d+ of 33 cases", out) and "spent $" in out


def test_a_set_that_is_never_started_is_reported_not_run(monkeypatch, capsys, tmp_path):
    _fake_http(monkeypatch, usage={"input_tokens": 500, "output_tokens": 3000}, model="claude-opus-5-5")
    rc, out = _run(["repro", "--set", "dhanush_m1,codex_redteam", "--consent", "live", "--max-spend", "0.30",
                    "--ledger", str(tmp_path / "l.json")], monkeypatch, capsys)
    assert rc == 4 and "NOT RUN" in out and "codex_redteam" in out


def test_the_ledger_makes_the_cap_cover_commands_run_in_sequence(monkeypatch, capsys, tmp_path):
    sent = _fake_http(monkeypatch, usage={"input_tokens": 500, "output_tokens": 3000}, model="claude-opus-5-5")
    led = str(tmp_path / "l.json")
    rc1, _ = _run(["repro", "--set", "dhanush_m1", "--consent", "live", "--max-spend", "0.50", "--ledger", led], monkeypatch, capsys)
    c1 = D(json.load(open(led, encoding="utf-8"))["committed_usd"])
    assert 0 < c1 <= D("0.50")
    rc2, out2 = _run(["repro", "--set", "codex_redteam", "--consent", "live", "--max-spend", "0.50", "--ledger", led], monkeypatch, capsys)
    c2 = D(json.load(open(led, encoding="utf-8"))["committed_usd"])
    assert rc1 == 4 and rc2 == 4 and c1 <= c2 <= D("0.50")                                  # the second command started from the first one's spend


def test_a_full_run_under_the_cap_is_not_partial(monkeypatch, capsys):
    _fake_http(monkeypatch, usage={"input_tokens": 500, "output_tokens": 300}, model="claude-haiku-5-5")
    rc, out = _run(["repro", "--set", "codex_redteam", "--consent", "live", "--max-spend", "1.00", "--json"], monkeypatch, capsys)
    r = json.loads(out[out.index("["):out.rindex("]") + 1])[0]
    assert rc == 0 and "partial" not in r and r["messages"] == 34 and r["spend"]["spent_usd"] > 0


def test_a_rejected_key_gives_a_partial_with_no_case_counted_from_rules_alone(monkeypatch, capsys):
    sent = _fake_http(monkeypatch, status=401, model="claude-haiku-5-5")
    rc, out = _run(["repro", "--set", "dhanush_m1", "--consent", "live", "--json"], monkeypatch, capsys)
    r = json.loads(out[out.index("["):out.rindex("]") + 1])[0]
    assert rc == 4 and "401" in r["partial"]["reason"] and r["partial"]["cases_run"] == 0 and r["messages"] == 0
    assert len(sent) == 1


def test_save_consent_collects_the_replies_of_every_set_in_one_file(monkeypatch, capsys, tmp_path):
    good = json.dumps({"verdict": "NOT_A_REQUEST", "flags": [], "request_quote": None, "reason": "no"})
    _fake_http(monkeypatch, usage={"input_tokens": 500, "output_tokens": 100}, model="claude-haiku-5-5", text=good)
    out_file = str(tmp_path / "rec.json")
    rc, _ = _run(["repro", "--set", "dhanush_m1,codex_redteam", "--consent", "live", "--save-consent", out_file], monkeypatch, capsys)
    entries = json.load(open(out_file, encoding="utf-8"))["entries"]
    texts = {e["text"] for e in entries}
    d = ev.load_corpus(ev.SETS["dhanush_m1"])["cases"]
    c = ev.load_corpus(ev.SETS["codex_redteam"])["cases"]
    assert any(s["text"] in texts for x in d for s in x["steps"]) and any(s["text"] in texts for x in c for s in x["steps"])


def test_unknown_set_names_are_refused(monkeypatch, capsys):
    monkeypatch.setenv("APP_MODEL_ID", "claude-haiku-5-5")
    rc, out = _run(["repro", "--set", "dev,nonsense", "--consent", "live", "--dry-run"], monkeypatch, capsys)
    assert rc == 2 and "nonsense" in out


def test_the_fixture_run_is_untouched_by_the_guard(monkeypatch, capsys):
    rc, out = _run(["repro", "--set", "dhanush_m1"], monkeypatch, capsys)
    assert rc == 0 and "PARTIAL" not in out


# ---- python -m app --consent-reader model ------------------------------------------------------------------
def test_the_app_has_a_per_session_cap_flag_defaulting_to_one_dollar():
    from app.__main__ import build_parser
    assert build_parser().parse_args([]).max_spend == D("1.00")
    assert build_parser().parse_args(["--max-spend", "0.25"]).max_spend == D("0.25")


def test_the_consent_reader_runs_under_the_session_guard_and_goes_quiet_at_the_cap(monkeypatch, tmp_path, capsys):
    from app.__main__ import build, build_parser
    _fake_http(monkeypatch, usage={"input_tokens": 500, "output_tokens": 3000}, model="claude-opus-5-5")
    args = build_parser().parse_args(["--data-dir", str(tmp_path / "d"), "--consent-reader", "model", "--max-spend", "0.10"])
    app, _ = build(args)
    reader = app.svc.agent.consent_reader
    reader.read("Please refund my order, it arrived broken.")               # one call: $0.062 settled
    assert reader.read("Please refund my order, it arrived broken.") is None
    assert reader.last["verdict"].startswith("REJECTED:SpendCapReached") or "SpendCapReached" in reader.last["verdict"]
    assert "SPEND CAP" in capsys.readouterr().err


def test_the_app_refuses_to_start_a_model_session_with_an_unpriced_model(monkeypatch, tmp_path):
    from app.__main__ import build, build_parser
    _fake_http(monkeypatch, model="mystery-model")
    args = build_parser().parse_args(["--data-dir", str(tmp_path / "d"), "--consent-reader", "model"])
    with pytest.raises(RuntimeError, match="price"):
        build(args)


def test_a_session_without_a_model_makes_no_guard(monkeypatch, tmp_path):
    from app.__main__ import build, build_parser
    args = build_parser().parse_args(["--data-dir", str(tmp_path / "d")])
    app, _ = build(args)
    assert app.svc.agent.consent_reader is None


# ---- the one-command script --------------------------------------------------------------------------------
PS1 = os.path.join(ROOT, "scripts", "live_model_eval.ps1")


def test_the_walter_script_exists_and_never_prints_or_stores_the_key():
    src = open(PS1, encoding="utf-8").read()
    assert "Read-Host" in src and "-AsSecureString" in src
    assert "ANTHROPIC_API_KEY" in src
    for bad in ("sk-ant", "Write-Host $env:ANTHROPIC_API_KEY", "Out-File", "Set-Content", "Add-Content", "setx"):
        assert bad not in src, bad
    assert not re.search(r"(Write-Host|Write-Output|echo)[^\n]*\$(env:ANTHROPIC_API_KEY|plain|key)\b", src, re.I)


def test_the_walter_script_runs_dry_run_then_asks_then_the_four_sets_under_one_cap():
    src = open(PS1, encoding="utf-8").read()
    assert src.index("--dry-run") < src.index("Continue?") < src.index("# LIVE RUN")
    for name in ("dhanush_m1", "codex_redteam", "codex_pass2", "dev"):
        assert name in src
    assert "'--max-spend'" in src and "'--ledger'" in src and "'--save-consent'" in src and "'--consent', 'live'" in src
    assert "1.00" in src and "claude-haiku-5-5" in src and "git push" not in src
