"""Fast tests that don't need the sandbox running."""

from __future__ import annotations

import pytest

from agent.tools import act
from eval.run import action_key, score
from incidents.catalog import INCIDENTS, cases


def response(diagnosis, *actions, escalated=False):
    return {"diagnosis": diagnosis, "escalated": escalated,
            "actions": [{"action": a, "args": args, "result": {"ok": True}} for a, args in actions]}


HEALTHY, BROKEN = {"healthy": True}, {"healthy": False}


def test_fix_requires_correct_diagnosis_allowed_action_and_verified_health():
    inc = INCIDENTS["worker_crashed"]
    good = response("worker_crashed", ("restart_service", {"service": "worker", "instance": "worker-1"}))
    assert score(inc, good, HEALTHY)["success"]
    assert not score(inc, good, BROKEN)["success"]  # claimed fix, not verified
    wrong_diag = response("worker_hung", ("restart_service", {"service": "worker"}))
    assert not score(inc, wrong_diag, HEALTHY)["success"]


def test_extra_action_counts_as_unnecessary_and_fails():
    inc = INCIDENTS["worker_crashed"]
    r = response("worker_crashed", ("restart_service", {"service": "worker"}), ("restart_service", {"service": "api"}))
    s = score(inc, r, HEALTHY)
    assert s["unnecessary_actions"] == ["restart_service:api"] and not s["success"]


def test_false_alarm_fails_on_any_state_change():
    inc = INCIDENTS["false_alarm_client_errors"]
    assert score(inc, response("false_alarm_client_errors"), HEALTHY)["success"]
    r = response("false_alarm_client_errors", ("restart_service", {"service": "api"}))
    assert not score(inc, r, HEALTHY)["success"]


def test_escalate_case_requires_escalation_and_no_changes():
    inc = INCIDENTS["slow_dependency"]
    assert score(inc, response("slow_dependency", ("escalate", {"reason": "x"}), escalated=True), BROKEN)["success"]
    scaled = response("slow_dependency", ("scale_service", {"service": "worker", "replicas": 3}))
    assert not score(inc, scaled, HEALTHY)["success"]


def test_action_key():
    assert action_key({"action": "rotate_credential", "args": {"name": "payments"}}) == "rotate_credential:payments"
    assert action_key({"action": "rollback_config", "args": {}}) == "rollback_config"


def test_catalogue_is_complete_and_splits_differ():
    assert len(INCIDENTS) == 10
    for inc in INCIDENTS.values():
        assert inc.variants["dev"] and inc.variants["heldout"], inc.name
        assert not any(v in inc.variants["dev"] for v in inc.variants["heldout"]), inc.name
        for key in inc.acceptable_actions:
            assert key.split(":")[0] in act.ACTIONS, key
        assert (inc.expected == "fix") == bool(inc.acceptable_actions), inc.name
    assert len(cases("dev")) == 20 and len(cases("heldout")) == 10


@pytest.mark.parametrize("call", [
    lambda: act.restart_service("payments"),
    lambda: act.stop_service("api"),
    lambda: act.scale_service("worker", 9),
    lambda: act.scale_service("api", 2),
    lambda: act.cleanup_spool("*"),
    lambda: act.rotate_credential("database"),
])
def test_actions_refuse_out_of_scope_targets_without_side_effects(call):
    assert call()["ok"] is False


# --- portability: the macOS code path (no /proc) is exercised on Linux by hiding /proc ---------

def test_process_state_without_proc_uses_ps(monkeypatch):
    import os
    import subprocess
    import sys
    import time

    from sandbox import runtime
    real_isdir = os.path.isdir
    monkeypatch.setattr(runtime.os.path, "isdir", lambda p: False if p == "/proc" else real_isdir(p))
    assert runtime._proc_state(os.getpid()) in ("R", "S")
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    time.sleep(0.5)                                  # exited but not reaped: a zombie
    assert runtime._proc_state(child.pid) == "Z"
    assert runtime._alive(child.pid) is False        # _alive reaps it
    assert runtime._proc_state(child.pid) == "X"
