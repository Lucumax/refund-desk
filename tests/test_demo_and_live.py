import json
import sys
import os

from app.fixture_transport import FixturePayPal
from app.demo import SCENES
from conftest import ROOT, build_env, MSG_A

sys.path.insert(0, os.path.join(ROOT, "scripts"))
import live_sandbox_run  # noqa: E402


def scene(env, name):
    r = env.post("/api/demo/scene", {"scene": name})
    assert r.status == 200, r.body
    return r.json()["request"]


def test_all_demo_scenes_land_where_the_script_says(env):
    assert scene(env, "A")["stage"] == "SENT_DONE"
    assert scene(env, "B")["stage"] == "PENDING_APPROVAL"
    c1 = scene(env, "C_UNKNOWN")
    assert c1["stage"] == "NEEDS_INFO" and c1["kind"] == "NEEDS_INFO"       # link is for another order
    c2 = scene(env, "C_INJECTION")
    assert c2["stage"] == "BLOCKED" and c2["flags"] and c2["gate"]["rule_id"] == "G1"       # refused, no provider call
    assert scene(env, "D_TIMEOUT")["stage"] == "UNKNOWN"
    assert scene(env, "E_PENDING")["stage"] == "SENT_PENDING"
    assert env.fx.refunds_created == 3            # A, D (accepted but unheard) and E; B and C* moved no money


def test_demo_timeout_scene_ends_with_one_refund_after_retry(env):
    d = scene(env, "D_TIMEOUT")
    env.retry(d['request_id'])
    assert env.fx.refunds_created == 1 and env.item(d["request_id"])["stage"] in ("SENT_DONE", "SENT_PENDING")


def test_demo_webhook_settles_pending_and_replay_is_ignored(env):
    scene(env, "E_PENDING")
    assert env.post("/api/demo/webhook").json()["result"] == "APPLIED"
    assert env.post("/api/demo/webhook", {"replay": True}).json()["result"] == "DUPLICATE_EVENT"
    assert env.post("/api/demo/webhook").json()["result"] in ("NOOP", "APPLIED", "DUPLICATE_EVENT")


def test_demo_panel_scene_list_matches_scene_ids(env):
    assert [s["id"] for s in env.get("/api/demo/scenes").json()["scenes"]] == list(SCENES)


def test_demo_routes_do_not_exist_outside_fixture_mode(env):
    env.app.demo = None
    assert env.get("/api/demo/scenes").status == 404


def test_whole_demo_runs_in_a_blink(env):
    import time
    t = time.time()
    for s in SCENES:
        scene(env, s)
    assert time.time() - t < 5


# ---- live script: its guards, tested with no network -----------------------
def good_env():
    return {"PAYPAL_CLIENT_ID": "id-from-env", "PAYPAL_CLIENT_SECRET": "secret-from-env", "PAYPAL_ENV": "sandbox"}


def test_live_script_refuses_without_keys(capsys):
    assert live_sandbox_run.main(["--capture-id", "C1", "--yes"], env={}) == 2
    assert "missing environment variables" in capsys.readouterr().out


def test_live_script_refuses_without_sandbox_flag_or_yes(capsys):
    e = good_env()
    e["PAYPAL_ENV"] = "live"
    assert live_sandbox_run.main(["--capture-id", "C1", "--yes"], env=e) == 2
    assert live_sandbox_run.main(["--capture-id", "C1"], env=good_env()) == 2


def test_live_script_refuses_large_or_bad_amount():
    assert live_sandbox_run.main(["--capture-id", "C1", "--yes", "--amount", "26.00"], env=good_env()) == 2
    assert live_sandbox_run.main(["--capture-id", "C1", "--yes", "--amount", "abc"], env=good_env()) == 2


def test_live_script_logic_runs_end_to_end_on_the_fixture_transport(capsys):
    fx = FixturePayPal({"CAP-LIVE": 5_000})
    code = live_sandbox_run.main(["--capture-id", "CAP-LIVE", "--amount", "1.00", "--yes"], env=good_env(), transport=fx)
    out = capsys.readouterr().out
    assert code == 0 and "decision: ALLOW" in out and "returns the same refund id: True" in out
    assert fx.refunds_created == 1 and fx.refund_posts == 2     # the second POST is the same-key probe


def test_live_script_default_transport_is_host_locked():
    from app.transport import UrllibTransport, SANDBOX_HOST
    assert UrllibTransport().allowed_hosts == (SANDBOX_HOST,)
