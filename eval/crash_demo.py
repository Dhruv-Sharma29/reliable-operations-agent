"""End-to-end crash demo with the real model (PLAN.md M4): kill the agent between an effect and its
record, resume, show the effect happened exactly once.

    AGENT_LLM_URL=http://127.0.0.1:21091 python -m eval.crash_demo

1. Inject a bad config (dev variant 0) and fire the real alerts.
2. A child process runs the durable workflow with the real model and a stand-in approver, and
   kills itself (os._exit, no clean-up) right after the chosen action's effect, before its result
   is recorded.
3. This process resumes the incident with a model client that fails if called.
4. Checks in the target system: how many times the effect happened, whether the service is
   healthy, and what the audit trail shows.

If the model doesn't propose an action that has an effect (e.g. it misdiagnoses and escalates),
the crash point is never reached and the demo reports "inconclusive" instead of a result.
Writes results/summary/crash_demo.json.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import httpx

from agent import store, workflow
from agent.executor import Executor
from eval.verify import verify
from incidents.catalog import INCIDENTS, inject
from ops.alerts import evaluate
from sandbox import paths, state
from sandbox.runtime import Runtime

PARAMS = INCIDENTS["bad_config"].variants["dev"][0]


def _no_model() -> httpx.Client:
    def handler(request):
        raise AssertionError("model called during resume")
    return httpx.Client(transport=httpx.MockTransport(handler), base_url="http://none")


def _effects(action_id: str) -> dict:
    """Effect counts read from the target system, per kind of action."""
    return {"config_records": sum(r.get("action_id") == action_id for r in state.config_history()),
            "deploy_records": sum(r.get("action_id") == action_id for r in state.deploy_history()),
            "secret_written": state.secret_written_by("payments_token") == action_id,
            "api_started_by_action": Runtime().started_by("api") == action_id}


def _audit(action_id: str) -> list[str]:
    with store.connect() as conn:
        return [r[0] for r in conn.execute("SELECT event FROM audit_events WHERE action_id = %s ORDER BY id", (action_id,))]


def child(incident_id: str) -> None:
    from agent import executor as executor_mod
    executor_mod._after_effect = lambda action_id: os._exit(137)
    workflow.run(incident_id, Executor("ops-agent"), approver=lambda outcome: "demo-approver",
                 verify=lambda: {"healthy": True})
    sys.exit(3)   # crash point never reached: no action with an effect was executed


def main() -> None:
    rt = Runtime()
    rt.reset(workers=PARAMS["workers"])
    time.sleep(3)
    inject(rt, INCIDENTS["bad_config"], PARAMS)
    time.sleep(INCIDENTS["bad_config"].settle_s)
    alerts = evaluate()
    incident = workflow.open_incident(alerts)
    t0 = time.monotonic()
    r = subprocess.run([sys.executable, "-m", "eval.crash_demo", "child", incident], cwd=paths.ROOT,
                       capture_output=True, text=True, timeout=1800)
    child_s = round(time.monotonic() - t0, 1)
    inc = workflow.load(incident)
    report = {"incident": incident, "alerts": alerts, "child_exit": r.returncode, "child_seconds": child_s,
              "state_after_crash": inc["state"], "diagnosis": (inc.get("diagnosis") or {}).get("type"),
              "proposal": inc.get("proposal")}
    if r.returncode != 137:
        report["result"] = "inconclusive: the model's proposal never reached an effect"
        report["child_stderr"] = r.stderr[-1500:]
    else:
        action_id = inc["proposal"]["action_id"]
        report["after_crash"] = {"effects": _effects(action_id), "audit": _audit(action_id)}
        t1 = time.monotonic()
        inc = workflow.run(incident, Executor("ops-agent"), client=_no_model(), approver=lambda o: "demo-approver",
                           verify=lambda: verify(timeout_s=60))
        report["after_resume"] = {"state": inc["state"], "effects": _effects(action_id), "audit": _audit(action_id),
                                  "resume_seconds": round(time.monotonic() - t1, 1), "model_calls": 0,
                                  "config_ok": state.current_config()["api"]["db_port"] == paths.PG_PORT,
                                  "verified": verify(timeout_s=30)}
        e1, e2 = report["after_crash"]["effects"], report["after_resume"]["effects"]
        once = e1 == e2 and (e2["config_records"] + e2["deploy_records"] <= 1)
        report["result"] = ("passed: effect happened once, resumed without the model, incident "
                            f"{inc['state']}") if once else "FAILED: effect count changed on resume"
        report["history"] = workflow.history(incident)
    rt.reset()
    out = paths.ROOT / "results/summary/crash_demo.json"
    out.write_text(json.dumps(report, indent=1, default=str))
    print(json.dumps({k: report.get(k) for k in ("diagnosis", "proposal", "child_exit", "after_crash", "after_resume",
                                                 "history", "result")}, indent=1, default=str))


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "child":
        child(sys.argv[2])
    else:
        main()
