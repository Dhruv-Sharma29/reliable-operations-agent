"""Alert rules: the trigger an operator (or the agent) receives. Deliberately simple and
symptom-based, like many real alerting setups, so one alert can have several causes and an alert
can fire when nothing is broken (ApiErrorRateHigh counts client 4xx too; Api5xxRateHigh doesn't)."""

from __future__ import annotations

from agent.tools import read

ERROR_RATE_THRESHOLD = 0.10
SERVER_ERROR_RATE_THRESHOLD = 0.02  # 5xx are our failures; alert on them much earlier than on 4xx
MIN_REQUESTS = 10
BACKLOG_PENDING = 300
BACKLOG_AGE_S = 20
HEARTBEAT_STALE_S = 5
FAILED_JOBS_60S = 5
JOB_DURATION_P95_S = 1.0


def evaluate() -> list[dict]:
    alerts = []
    api = read.api_metrics()
    if "error" in api:
        alerts.append({"alert": "ApiUnreachable", "detail": api["error"]})
    elif api["requests"] >= MIN_REQUESTS:
        errors = sum(n for code, n in api["by_status"].items() if int(code) >= 400)
        server = sum(n for code, n in api["by_status"].items() if int(code) >= 500)
        if errors / api["requests"] > ERROR_RATE_THRESHOLD:
            alerts.append({"alert": "ApiErrorRateHigh", "detail": f"{errors}/{api['requests']} requests >= 400 in 60s"})
        # Added after the no-op control showed a bug failing ~10% of requests (only GETs of jobs
        # without a customer) sat at the generic threshold and never alerted.
        if server >= 3 and server / api["requests"] > SERVER_ERROR_RATE_THRESHOLD:
            alerts.append({"alert": "Api5xxRateHigh", "detail": f"{server}/{api['requests']} requests 5xx in 60s"})
    q = read.queue_stats()
    if q["pending"] > BACKLOG_PENDING or (q["oldest_pending_age_s"] or 0) > BACKLOG_AGE_S:
        alerts.append({"alert": "JobBacklogHigh", "detail": f"{q['pending']} pending, oldest {q['oldest_pending_age_s']}s"})
    if q["failed_last_60s"] >= FAILED_JOBS_60S:
        alerts.append({"alert": "JobFailuresHigh", "detail": f"{q['failed_last_60s']} failed jobs in 60s"})
    if (q["job_duration_p95_s"] or 0) > JOB_DURATION_P95_S:
        alerts.append({"alert": "JobLatencyHigh", "detail": f"p95 job duration {q['job_duration_p95_s']}s"})
    stale = [h["worker_id"] for h in read.worker_heartbeats() if h["expected"] and h["age_s"] > HEARTBEAT_STALE_S]
    if stale:
        alerts.append({"alert": "WorkerHeartbeatStale", "detail": f"stale: {', '.join(stale)}"})
    return alerts
