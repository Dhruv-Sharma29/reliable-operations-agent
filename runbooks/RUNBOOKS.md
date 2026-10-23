# Runbooks — jobs service sandbox

Human-readable procedures. The scripted baseline (`runbooks/baseline.py`) implements these rules;
the LLM agent can read this file through its `runbook` tool. Always gather evidence before acting
and verify end-to-end afterwards.

## ApiErrorRateHigh / Api5xxRateHigh

1. Split errors by status in `api_metrics.by_route_status`. If every error is 4xx (no 5xx), the
   service is rejecting bad client input: **no action**, close as a false alarm and notify the client owner.
2. 5xx on all routes with `database error ... connection refused` in API logs: check
   `config_history` for a recent change to `api.db_port`. **Roll back the config.**
3. 5xx with `too many clients` / `remaining connection slots`: see *Database connections exhausted*.
4. 5xx only on `GET /jobs/{id}` with an unhandled exception, shortly after a deploy in
   `deploy_history`: **roll back the release.**
5. 507 with `spool quota exceeded` in API logs: **clean up `*.tmp` files in the spool.** Never
   delete other spool files.

## Database connections exhausted

`db_connections` shows one application holding most of `max_connections`. If it is
`report-exporter`, **restart it** (or stop it). Never terminate API or worker sessions by hand.
Worker heartbeats also go stale here, because heartbeats need a DB connection; fix the
connection leak first.

## WorkerHeartbeatStale

1. `service_status`: if the worker instance is **not running**, it crashed: **restart that worker**.
2. If it is running and `/health` says ok but the heartbeat is stale and `jobs_done` stops
   increasing, the loop is hung: **restart that worker**. Liveness checks don't detect this.
3. If *all* heartbeats are stale, check database connections first.

## JobFailuresHigh

Worker logs show `payments returned 401 ... token_expired`: the provider credential expired.
**Rotate the payments credential.** Other 4xx/5xx from payments: escalate.

## JobLatencyHigh

If `worker_metrics.dep_latency_p95_ms` is high, the external payments provider is slow. It is
outside our control: **escalate** to the provider on-call. Scaling workers just adds load to a
slow dependency.

## JobBacklogHigh

If workers are running, heartbeats are fresh, failures are low, and dependency latency is normal,
the queue is receiving more work than current capacity: **scale workers** (max 4).
If a worker is down or hung, handle that first.

## Escalation

Escalate when evidence is insufficient or conflicting, or when the only fix is outside the
sandbox allowlist. State what you checked and what you could not rule out.
