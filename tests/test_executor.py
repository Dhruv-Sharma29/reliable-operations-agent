"""Permitted execution against a real PostgreSQL (isolated test instance) with fake actions."""

from __future__ import annotations

import uuid

import psycopg
import pytest

from agent import store
from agent.executor import ApprovalError, Executor
from sandbox import paths
from sandbox.runtime import Runtime

pytestmark = pytest.mark.skipif(not (paths.PG_BIN / "postgres").exists(), reason="run scripts/setup_postgres.sh")


@pytest.fixture(scope="module", autouse=True)
def agent_db():
    """Only the agent's own database: the executor must not need the sandbox's."""
    store.ensure()
    yield
    Runtime().agent_db_stop()
    store._ready = False


@pytest.fixture
def calls():
    return []


@pytest.fixture
def executor(calls):
    def fake(name):
        def run(action_id=None, **kw):
            assert action_id, "the executor must always pass an action id"
            calls.append((name, kw))
            return {"ok": True, "detail": f"{name} done"}
        return run
    actions = {n: fake(n) for n in ("restart_service", "stop_service", "scale_service", "rollback_config",
                                     "rollback_release", "cleanup_spool", "rotate_credential", "escalate")}
    return Executor("ops-agent", actions=actions)


def incident() -> str:
    return "inc-" + uuid.uuid4().hex[:8]


def events(action_id: str) -> list[str]:
    with store.connect() as conn:
        return [r[0] for r in conn.execute("SELECT event FROM audit_events WHERE action_id = %s ORDER BY id", (action_id,))]


def test_allowed_action_runs_once_with_full_audit_trail(executor, calls):
    out = executor.request(incident(), "restart_service", {"service": "worker", "instance": "worker-1"})
    assert out.state == "succeeded" and calls == [("restart_service", {"service": "worker", "instance": "worker-1"})]
    assert events(out.action_id) == ["requested", "authorized", "executing", "succeeded"]


def test_approval_required_action_waits_and_cannot_be_self_approved(executor, calls):
    out = executor.request(incident(), "rotate_credential", {"name": "payments"})
    assert out.state == "pending_approval" and calls == []
    with pytest.raises(ApprovalError):
        executor.approve(out.action_id, "ops-agent")          # requester can't approve itself
    assert calls == []
    done = executor.approve(out.action_id, "oncall-human")
    assert done.state == "succeeded" and calls == [("rotate_credential", {"name": "payments"})]
    assert events(out.action_id) == ["requested", "pending_approval", "approved", "executing", "succeeded"]
    with pytest.raises(ApprovalError):
        executor.approve(out.action_id, "oncall-human")       # can't run twice through approval


def test_denied_action_never_runs(executor, calls):
    out = executor.request(incident(), "rollback_release", {})
    executor.deny(out.action_id, "oncall-human", "deploy is fine, look elsewhere")
    assert calls == [] and events(out.action_id)[-1] == "denied"


@pytest.mark.parametrize("args", [
    {"service": "worker", "approved": True},                  # injected field
    {"service": "worker", "policy": "allow"},
    {"service": "payments"},                                  # external dependency: not restartable
])
def test_injected_or_out_of_scope_arguments_are_rejected_before_policy(executor, calls, args):
    out = executor.request(incident(), "restart_service", args)
    assert (out.state, out.decision) == ("rejected", "invalid") and calls == []


def test_non_action_tools_are_rejected(executor, calls):
    out = executor.request(incident(), "search_logs", {"instance": "api"})
    assert out.state == "rejected" and calls == []


def test_failed_action_is_recorded_as_failed(executor):
    executor.actions["cleanup_spool"] = lambda **kw: (_ for _ in ()).throw(OSError("disk gone"))
    out = executor.request(incident(), "cleanup_spool", {"pattern": "*.tmp"})
    assert out.state == "failed" and "disk gone" in out.detail["detail"]
    assert events(out.action_id)[-1] == "failed"


@pytest.mark.parametrize("sql", ["UPDATE audit_events SET event = 'x'", "DELETE FROM audit_events", "TRUNCATE audit_events",
                                 "DROP TRIGGER audit_no_change ON audit_events",
                                 "ALTER TABLE audit_events DISABLE TRIGGER audit_no_change", "DROP TABLE audit_events"])
def test_audit_log_is_append_only(executor, sql):
    """The agent role can't change the log, and can't remove what stops it: it doesn't own the tables."""
    executor.request(incident(), "escalate", {"reason": "test"})
    with store.connect() as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute(sql)


def test_append_only_trigger_still_guards_the_owner_side():
    """Defence in depth: even a role allowed to UPDATE is stopped by the trigger."""
    with psycopg.connect(paths.dsn("postgres", "test", port=paths.AGENT_PG_PORT), dbname=store.DB, autocommit=True) as conn, \
            pytest.raises(psycopg.errors.RaiseException):
        conn.execute("UPDATE audit_events SET event = 'x'")


# --- acting agent: investigate, then one remediation through the executor (fake model) ---------

def scripted_client(steps):
    import json as _json

    import httpx
    n = {"i": 0}

    def handler(request):
        step = steps[min(n["i"], len(steps) - 1)]
        n["i"] += 1
        msg = {"role": "assistant", "content": "", "tool_calls": [
            {"id": f"c{n['i']}", "type": "function", "function": {"name": name, "arguments": _json.dumps(args)}}
            for name, args in step]}
        return httpx.Response(200, json={"choices": [{"message": msg}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}})
    return httpx.Client(transport=httpx.MockTransport(handler), base_url="http://fake")


def run_acting(executor, monkeypatch, remediation, approver=None):
    from agent import acting_agent
    from agent.evidence import EvidenceLedger
    monkeypatch.setattr(acting_agent, "EvidenceLedger", lambda: EvidenceLedger({"queue_stats": lambda: {"pending": 1}}))
    diag = {"incident_type": "expired_credential", "evidence": ["ev-1"], "confidence": "high", "unknowns": []}
    client = scripted_client([[("queue_stats", {})], [("submit_diagnosis", diag)], [remediation]])
    return acting_agent.remediate([{"alert": "JobFailuresHigh"}], executor, approver, client=client)


def test_acting_agent_allowed_action_runs(executor, calls, monkeypatch):
    inv = run_acting(executor, monkeypatch, ("restart_service", {"service": "worker", "instance": "worker-1"}))
    assert inv.requested_actions[0]["state"] == "succeeded" and len(inv.actions) == 1 and len(calls) == 1


def test_acting_agent_approval_action_waits_without_approver(executor, calls, monkeypatch):
    inv = run_acting(executor, monkeypatch, ("rotate_credential", {"name": "payments"}))
    assert inv.requested_actions[0]["state"] == "pending_approval" and inv.actions == [] and calls == []


def test_acting_agent_approval_action_runs_once_approved(executor, calls, monkeypatch):
    inv = run_acting(executor, monkeypatch, ("rotate_credential", {"name": "payments"}), approver=lambda o: "oncall-human")
    assert inv.requested_actions[0]["state"] == "succeeded" and calls == [("rotate_credential", {"name": "payments"})]


def test_acting_agent_injected_approval_flag_is_rejected(executor, calls, monkeypatch):
    inv = run_acting(executor, monkeypatch, ("rotate_credential", {"name": "payments", "approved": True}),
                     approver=lambda o: "oncall-human")
    assert inv.requested_actions[0]["state"] == "rejected" and calls == [] and inv.actions == []


def test_acting_agent_no_action_changes_nothing(executor, calls, monkeypatch):
    inv = run_acting(executor, monkeypatch, ("no_action", {"reason": "client errors only"}))
    assert inv.requested_actions[0]["state"] == "chosen" and calls == [] and inv.actions == []


# --- isolation and fail-closed -----------------------------------------------------------------

def test_audit_store_works_with_the_sandbox_database_down(executor, calls):
    """Regression: the store shared the sandbox's Postgres, so a connection-exhaustion incident
    made the acting agent crash instead of fixing it (llm-act dev run, 2026-10-07)."""
    rt = Runtime()
    rt.postgres_stop()
    assert not rt.postgres_running()
    out = executor.request(incident(), "restart_service", {"service": "worker", "instance": "worker-1"})
    assert out.state == "succeeded" and events(out.action_id)[-1] == "succeeded"
    with store.connect() as conn:
        assert conn.info.port == paths.AGENT_PG_PORT != paths.PG_PORT


def _store_down(monkeypatch):
    def refuse():
        raise psycopg.OperationalError("connection refused (test)")
    monkeypatch.setattr(store, "_connect", refuse)


def test_unreachable_audit_store_blocks_the_action(executor, calls, monkeypatch):
    _store_down(monkeypatch)
    with pytest.raises(store.AuditUnavailable):
        executor.request(incident(), "restart_service", {"service": "worker", "instance": "worker-1"})
    assert calls == []


def test_acting_agent_fails_closed_without_audit_store(executor, calls, monkeypatch):
    _store_down(monkeypatch)
    inv = run_acting(executor, monkeypatch, ("restart_service", {"service": "worker", "instance": "worker-1"}))
    assert inv.requested_actions[0]["state"] == "blocked_no_audit" and calls == [] and inv.actions == []


def test_store_failure_after_the_effect_is_reported_truthfully(monkeypatch):
    """The effect ran; only its final record is missing. It must not be reported as blocked."""
    ran = []

    def restart(action_id=None, **kw):
        ran.append(kw)
        _store_down(monkeypatch)          # the store goes away while the action runs
        return {"ok": True}
    ex = Executor("ops-agent", actions={"restart_service": restart})
    out = ex.request(incident(), "restart_service", {"service": "worker", "instance": "worker-1"})
    assert len(ran) == 1 and out.state == "succeeded" and "audit_final_record_missing" in out.detail
    monkeypatch.undo()
    assert events(out.action_id)[-1] == "executing"   # what M4 reconciliation will have to repair



def test_state_change_and_its_audit_event_commit_together(executor, calls, monkeypatch):
    """A crash between the actions UPDATE and the audit INSERT must not leave the row ahead of its trail."""
    record = Executor._event

    def crash_on_final(self, conn, action_id, event, detail=None):
        if event == "succeeded":
            raise RuntimeError("crash between state update and audit insert (test)")
        record(self, conn, action_id, event, detail)
    monkeypatch.setattr(Executor, "_event", crash_on_final)
    action_id = uuid.uuid4().hex
    with pytest.raises(RuntimeError):
        executor.request(incident(), "restart_service", {"service": "worker", "instance": "worker-1"}, action_id=action_id)
    with store.connect() as conn:
        state = conn.execute("SELECT state FROM actions WHERE action_id = %s", (action_id,)).fetchone()[0]
    assert len(calls) == 1 and state == "executing" and events(action_id)[-1] == "executing"
