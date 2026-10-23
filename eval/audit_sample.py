"""Audit trace sample (definition of done): full action records and audit events for every path
through the executor, read back from the agent's own store.

    python -m eval.audit_sample

Recorded paths are taken from the store as the evaluation runs and the crash demo left them:
allowed -> succeeded, approval -> succeeded, approval -> failed, and the crash demo's action
(executing -> reconciling -> succeeded after the process was killed).

The paths where nothing runs (forbidden, invalid, denied) never happened in an evaluation run, so
they are produced here through the real executor and store, with action functions that fail if
called. Their incident ids start with `audit-sample-`.

Last, it tries to UPDATE, DELETE and TRUNCATE audit_events and records that the store refused.
Writes results/summary/audit_trace_sample.json.
"""

from __future__ import annotations

import json
import uuid

import psycopg

from agent import store
from agent.executor import ApprovalError, Executor
from agent.tools.schemas import ACTION_TOOLS
from sandbox import paths

CRASH_DEMO = paths.ROOT / "results/summary/crash_demo.json"


def _no_effect(name):
    def run(**kw):
        raise AssertionError(f"{name} must not run in the audit sample")
    return run


def _trace(conn, action_id: str) -> dict:
    cur = conn.execute("SELECT * FROM actions WHERE action_id = %s", (action_id,))
    action = dict(zip([c.name for c in cur.description], cur.fetchone()))
    events = [{"id": i, "at": at, "event": ev, "detail": d} for i, at, ev, d in conn.execute(
        "SELECT id, at, event, detail FROM audit_events WHERE action_id = %s ORDER BY id", (action_id,))]
    return {"action": action, "events": events}


def _latest(conn, where: str) -> str | None:
    row = conn.execute(f"SELECT action_id FROM actions WHERE incident_id NOT LIKE 'audit-sample-%%' AND {where} "
                       "ORDER BY requested_at DESC LIMIT 1").fetchone()
    return row and row[0]


def _tamper(conn, action_id: str) -> dict:
    attempts = {"update": "UPDATE audit_events SET event = 'succeeded' WHERE action_id = %s",
                "delete": "DELETE FROM audit_events WHERE action_id = %s",
                "truncate": "TRUNCATE audit_events"}
    out = {}
    for name, sql in attempts.items():
        try:
            with conn.transaction():
                conn.execute(sql, (action_id,) if "%s" in sql else None)
            out[name] = "ALLOWED"
        except psycopg.Error as e:
            out[name] = f"refused: {str(e).splitlines()[0]}"
    return out


def main() -> None:
    crash_action = json.loads(CRASH_DEMO.read_text())["proposal"]["action_id"]
    recorded = {"allowed_succeeded": "decision = 'allow' AND state = 'succeeded'",
                "approved_succeeded": "decision = 'approval' AND state = 'succeeded' AND approver = 'eval-approver'",
                "approved_failed": "decision = 'approval' AND state = 'failed'"}
    report = {"store": f"database {store.DB} on the agent's own PostgreSQL (port {paths.AGENT_PG_PORT})",
              "recorded": {}, "generated": {}}
    with store.connect() as conn:
        for name, where in recorded.items():
            action_id = _latest(conn, where)
            report["recorded"][name] = _trace(conn, action_id) if action_id else None
        report["recorded"]["crash_reconciled"] = _trace(conn, crash_action)

    ex = Executor("ops-agent", actions={n: _no_effect(n) for n in ACTION_TOOLS})
    incident = f"audit-sample-{uuid.uuid4().hex[:8]}"
    forbidden = ex.request(incident, "drop_table", {"table": "payments"})
    invalid = ex.request(incident, "restart_service", {"service": "api", "approved": True})
    pending = ex.request(incident, "rollback_release", {})
    try:
        ex.approve(pending.action_id, "ops-agent")
        self_approval = "ALLOWED"
    except ApprovalError as e:
        self_approval = f"refused: {e}"
    denied = ex.deny(pending.action_id, "audit-sample-approver", "no evidence the release is at fault")

    with store.connect() as conn:
        for name, out in (("forbidden", forbidden), ("invalid", invalid), ("denied", denied)):
            report["generated"][name] = _trace(conn, out.action_id)
        report["generated"]["denied"]["self_approval_attempt"] = self_approval
        report["append_only"] = _tamper(conn, crash_action)

    ok = (forbidden.state == invalid.state == "rejected" and denied.state == "denied"
          and self_approval.startswith("refused") and all(v.startswith("refused") for v in report["append_only"].values()))
    report["result"] = "passed" if ok else "FAILED"
    out = paths.ROOT / "results/summary/audit_trace_sample.json"
    out.write_text(json.dumps(report, indent=1, default=str))
    for group in ("recorded", "generated"):
        for name, t in report[group].items():
            print(f"{group:9} {name:19}", t and " -> ".join(e["event"] for e in t["events"]))
    print("self-approval:", self_approval)
    print("append-only:", report["append_only"])
    print(report["result"], "->", out.relative_to(paths.ROOT))


if __name__ == "__main__":
    main()
