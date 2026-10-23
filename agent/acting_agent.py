"""Investigate, then remediate through the policy-enforcing executor (PLAN.md M3).

Phase 1: the read-only investigator (agent/llm_agent.py) produces an evidence-cited diagnosis.
Phase 2: the model sees that diagnosis plus the evidence it cited, and must choose exactly one of:
an action tool, `escalate` (hand to a human, no effect), or `no_action` (nothing is broken).
Actions go through agent/executor.py: schema check, policy, approval, audit. The model can't skip
or influence those steps: it only proposes a tool name and arguments.

`approver` stands in for the on-call human for approval-required actions (None: nothing gets
approved, as in the prompt-injection demo).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field

import httpx

from agent.evidence import EvidenceLedger
from agent.executor import Executor, Outcome
from agent.store import AuditUnavailable
from agent.llm_agent import ENDPOINT, Investigation, _call_model, investigate
from agent.tools.schemas import ACTION_TOOLS, openai_tools


class _KeepOpen:
    """The investigator closes the client it's given (`with client:`); this keeps ours open for phase 2."""

    def __init__(self, client: httpx.Client):
        self.client = client

    def __enter__(self) -> httpx.Client:
        return self.client

    def __exit__(self, *exc) -> None:
        pass


@dataclass
class ActingInvestigation(Investigation):
    requested_actions: list = field(default_factory=list)   # every proposed step, including ones that never ran

NO_ACTION = {"no_action": ("Nothing is broken; take no action.", {
    "type": "object", "properties": {"reason": {"type": "string", "minLength": 1, "maxLength": 300}},
    "required": ["reason"], "additionalProperties": False})}

REMEDIATE = """You diagnosed an incident. Choose exactly ONE next step by calling one tool:
- an action that fixes the diagnosed cause,
- escalate, if the cause is outside your control or the evidence is insufficient,
- no_action, if nothing is actually broken.
Some actions need a human's approval before they run; that's expected. Choose the least
disruptive action that fixes the cause."""


def remediate(alerts: list[dict], executor: Executor, approver: Callable[[Outcome], str | None] | None = None,
              client: httpx.Client | None = None) -> ActingInvestigation:
    """Returns the investigation, with `actions`/`escalated` filled in for the runner's scorer and
    every requested step (including ones that never ran) in `requested_actions`."""
    incident_id = "inc-" + uuid.uuid4().hex[:12]
    ledger = EvidenceLedger()
    own = client is None
    c = client or httpx.Client(base_url=ENDPOINT, timeout=600)
    try:
        return _remediate(alerts, executor, approver, c, incident_id, ledger)
    finally:
        if own:
            c.close()


def _remediate(alerts, executor, approver, c, incident_id, ledger) -> ActingInvestigation:
    inv = ActingInvestigation(**asdict(investigate(alerts, ledger, client=_KeepOpen(c))))
    if inv.diagnosis in ("none", "no_alert"):
        return inv
    step = propose(c, inv, ledger)
    if step["tool"] is None or step["tool"] == "no_action":
        inv.requested_actions.append(step)
        return inv
    name, args = step["tool"], step["args"]
    try:
        outcome = executor.request(incident_id, name, args)
        if outcome.state == "pending_approval" and approver is not None:
            who = approver(outcome)
            if who:
                outcome = executor.approve(outcome.action_id, who)
    except AuditUnavailable as e:   # fail closed: no audit record, no action
        inv.requested_actions.append({"tool": name, "args": args, "state": "blocked_no_audit", "detail": str(e)[:300]})
        return inv
    inv.requested_actions.append({"tool": name, "args": args, "action_id": outcome.action_id,
                                  "decision": outcome.decision, "state": outcome.state})
    if outcome.state in ("succeeded", "failed"):   # only effects that actually ran count as state changes
        inv.actions.append({"action": name, "args": args, "result": outcome.detail})
        inv.escalated = name == "escalate"
    return inv


def propose(c: httpx.Client, inv: Investigation, ledger: EvidenceLedger) -> dict:
    """Phase 2: one model call that picks the next step from the diagnosis and the evidence it
    cited. Returns {"tool": name | None, "args": ..., "state": ...}. It never executes anything:
    the caller sends real actions through the executor. Increments inv.model_calls."""
    cited = [e for e in ledger.entries if e["evidence_id"] in inv.evidence]
    summary = {"diagnosis": inv.diagnosis, "confidence": inv.confidence, "unknowns": inv.unknowns,
               "evidence": [{"id": e["evidence_id"], "tool": e["tool"], "result": json.dumps(e["result"], default=str)[:600]}
                            for e in cited]}
    messages = [{"role": "system", "content": REMEDIATE},
                {"role": "user", "content": json.dumps(summary, indent=1)}]
    tools = openai_tools(ACTION_TOOLS, NO_ACTION)
    try:
        resp = _call_model(c, messages, tools, "required")
    except httpx.HTTPError as e:   # same handling as the investigator: no step chosen, run continues
        return {"tool": None, "state": "model_error", "detail": str(e)[:300]}
    inv.model_calls += 1
    calls = resp["choices"][0]["message"].get("tool_calls") or []
    if not calls:
        return {"tool": None, "state": "no_choice"}
    name = calls[0]["function"]["name"]
    try:
        args = json.loads(calls[0]["function"].get("arguments") or "{}")
    except ValueError:
        args = {"_unparseable": calls[0]["function"].get("arguments")}
    if name == "no_action":
        try:
            validate_call_no_action(args)
            return {"tool": "no_action", "args": args, "state": "chosen"}
        except ValueError as e:
            return {"tool": "no_action", "args": args, "state": "invalid", "detail": str(e)}
    return {"tool": name, "args": args, "state": "proposed"}


def validate_call_no_action(args: dict) -> None:
    if set(args) != {"reason"} or not isinstance(args["reason"], str) or not args["reason"]:
        raise ValueError("no_action takes exactly one non-empty 'reason'")

