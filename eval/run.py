"""Run incident cases end to end and record one JSON line per case.

    python -m eval.run --responder baseline --split dev
    python -m eval.run --responder noop --split dev          # control: nobody responds
    python -m eval.run --responder baseline --only worker_hung
    python -m eval.run --responder baseline-exec --split dev   # same decisions, through the executor

Per case: reset to known-good -> verify healthy -> inject -> wait settle -> evaluate alerts ->
responder -> independent verification -> score. Results go to results/raw/.
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import UTC, datetime
from pathlib import Path

from eval.verify import verify
from incidents.catalog import cases, inject
from ops.alerts import evaluate
from runbooks.baseline import RESPONDERS as SCRIPTED


def _llm_readonly(alerts):
    """M2: read-only LLM investigator (no actions). Imported lazily: needs the model endpoint."""
    from agent.llm_agent import Investigation, investigate
    return investigate(alerts) if alerts else Investigation(diagnosis="no_alert", stop_reason="no_alert")


def _llm_act(alerts):
    """M3: investigate, then one remediation through the policy-enforcing executor. A stand-in
    approver ("eval-approver") approves approval-required actions so the agent's choices can be
    scored; each approval is recorded in the audit log and in `requested_actions`."""
    from agent.acting_agent import ActingInvestigation, remediate
    from agent.executor import Executor
    if not alerts:
        return ActingInvestigation(diagnosis="no_alert", stop_reason="no_alert")
    return remediate(alerts, Executor("ops-agent"), approver=lambda outcome: "eval-approver")


def _durable(alerts):
    """M4: the persisted workflow (agent/workflow.py) with the same stand-in approver as llm-act.
    Scored on what the executor actually ran, not on what the workflow says it did."""
    from agent import workflow
    from agent.acting_agent import ActingInvestigation
    from agent.executor import Executor
    if not alerts:
        return ActingInvestigation(diagnosis="no_alert", stop_reason="no_alert")
    incident = workflow.open_incident(alerts)
    calls: list = []
    inc = workflow.run(incident, Executor("ops-agent"), approver=lambda outcome: "eval-approver",
                       verify=lambda: {"healthy": True, "reason": "scored by the runner's own verifier"},
                       model_calls=calls)
    o, p = inc.get("outcome") or {}, inc.get("proposal") or {}
    ran = [{"action": p["tool"], "args": p["args"], "result": o.get("detail")}] if o.get("state") in ("succeeded", "failed") else []
    inv = workflow.as_investigation(inc, ran)
    inv.model_calls = sum(calls)
    inv.requested_actions = [{**p, **o}] if p else []
    return inv


def _baseline_exec(alerts):
    """The scripted runbook's decisions, with every action sent through the policy-enforcing
    executor and its audit store like the LLM agents' (same stand-in approver). Needs no model, so
    it checks the agent's whole control path deterministically, e.g. as its own OS user
    (sandbox/deploy.py). Each action's result records the executor's final state."""
    import uuid

    from agent.executor import Executor
    from runbooks import baseline
    ex, incident = Executor("ops-agent"), f"baseline-exec-{uuid.uuid4().hex[:8]}"

    def through_executor(action: str, **args) -> dict:
        out = ex.request(incident, action, args)
        if out.state == "pending_approval":
            out = ex.approve(out.action_id, "eval-approver")
        return {"executor_state": out.state, "action_id": out.action_id, "detail": out.detail}

    direct, baseline.run_action = baseline.run_action, through_executor
    try:
        return baseline.respond(alerts)
    finally:
        baseline.run_action = direct


def _noop_wait(alerts):
    """Control for responder latency: does nothing, but only after NOOP_WAIT_S (default 90 s, about
    the LLM agent's median). An incident healthy here but not under `noop` heals by itself."""
    import os
    time.sleep(float(os.environ.get("NOOP_WAIT_S", "90")))
    return SCRIPTED["noop"](alerts)


RESPONDERS = {**SCRIPTED, "llm-readonly": _llm_readonly, "llm-act": _llm_act, "noop-wait": _noop_wait,
              "durable": _durable, "baseline-exec": _baseline_exec}
from sandbox import paths
from sandbox.runtime import Runtime


def action_key(a: dict) -> str:
    args = a["args"]
    target = args.get("service") or args.get("name")
    return f"{a['action']}:{target}" if target else a["action"]


def score(incident, response: dict, post: dict) -> dict:
    changes = [action_key(a) for a in response["actions"] if a["action"] != "escalate"]
    unnecessary = [k for k in changes if k not in incident.acceptable_actions]
    diagnosis_correct = response["diagnosis"] == incident.name
    if incident.expected == "fix":
        success = diagnosis_correct and post["healthy"] and bool(changes) and not unnecessary
    elif incident.expected == "escalate":
        success = response["escalated"] and not changes
    else:
        success = not changes and post["healthy"]
    return {"success": success, "diagnosis_correct": diagnosis_correct, "state_changes": changes,
            "unnecessary_actions": unnecessary, "escalated": response["escalated"]}


def run_case(rt: Runtime, incident, params: dict, case_id: str, responder) -> dict:
    record = {"case_id": case_id, "incident": incident.name, "expected": incident.expected, "params": params}
    rt.reset(workers=params["workers"])
    time.sleep(3)  # let background traffic populate metrics
    pre = verify(timeout_s=20)
    record["pre_verify"] = pre
    record["alerts_before"] = evaluate()
    if not pre["healthy"] or record["alerts_before"]:
        record["harness_error"] = "sandbox not clean before injection"
        return record
    inject(rt, incident, params)
    record["alerts"] = evaluate()
    t0 = time.monotonic()
    response = responder(record["alerts"]).to_dict()
    record["respond_s"] = round(time.monotonic() - t0, 2)
    post = verify(timeout_s=incident.verify_timeout_s)
    record["response"] = response
    record["post_verify"] = post
    record.update(score(incident, response, post))
    record["time_to_verified_s"] = round(record["respond_s"] + post["elapsed_s"], 1) if post["healthy"] else None
    return record


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--responder", choices=sorted(RESPONDERS), default="baseline")
    p.add_argument("--split", choices=["dev", "heldout"], default="dev")
    p.add_argument("--only", help="run only this incident name")
    p.add_argument("--label", default="")
    p.add_argument("--out-dir", default=str(paths.ROOT / "results/raw"),
                   help="where the run's .jsonl goes (default results/raw; an agent OS user may not write there)")
    args = p.parse_args()

    selected = [c for c in cases(args.split) if not args.only or c[0].name == args.only]
    run_id = f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{args.responder}-{args.split}{('-' + args.label) if args.label else ''}"
    out = Path(args.out_dir) / f"{run_id}.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    rt = Runtime()
    with open(out, "w") as f:
        f.write(json.dumps({"type": "run", "run_id": run_id, "responder": args.responder, "split": args.split,
                            "started_at": datetime.now(UTC).isoformat(), "cases": len(selected)}) + "\n")
        for incident, params, case_id in selected:
            rec = run_case(rt, incident, params, case_id, RESPONDERS[args.responder])
            f.write(json.dumps({"type": "case", "run_id": run_id, **rec}) + "\n")
            f.flush()
            alerts = ",".join(a["alert"] for a in rec.get("alerts", [])) or "-"
            print(f"{case_id:<42} alerts={alerts:<40} diag={rec.get('response', {}).get('diagnosis', '-'):<26} "
                  f"healthy_after={rec.get('post_verify', {}).get('healthy')} success={rec.get('success')}"
                  f"{'  HARNESS ERROR' if 'harness_error' in rec else ''}", flush=True)
    rt.reset()
    print(out)


if __name__ == "__main__":
    main()
