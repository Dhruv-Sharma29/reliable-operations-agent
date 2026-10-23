"""Permitted execution (PLAN.md M3): every action the agent asks for goes through here.

    request -> known action tool? -> schema-valid arguments? -> policy decision
       allow     -> execute -> succeeded | failed
       approval  -> pending_approval -> approve(by someone else) -> execute | deny -> denied
       forbid / invalid -> rejected (nothing runs)

Identity (`requested_by`) and incident id come from the workflow, never from model output. Every
transition is persisted and written to the append-only audit log in the same transaction.
Fail closed: if the store is unreachable, store.AuditUnavailable propagates *before* any effect,
because the request, authorisation and "executing" records are all written first. (If the store
fails after an effect, the effect has happened but its final record is missing. That's M4's
crash-between-effect-and-record case.)
M4 adds deterministic action IDs and idempotency at the effect boundary.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from agent import store
from agent.policy import ALLOW, APPROVAL, decide, load_policy
from agent.tools.schemas import ACTION_TOOLS, ToolArgumentError, validate_call


class ApprovalError(Exception):
    pass


# Failures after which we can't tell whether the effect happened: reconcile, don't assume.
AMBIGUOUS = (TimeoutError, httpx.TimeoutException)
TERMINAL = {"succeeded", "failed", "denied", "rejected"}

# Test-only fault injection: called after an effect, before its result is recorded.
_after_effect: Callable[[str], None] | None = None


@dataclass
class Outcome:
    action_id: str
    state: str          # succeeded | failed | pending_approval | rejected | denied
    decision: str
    detail: object = None


class Executor:
    def __init__(self, principal: str, actions: dict[str, Callable] | None = None, policy: dict | None = None,
                 observe: Callable[[str, dict, str], bool | None] | None = None):
        from agent.tools import act
        self.principal = principal
        self.actions = actions or act.ACTIONS
        self.observe = observe or act.observe
        self.policy = policy or load_policy()   # store connects lazily: constructing never fails

    # --- persistence ---------------------------------------------------------------------------
    def _event(self, conn, action_id: str, event: str, detail: dict | None = None) -> None:
        conn.execute("INSERT INTO audit_events (action_id, event, detail) VALUES (%s, %s, %s)",
                     (action_id, event, json.dumps(detail or {}, default=str)))

    def _set(self, conn, action_id: str, event: str, detail: dict | None = None, **fields) -> None:
        if fields:
            cols = ", ".join(f"{k} = %s" for k in fields)
            conn.execute(f"UPDATE actions SET {cols} WHERE action_id = %s", (*fields.values(), action_id))
        self._event(conn, action_id, event, detail)

    # --- API -------------------------------------------------------------------------------------
    def request(self, incident_id: str, tool: str, args: dict, action_id: str | None = None) -> Outcome:
        """`action_id`: a stable id chosen by the workflow before asking. Requesting the same id
        again (e.g. after a crash) resumes that action instead of starting a new one. Recording the
        request and deciding on it happen in ONE transaction, so no crash leaves an undecided row."""
        action_id = action_id or uuid.uuid4().hex
        with store.connect() as conn, conn.transaction():
            inserted = conn.execute(
                "INSERT INTO actions (action_id, incident_id, tool, args, requested_by, decision, state) "
                "VALUES (%s, %s, %s, %s, %s, 'pending', 'requested') ON CONFLICT (action_id) DO NOTHING",
                (action_id, incident_id, tool, json.dumps(args), self.principal)).rowcount
            if inserted:
                outcome = self._decide(conn, action_id, tool, args)
            else:
                row = conn.execute("SELECT tool, args FROM actions WHERE action_id = %s", (action_id,)).fetchone()
                if (row[0], row[1]) != (tool, args):
                    raise ValueError(f"action {action_id} already exists for a different tool or arguments")
        if not inserted:
            return self.resume(action_id)
        if outcome.state == "authorized":
            return self._execute(action_id, tool, args, outcome.decision)
        return outcome

    def _decide(self, conn, action_id: str, tool: str, args: dict) -> Outcome:
        self._event(conn, action_id, "requested", {"tool": tool, "args": args, "by": self.principal})
        if tool not in ACTION_TOOLS:
            self._set(conn, action_id, "rejected", {"why": "not an action tool"}, decision="forbid",
                      state="rejected", reason="not an action tool", decided_at=_now())
            return Outcome(action_id, "rejected", "forbid", "not an action tool")
        try:
            validate_call(tool, args)          # schema first: policy only ever sees valid arguments
        except ToolArgumentError as e:
            self._set(conn, action_id, "rejected", {"why": str(e)}, decision="invalid", state="rejected",
                      reason=str(e)[:500], decided_at=_now())
            return Outcome(action_id, "rejected", "invalid", str(e))
        d = decide(self.policy, tool, args)
        fields = dict(decision=d.decision, rule=d.rule, policy_version=d.policy_version, reason=d.reason, decided_at=_now())
        if d.decision == APPROVAL:
            self._set(conn, action_id, "pending_approval", {"rule": d.rule}, state="pending_approval", **fields)
            return Outcome(action_id, "pending_approval", d.decision, d.reason)
        if d.decision != ALLOW:
            self._set(conn, action_id, "rejected", {"rule": d.rule}, state="rejected", **fields)
            return Outcome(action_id, "rejected", d.decision, d.reason)
        self._set(conn, action_id, "authorized", {"rule": d.rule}, state="authorized", **fields)
        return Outcome(action_id, "authorized", d.decision, d.reason)

    def approve(self, action_id: str, approver: str) -> Outcome:
        with store.connect() as conn, conn.transaction():
            row = conn.execute("SELECT tool, args, state, requested_by, decision FROM actions WHERE action_id = %s FOR UPDATE",
                               (action_id,)).fetchone()
            if row is None or row[2] != "pending_approval":
                raise ApprovalError(f"{action_id} is not awaiting approval")
            if approver == row[3]:
                raise ApprovalError("the requester cannot approve its own action")
            self._set(conn, action_id, "approved", {"by": approver}, approver=approver, approved_at=_now(), state="approved")
        return self._execute(action_id, row[0], row[1], row[4])

    def deny(self, action_id: str, approver: str, reason: str) -> Outcome:
        with store.connect() as conn, conn.transaction():
            state = conn.execute("SELECT state FROM actions WHERE action_id = %s FOR UPDATE", (action_id,)).fetchone()
            if state is None or state[0] != "pending_approval":
                raise ApprovalError(f"{action_id} is not awaiting approval")
            self._set(conn, action_id, "denied", {"by": approver, "reason": reason}, approver=approver,
                      state="denied", reason=reason[:500], finished_at=_now())
        return Outcome(action_id, "denied", "approval", reason)

    def resume(self, action_id: str) -> Outcome:
        """Continue an action from its persisted state. Never re-runs a finished action. An action
        that may have had its effect (`executing`, `unknown`) is reconciled first."""
        with store.connect() as conn:
            row = conn.execute("SELECT tool, args, state, decision, reason, result FROM actions WHERE action_id = %s",
                               (action_id,)).fetchone()
        if row is None:
            raise KeyError(action_id)
        tool, args, state, decision, reason, result = row
        if state in TERMINAL:
            return Outcome(action_id, state, decision, result if result is not None else reason)
        if state == "pending_approval":
            return Outcome(action_id, state, decision, reason)
        if state in ("authorized", "approved"):          # decided, never started: nothing to reconcile
            return self._execute(action_id, tool, args, decision)
        return self._reconcile(action_id, tool, args, decision)   # executing | unknown

    def _reconcile(self, action_id: str, tool: str, args: dict, decision: str) -> Outcome:
        seen = self.observe(tool, args, action_id)
        with store.connect() as conn, conn.transaction():
            self._event(conn, action_id, "reconciling", {"effect_observed": seen})
        if seen:
            result = {"ok": True, "detail": "effect observed in the system during reconciliation", "reconciled": True}
            with store.connect() as conn, conn.transaction():
                self._set(conn, action_id, "succeeded", {"result": result, "reconciled": True}, state="succeeded",
                          result=json.dumps(result), finished_at=_now())
            return Outcome(action_id, "succeeded", decision, result)
        # Not observed (or unobservable): re-run. Safe because every action is idempotent under its
        # action_id, either by recording it with the effect or by setting an absolute state.
        return self._execute(action_id, tool, args, decision, retry=True)

    def _execute(self, action_id: str, tool: str, args: dict, decision: str, retry: bool = False) -> Outcome:
        with store.connect() as conn, conn.transaction():
            self._set(conn, action_id, "executing", {"retry": True} if retry else None, state="executing",
                      started_at=_now())
        try:
            result = self.actions[tool](**args, action_id=action_id)
            state = "succeeded" if result.get("ok") else "failed"
        except AMBIGUOUS as e:
            # The effect may or may not have happened. Record that, then read the system.
            with store.connect() as conn, conn.transaction():
                self._set(conn, action_id, "unknown", {"why": f"{type(e).__name__}: {e}"}, state="unknown")
            if retry:   # reconciled once already and still ambiguous: stop, a human decides
                return Outcome(action_id, "unknown", decision, f"{type(e).__name__}: {e}")
            return self._reconcile(action_id, tool, args, decision)
        except Exception as e:  # noqa: BLE001 - recorded, then surfaced to the workflow
            result, state = {"ok": False, "detail": f"{type(e).__name__}: {e}"}, "failed"
        if _after_effect:
            _after_effect(action_id)
        try:
            with store.connect() as conn, conn.transaction():
                self._set(conn, action_id, state, {"result": result}, state=state, result=json.dumps(result, default=str),
                          finished_at=_now())
        except store.AuditUnavailable as e:
            # The effect HAS happened; report it truthfully. The audit trail shows it stuck in
            # "executing", and M4's reconciliation must repair it. Never report "blocked" here.
            result = {**result, "audit_final_record_missing": str(e)[:200]}
        return Outcome(action_id, state, decision, result)


def _now():
    from datetime import UTC, datetime
    return datetime.now(UTC)
