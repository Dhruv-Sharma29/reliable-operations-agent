"""M4 durability: idempotent effects, crash/resume, ambiguous completion, against a real sandbox
(test ports, own run directory) and a scripted fake model. The PLAN.md M4 exit criterion is
"retries after interruption do not repeat the effect"; each test checks the effect count directly
in the target system, not the agent's own records."""

from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid

import httpx
import pytest

from agent import executor as executor_mod
from agent import store
from agent import workflow
from agent.executor import Executor
from agent.tools import act
from incidents.catalog import INCIDENTS, inject
from sandbox import paths, state
from sandbox.runtime import Runtime
from tests.crash_helpers import bad_config_model, no_model, scripted_model

pytestmark = pytest.mark.skipif(not (paths.PG_BIN / "postgres").exists(), reason="run scripts/setup_postgres.sh")
ROOT = paths.ROOT
BAD = {"workers": 1, "bad_port": 17499, "author": "jdoe", "note": "test"}


@pytest.fixture(scope="module")
def rt():
    runtime = Runtime()
    yield runtime
    runtime.down()
    runtime.postgres_stop()
    runtime.agent_db_stop()
    store._ready = False      # the next module's first connection starts it again


@pytest.fixture
def bad_config(rt):
    rt.reset(workers=1)
    time.sleep(1)
    inject(rt, INCIDENTS["bad_config"], BAD)
    assert state.current_config()["api"]["db_port"] == 17499
    return rt


def rollbacks(action_id=None) -> list[dict]:
    return [r for r in state.config_history() if r["author"] == "ops-agent"
            and (action_id is None or r.get("action_id") == action_id)]


def good_config() -> bool:
    return state.current_config()["api"]["db_port"] == paths.PG_PORT


def audit(action_id) -> list[str]:
    with store.connect() as conn:
        return [r[0] for r in conn.execute("SELECT event FROM audit_events WHERE action_id = %s ORDER BY id", (action_id,))]


# --- effect boundary ----------------------------------------------------------------------------

def test_repeating_a_rollback_without_an_action_id_reintroduces_the_fault(bad_config):
    """Why M4 exists: a blind retry of a 'successful' rollback rolls back to the bad config."""
    act.rollback_config()
    assert good_config()
    act.rollback_config()
    assert not good_config()


def test_repeating_a_rollback_with_its_action_id_is_a_no_op(bad_config):
    first = act.rollback_config(action_id="a1")
    pid = Runtime().started_by("api")
    second = act.rollback_config(action_id="a1")
    assert good_config() and len(rollbacks("a1")) == 1 and second["repeat"] and "repeat" not in first
    assert Runtime().started_by("api") == pid == "a1"          # api not restarted a second time


def test_rollback_release_and_rotation_are_idempotent(rt):
    rt.reset(workers=1)
    time.sleep(1)
    inject(rt, INCIDENTS["bad_deploy"], {"workers": 1, "note": "test"})
    act.rollback_release(action_id="r1")
    act.rollback_release(action_id="r1")
    assert [d["release"] for d in state.deploy_history()] == ["2.3.0", "2.3.1", "2.3.0"]

    issued = lambda: httpx.get(f"{paths.PAYMENTS_URL}/admin/issued", timeout=5).json()["issued"]  # noqa: E731
    before = issued()
    act.rotate_credential("payments", action_id="k1")
    token = state.read_secret("payments_token")
    act.rotate_credential("payments", action_id="k1")
    assert issued() == before + 1 and state.read_secret("payments_token") == token

    act.escalate("help", action_id="e1")
    act.escalate("help", action_id="e1")
    assert len(state.escalations()) == 1


# --- executor: ambiguous completion and crash between effect and record -------------------------

def test_timeout_after_the_effect_is_reconciled_without_a_second_effect(bad_config):
    runs = []

    def rollback_then_time_out(action_id=None):
        runs.append(action_id)
        act.rollback_config(action_id=action_id)       # the effect happens...
        raise TimeoutError("no response from config service")   # ...but the caller can't know
    ex = Executor("ops-agent", actions={**act.ACTIONS, "rollback_config": rollback_then_time_out})
    aid = "t-" + uuid.uuid4().hex     # action ids are global: the agent DB outlives test runs
    out = ex.request("inc-t", "rollback_config", {}, action_id=aid)
    assert out.state == "pending_approval"     # rollback_config is approval-required
    out = ex.approve(out.action_id, "oncall-human")
    assert out.state == "succeeded" and out.detail.get("reconciled")
    assert len(runs) == 1 and len(rollbacks(aid)) == 1 and good_config()
    assert audit(aid)[-3:] == ["unknown", "reconciling", "succeeded"]


def test_crash_after_effect_then_resume_does_not_repeat_it(bad_config):
    incident = workflow.open_incident([{"alert": "Api5xxRateHigh"}])
    # approval is required for rollback_config; pre-approve by running the workflow with an approver
    # in the child, which crashes right after the effect.
    _child(incident, "after_effect", approve=True)
    action_id = workflow.action_id_for(incident)
    assert len(rollbacks(action_id)) == 1 and audit(action_id)[-1] == "executing"   # effect, no record
    inc = workflow.run(incident, Executor("ops-agent"), client=no_model(), approver=lambda o: "oncall-human")
    assert inc["state"] == "RESOLVED", inc
    assert len(rollbacks(action_id)) == 1 and good_config()                         # not repeated
    assert "reconciling" in audit(action_id) and audit(action_id)[-1] == "succeeded"


def test_resume_all_finishes_an_incident_interrupted_by_a_crash(bad_config):
    """What agent/supervisor.py does at start-up."""
    incident = workflow.open_incident([{"alert": "Api5xxRateHigh"}])
    _child(incident, "PROPOSED", approve=True)
    results = workflow.resume_all(Executor("ops-agent"), client=no_model(), approver=lambda o: "oncall-human")
    mine = next(r for r in results if r.get("incident_id") == incident)
    assert mine["state"] == "RESOLVED" and len(rollbacks(workflow.action_id_for(incident))) == 1 and good_config()


def test_crash_after_proposal_resumes_without_asking_the_model_again(bad_config):
    incident = workflow.open_incident([{"alert": "Api5xxRateHigh"}])
    _child(incident, "PROPOSED", approve=True)
    assert workflow.load(incident)["state"] == "PROPOSED" and rollbacks() == []      # nothing executed yet
    inc = workflow.run(incident, Executor("ops-agent"), client=no_model(), approver=lambda o: "oncall-human")
    assert inc["state"] == "RESOLVED" and len(rollbacks(workflow.action_id_for(incident))) == 1
    assert workflow.history(incident) == ["OPEN", "INVESTIGATING", "DIAGNOSED", "PROPOSED", "EXECUTING",
                                          "VERIFYING", "RESOLVED"]


# --- workflow decisions -------------------------------------------------------------------------

def test_approval_wait_survives_and_resumes_after_a_human_approves(bad_config):
    calls: list = []
    incident = workflow.open_incident([{"alert": "Api5xxRateHigh"}])
    inc = workflow.run(incident, Executor("ops-agent"), client=bad_config_model(calls))
    assert inc["state"] == "AWAITING_APPROVAL" and rollbacks() == []
    Executor("oncall-human").approve(inc["outcome"]["action_id"], "oncall-human")   # out of band
    inc = workflow.run(incident, Executor("ops-agent"), client=no_model())
    assert inc["state"] == "RESOLVED" and len(rollbacks()) == 1 and len(calls) == 3


def test_insufficient_evidence_escalates_without_any_action(bad_config):
    diag = {"incident_type": "unknown", "evidence": ["ev-1"], "confidence": "low", "unknowns": ["cause"]}
    model = scripted_model([[("config_history", {})], [("submit_diagnosis", diag)]])
    incident = workflow.open_incident([{"alert": "Api5xxRateHigh"}])
    inc = workflow.run(incident, Executor("ops-agent"), client=model, approver=lambda o: "oncall-human")
    assert inc["state"] == "ESCALATED" and "insufficient evidence" in inc["reason"] and rollbacks() == []


def _child(incident: str, point: str, approve: bool) -> None:
    env = {**os.environ, "CRASH_APPROVER": "oncall-human" if approve else ""}
    r = subprocess.run([sys.executable, "-m", "tests.crash_helpers", incident, point], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 137, r.stdout + r.stderr
