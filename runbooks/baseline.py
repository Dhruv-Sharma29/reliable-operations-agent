"""Scripted baseline responder: deterministic rules from RUNBOOKS.md over the same read tools the
LLM agent will get. Caveat for comparisons: these rules were written knowing the incident
catalogue, so held-out variants test robustness to parameter changes, not to unseen incident types.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field

from agent.tools import act, read

RECENT_S = 120
SLOW_DEPENDENCY_MS = 1000
# How an action runs: directly by default; eval.run's `baseline-exec` sends it through the executor.
run_action: Callable[..., dict] = lambda action, **args: act.ACTIONS[action](**args)


@dataclass
class Response:
    diagnosis: str
    evidence: list[str] = field(default_factory=list)
    actions: list[dict] = field(default_factory=list)
    escalated: bool = False

    def do(self, action: str, **args) -> None:
        result = run_action(action, **args)
        self.actions.append({"action": action, "args": args, "result": result})
        if action == "escalate":
            self.escalated = True

    def to_dict(self) -> dict:
        return asdict(self)


def _recent_logs(instance: str, **kw) -> list[dict]:
    cutoff = time.time() - RECENT_S
    return [r for r in read.search_logs(instance, **kw) if r["ts"] >= cutoff]


def respond(alerts: list[dict]) -> Response:
    if not alerts:
        return Response("no_alert")
    status = read.service_status()
    conns = read.db_connections()
    heartbeats = read.worker_heartbeats()
    api = read.api_metrics()
    workers = read.worker_metrics()
    queue = read.queue_stats()

    # 1. Connection exhaustion first: it also makes heartbeats stale and the API fail.
    by_app: dict[str, int] = {}
    for row in conns["by_application"]:
        by_app[row["application"]] = by_app.get(row["application"], 0) + row["count"]
    if by_app.get("report-exporter", 0) >= conns["max_connections"] // 2:
        r = Response("db_connection_exhaustion",
                     [f"report-exporter holds {by_app['report-exporter']}/{conns['max_connections']} connections"])
        r.do("restart_service", service="report-exporter")
        return r

    # 2. Crashed workers.
    dead = [s["instance"] for s in status if s["service"] == "worker" and not s["running"]]
    if dead:
        r = Response("worker_crashed", [f"{i} not running (stale pid file)" for i in dead])
        for inst in dead:
            r.do("restart_service", service="worker", instance=inst)
        return r

    # 3. API 5xx / 507 causes, distinguished by log evidence.
    api_errors = {} if "error" in api else {c: n for c, n in api["by_status"].items() if int(c) >= 400}
    server_errors = sum(n for c, n in api_errors.items() if int(c) >= 500)
    if server_errors:
        api_err_logs = _recent_logs("api", level="error")
        last_change = read.config_history(last=1)[-1]
        if any("connection refused" in str(l.get("error", "")).lower() for l in api_err_logs) \
                and "api.db_port" in last_change["changed"]:
            r = Response("bad_config", [f"API logs: connection refused; config v{last_change['version']} changed "
                                        f"api.db_port {last_change['changed']['api.db_port']}"])
            r.do("rollback_config")
            return r
        if any(l["msg"] == "spool quota exceeded" for l in api_err_logs):
            r = Response("disk_fill", ["API logs: spool quota exceeded (507)"])
            r.do("cleanup_spool", pattern="*.tmp")
            return r
        deploys = read.deploy_history(last=2)
        get_500 = api["by_route_status"].get("GET /jobs/{id} 500", 0)
        if get_500 and any(l["msg"] == "unhandled exception" for l in api_err_logs) \
                and len(deploys) == 2 and deploys[-1]["age_s"] < 900:
            r = Response("bad_deploy", [f"{get_500} x GET /jobs/{{id}} 500 after deploy of {deploys[-1]['release']}"])
            r.do("rollback_release")
            return r

    # 4. Expired payments credential.
    for inst in workers:
        if any("token_expired" in str(l.get("error", "")) for l in _recent_logs(inst, level="error")):
            r = Response("expired_credential", [f"{inst} logs: payments returned 401 token_expired"])
            r.do("rotate_credential", name="payments")
            return r

    # 5. Hung workers: running, but heartbeat stale.
    running = {s["instance"] for s in status if s["service"] == "worker" and s["running"]}
    hung = [h["worker_id"] for h in heartbeats if h["expected"] and h["age_s"] > 5 and h["worker_id"] in running]
    if hung:
        r = Response("worker_hung", [f"{w} running, /health ok, heartbeat stale" for w in hung])
        for inst in hung:
            r.do("restart_service", service="worker", instance=inst)
        return r

    # 6. Slow external dependency: escalate, don't scale into it.
    slow = {i: m.get("dep_latency_p95_ms") for i, m in workers.items() if (m.get("dep_latency_p95_ms") or 0) > SLOW_DEPENDENCY_MS}
    if slow:
        r = Response("slow_dependency", [f"payments p95 latency {slow}"])
        r.do("escalate", reason="payments provider latency high; outside sandbox control")
        return r

    # 7. Plain backlog with healthy workers.
    if queue["pending"] > 300 or (queue["oldest_pending_age_s"] or 0) > 20:
        current = len(running)
        r = Response("queue_backlog", [f"{queue['pending']} pending, {current} healthy workers"])
        r.do("scale_service", service="worker", replicas=min(4, current + 2))
        return r

    # 8. Only client errors: false alarm.
    if api_errors and not server_errors:
        return Response("false_alarm_client_errors", [f"API errors are all 4xx: {api_errors}"])

    r = Response("unknown", [f"alerts: {[a['alert'] for a in alerts]}"])
    r.do("escalate", reason="no runbook rule matched the evidence")
    return r


def noop(alerts: list[dict]) -> Response:
    """Control responder: never acts. Shows which incidents are real (stay broken without help)."""
    return Response("none")


RESPONDERS = {"baseline": respond, "noop": noop}
