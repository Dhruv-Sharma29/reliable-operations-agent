"""Durable incident workflow (PLAN.md M4): a persisted state machine around the agent.

    OPEN -> INVESTIGATING -> DIAGNOSED -> PROPOSED -> EXECUTING -> VERIFYING -> RESOLVED
                                 |            |          |   ^            \\-> ESCALATED
                                 |            |          v   |  (approved)
                                 |            \\-> AWAITING_APPROVAL
                                 \\-> ESCALATED (insufficient evidence: no action)

Rules that make it safe to kill at any point and resume:
- Each transition commits (state + append-only event) BEFORE the work it leads to starts.
- What the model decided is persisted: the diagnosis at DIAGNOSED, and the proposed tool, args and
  action_id at PROPOSED. A resumed incident never asks the model again for a step it completed.
- The action id is derived from the incident id (uuid5), fixed before execution. Resuming from
  EXECUTING re-requests the same id, so the executor resumes or reconciles. It never starts a
  second action, and the action tools ignore repeats under the same id.
- INVESTIGATING has no side effects (read tools only), so a crash there just re-runs it.

Authorization (allow / approval / forbid) and the action's own states live in agent/executor.py.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable

import httpx

from agent import store
from agent.acting_agent import ActingInvestigation, _KeepOpen, propose
from agent.evidence import EvidenceLedger
from agent.executor import Executor, Outcome
from agent.llm_agent import ENDPOINT, Investigation, investigate

TERMINAL = {"RESOLVED", "ESCALATED"}
SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    incident_id text PRIMARY KEY,
    state       text NOT NULL,
    alerts      jsonb NOT NULL,
    diagnosis   jsonb,
    proposal    jsonb,              -- {"tool", "args", "action_id"}: fixed before execution
    outcome     jsonb,
    reason      text,
    opened_at   timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at  timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE IF NOT EXISTS incident_events (
    id          bigserial PRIMARY KEY,
    incident_id text NOT NULL,
    at          timestamptz NOT NULL DEFAULT clock_timestamp(),
    from_state  text,
    to_state    text NOT NULL,
    detail      jsonb NOT NULL DEFAULT '{}'
);
DROP TRIGGER IF EXISTS incident_events_no_change ON incident_events;
CREATE TRIGGER incident_events_no_change BEFORE UPDATE OR DELETE ON incident_events
    FOR EACH ROW EXECUTE FUNCTION audit_append_only();
"""

# Test-only fault injection: called after a transition commits, before the next step runs.
_after_transition: Callable[[str, str], None] | None = None


_applied_for = -1   # store.generation the workflow schema was last applied for


def ensure() -> None:
    global _applied_for
    store.ensure()
    if _applied_for != store.generation:
        if store.is_admin():   # a separate agent OS user relies on `python -m agent.store init` having run
            store.apply_schema(SCHEMA, {"incidents": "SELECT, INSERT, UPDATE", "incident_events": "SELECT, INSERT"})
        _applied_for = store.generation


def action_id_for(incident_id: str, step: str = "remediation") -> str:
    """Deterministic: the same incident step always maps to the same action id."""
    return uuid.uuid5(uuid.NAMESPACE_URL, f"ops-agent/{incident_id}/{step}").hex


def open_incident(alerts: list[dict], incident_id: str | None = None) -> str:
    ensure()
    incident_id = incident_id or "inc-" + uuid.uuid4().hex[:12]
    with store.connect() as conn, conn.transaction():
        conn.execute("INSERT INTO incidents (incident_id, state, alerts) VALUES (%s, 'OPEN', %s)",
                     (incident_id, json.dumps(alerts)))
        conn.execute("INSERT INTO incident_events (incident_id, to_state) VALUES (%s, 'OPEN')", (incident_id,))
    return incident_id


def load(incident_id: str) -> dict:
    with store.connect() as conn:
        row = conn.execute("SELECT state, alerts, diagnosis, proposal, outcome, reason FROM incidents "
                           "WHERE incident_id = %s", (incident_id,)).fetchone()
    if row is None:
        raise KeyError(incident_id)
    return dict(zip(("state", "alerts", "diagnosis", "proposal", "outcome", "reason"), row, strict=True),
                incident_id=incident_id)


def history(incident_id: str) -> list[str]:
    with store.connect() as conn:
        return [r[0] for r in conn.execute("SELECT to_state FROM incident_events WHERE incident_id = %s ORDER BY id",
                                           (incident_id,))]


def _move(inc: dict, to: str, detail: dict | None = None, **fields) -> None:
    sets = ", ".join(f"{k} = %s" for k in ("state", *fields)) + ", updated_at = clock_timestamp()"
    values = [to, *(json.dumps(v, default=str) if k in ("diagnosis", "proposal", "outcome") else v
                     for k, v in fields.items())]
    with store.connect() as conn, conn.transaction():
        conn.execute(f"UPDATE incidents SET {sets} WHERE incident_id = %s", (*values, inc["incident_id"]))
        conn.execute("INSERT INTO incident_events (incident_id, from_state, to_state, detail) VALUES (%s, %s, %s, %s)",
                     (inc["incident_id"], inc["state"], to, json.dumps(detail or {}, default=str)))
    inc.update(state=to, **fields)
    if _after_transition:
        _after_transition(inc["incident_id"], to)


def run(incident_id: str, executor: Executor, *, client: httpx.Client | None = None,
        approver: Callable[[Outcome], str | None] | None = None,
        verify: Callable[[], dict] | None = None, model_calls: list | None = None) -> dict:
    """Advance the incident until it is RESOLVED / ESCALATED or waits for a human approval.
    Safe to call again on the same incident at any time (that is how it resumes)."""
    own = client is None
    c = client or httpx.Client(base_url=ENDPOINT, timeout=600)
    try:
        inc = load(incident_id)
        while inc["state"] not in TERMINAL:
            before = inc["state"]
            _step(inc, executor, c, approver, verify, model_calls)
            if inc["state"] == before:      # waiting (approval) or nothing more to do now
                break
        return inc
    finally:
        if own:
            c.close()


def _step(inc, executor, c, approver, verify, model_calls) -> None:
    state = inc["state"]
    if state == "OPEN":
        _move(inc, "INVESTIGATING")
    elif state == "INVESTIGATING":
        ledger = EvidenceLedger()
        inv = investigate(inc["alerts"], ledger, client=_KeepOpen(c))
        _count(model_calls, inv.model_calls)
        diagnosis = {"type": inv.diagnosis, "evidence": inv.evidence, "confidence": inv.confidence,
                     "unknowns": inv.unknowns, "citations_valid": inv.citations_valid,
                     "cited": [e for e in ledger.entries if e["evidence_id"] in inv.evidence],
                     "stop_reason": inv.stop_reason,
                     # kept for audit and the M5 report: what was read, what it returned, what the model said
                     "tool_calls": inv.tool_calls, "evidence_log": inv.evidence_log, "transcript": inv.transcript,
                     "prompt_version": inv.prompt_version, "final_mode": inv.final_mode}
        _move(inc, "DIAGNOSED", {"type": inv.diagnosis}, diagnosis=diagnosis)
    elif state == "DIAGNOSED":
        d = inc["diagnosis"]
        if d["type"] in ("unknown", "none") or not d["citations_valid"]:
            _move(inc, "ESCALATED", {"why": "insufficient evidence"}, reason="insufficient evidence: no action taken")
            return
        inv = Investigation(diagnosis=d["type"], evidence=d["evidence"], confidence=d["confidence"], unknowns=d["unknowns"])
        ledger = EvidenceLedger()
        ledger.entries = d["cited"]
        step = propose(c, inv, ledger)
        _count(model_calls, inv.model_calls)
        if step["tool"] is None or step["state"] == "invalid":
            _move(inc, "ESCALATED", {"why": step["state"]}, reason=f"no valid next step ({step['state']})")
        elif step["tool"] == "no_action":
            _move(inc, "VERIFYING", {"no_action": step["args"]}, proposal={**step, "action_id": None})
        else:
            proposal = {"tool": step["tool"], "args": step["args"], "action_id": action_id_for(inc["incident_id"])}
            _move(inc, "PROPOSED", proposal, proposal=proposal)
    elif state == "PROPOSED":
        _move(inc, "EXECUTING")
    elif state in ("EXECUTING", "AWAITING_APPROVAL"):
        p = inc["proposal"]
        outcome = executor.request(inc["incident_id"], p["tool"], p["args"], action_id=p["action_id"])
        if outcome.state == "pending_approval" and approver is not None:
            who = approver(outcome)
            if who:
                outcome = executor.approve(outcome.action_id, who)
        record = {"action_id": outcome.action_id, "state": outcome.state, "decision": outcome.decision,
                  "detail": outcome.detail}
        if outcome.state == "pending_approval":
            if state != "AWAITING_APPROVAL":
                _move(inc, "AWAITING_APPROVAL", record, outcome=record)
        elif outcome.state == "succeeded" and p["tool"] == "escalate":
            _move(inc, "ESCALATED", record, outcome=record, reason=p["args"].get("reason"))
        elif outcome.state == "succeeded":
            _move(inc, "VERIFYING", record, outcome=record)
        else:   # rejected, denied, failed, unknown: a human takes over
            _move(inc, "ESCALATED", record, outcome=record, reason=f"action {outcome.state}")
    elif state == "VERIFYING":
        result = (verify or _default_verify)()
        if result["healthy"]:
            _move(inc, "RESOLVED", result)
        else:
            _move(inc, "ESCALATED", result, reason=f"not healthy after action: {result.get('reason')}")


def _default_verify() -> dict:
    from eval.verify import verify   # the same independent probe the evaluation uses
    return verify(timeout_s=60)


def _count(model_calls: list | None, n: int) -> None:
    if model_calls is not None:
        model_calls.append(n)


def resume_all(executor: Executor, **kw) -> list[dict]:
    """After a crash: continue every incident that isn't finished (agent/supervisor.py calls this at
    start-up). One incident failing, e.g. because the model endpoint is down, doesn't stop the
    others: its error is returned in its place and the incident stays where it was."""
    ensure()
    with store.connect() as conn:
        ids = [r[0] for r in conn.execute("SELECT incident_id FROM incidents WHERE state NOT IN ('RESOLVED', 'ESCALATED') "
                                          "ORDER BY opened_at")]
    results = []
    for i in ids:
        try:
            results.append(run(i, executor, **kw))
        except Exception as e:  # noqa: BLE001 - reported per incident; the supervisor keeps going
            results.append({"incident_id": i, "error": f"{type(e).__name__}: {e}"[:300]})
    return results


def as_investigation(inc: dict, actions: list[dict]) -> ActingInvestigation:
    """Shape a finished incident like the other responders' output, for the evaluation scorer."""
    d = inc.get("diagnosis") or {}
    inv = ActingInvestigation(diagnosis=d.get("type", "none"), evidence=d.get("evidence", []),
                              confidence=d.get("confidence"), unknowns=d.get("unknowns", []),
                              citations_valid=d.get("citations_valid", False), stop_reason=inc["state"],
                              tool_calls=d.get("tool_calls", []), evidence_log=d.get("evidence_log", []),
                              transcript=d.get("transcript", []), prompt_version=d.get("prompt_version", ""),
                              final_mode=d.get("final_mode", ""))
    inv.actions = actions
    inv.escalated = inc["state"] == "ESCALATED"
    return inv
