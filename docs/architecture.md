# Architecture — M1 (sandbox, faults, scripted baseline)

```text
                         ┌──────────────── sandbox (local processes; Compose later) ────────────────┐
 loadgen ──HTTP──►  api ─┤──SQL──► Postgres 16 ◄──SQL── worker-1..N ──HTTP──► payments (external)    │
 (~7.5 req/s)      :18080│          :18432 (jobs,         :18091+          :18070                    │
                         │           heartbeats)  ▲                                                  │
                         │  report-exporter ──SQL─┘                                                  │
                         └───────────────────────────────────────────────────────────────────────────┘
        JSON logs (.run/logs)   /metrics (JSON, 60 s windows)   config/deploy history   pg_stat_activity
                     │                    │                            │                     │
                     └──────────── read tools (agent/tools/read.py) ───┴─────────────────────┘
                                          │
  incident catalogue ─inject─► alert rules ─► responder (scripted baseline | noop | LLM agent in M2+)
                                                    │ actions (agent/tools/act.py)
                                                    ▼
                                independent verifier (probe jobs through the public API) ─► score
```

## Evaluation loop (one case)

1. `Runtime.reset(workers=N)`: stop everything, truncate tables, restore config v1, release 2.3.0 and
   a fresh payments token, clear faults/spool/logs, start all components.
2. Verify healthy with no alerts. Otherwise the case is recorded as a harness error and not scored.
3. Inject the fault and wait for the incident's settle time.
4. Evaluate alert rules. That's all the responder is handed.
5. Responder investigates with read tools and may act.
6. Independent verification sends probe jobs through the API and requires them to complete.
7. Score: diagnosis, state-changing actions against the incident's acceptable set, verified health.

## Incident catalogue

| Incident | Primary alert(s) | Distinguishing evidence | Correct response |
| --- | --- | --- | --- |
| worker_crashed | WorkerHeartbeatStale | process not running | restart that worker |
| worker_hung | WorkerHeartbeatStale | process running, `/health` ok, heartbeat stale | restart that worker |
| bad_config | ApiErrorRateHigh | API logs `connection refused`; config change to `api.db_port` | roll back config |
| db_connection_exhaustion | ApiErrorRateHigh, WorkerHeartbeatStale | `report-exporter` holds most connections | restart/stop exporter |
| queue_backlog | JobBacklogHigh | workers fine, failures low, latency normal | scale workers |
| slow_dependency | JobLatencyHigh (+ backlog) | payments p95 latency high | **escalate** (external) |
| disk_fill | ApiErrorRateHigh | 507 + `spool quota exceeded` | delete `*.tmp` in spool |
| bad_deploy | Api5xxRateHigh (often not ApiErrorRateHigh) | 500 only on `GET /jobs/{id}` after deploy 2.3.1 | roll back release |
| expired_credential | JobFailuresHigh | worker logs `401 token_expired` | rotate payments credential |
| false_alarm_client_errors | ApiErrorRateHigh | errors are all 4xx | **no action** |

Several incidents share an alert on purpose. The alert says *something is wrong*; the evidence
says *what*. Connection exhaustion also makes heartbeats stale, so a responder that trusts the
first alert will "fix" the wrong thing.

## Design decisions

- **Processes now, containers later.** Docker isn't available on this machine yet. `Runtime`
  exposes start/stop/restart/scale/kill, which is what Compose would provide. Action tools only
  call `Runtime`, so switching backends won't change the tool surface the agent sees.
- **Postgres from the Ubuntu archive, unpacked in user space** (`scripts/setup_postgres.sh`), run as
  `nobody` because Postgres refuses root. No system packages installed, no permissions changed.
- **Verification is independent of the responder.** It never reads the responder's output. It
  only checks that probe jobs succeed end to end and the backlog is bounded.
- **The no-op control** runs every case with a responder that does nothing. An incident counts as
  "reproduced" only if verification fails without intervention, or the case is a false alarm.
- **Superuser tools keep working during exhaustion** because Postgres reserves slots for superusers.
  Investigation must not depend on the resource that's broken.
- **Ports sit below the ephemeral range (18xxx).** An earlier choice (56xxx) collided with outgoing
  connection ports and a worker failed to bind.

## Known limitations (M1)

- Clearing an incident is a full reset, not a targeted undo.
- Per-request DB connections (no pool) keep exhaustion behaviour simple and deterministic.
  Real services usually pool.
- Telemetry is JSON logs plus JSON metrics windows, not OpenTelemetry yet.
- Actions have no authorization policy, argument schemas, or idempotency yet (M3/M4).
- The scripted baseline was written knowing the catalogue. Its high score shows the incidents are
  diagnosable from the available evidence; it says nothing about handling unseen incidents.

## Decision record: agent loop (M2), 2026-10-07

**Choice:** a plain Python loop over the OpenAI-compatible chat API (`agent/llm_agent.py`),
through the project 01 gateway, instead of Strands or Microsoft Agent Framework.

**Why**
- M3 and M4 need explicit, persisted state transitions and action IDs checked at the effect
  boundary. Owning the loop makes those guarantees easy to see and test. A framework's own
  memory and retry behaviour would have to be understood and constrained first.
- All three options make the same `/v1/chat/completions` tool-calling request to the local model.
  The risk worth testing is the *model's* tool calling, which is measured directly.
- Fewer fast-moving dependencies.

**Not done:** a hands-on trial of Strands and Agent Framework against the local endpoint. If a
later milestone needs features they provide (hosted tracing, multi-agent orchestration), revisit this.

**Safety properties of the loop (tested with a scripted fake model, `tests/test_llm_agent.py`)**
- Only read tools and `submit_diagnosis` are offered. Any other tool name is refused before it runs.
- Every argument is schema-validated before execution. Invalid calls are counted and returned to the model.
- Every read result gets an evidence ID. Diagnoses citing IDs that weren't observed are flagged.
- Step, token and wall-time budgets. A model that won't use tools gets one nudge, then stops.
- A model-server error (e.g. output budget hit mid tool call, which the server can't parse) ends
  that case with `stop_reason="model_error"` and no diagnosis; it doesn't abort the evaluation run.

## Decision (M3): the agent's state lives outside the sandbox's failure domain

**Context.** Actions and the append-only audit log first lived in an `agentstate` database on the
sandbox's PostgreSQL. In the first `llm-act` run, `db_connection_exhaustion` used up that server's
connection slots. The executor couldn't open its store, and the run crashed. The incident the
agent was meant to fix had disabled the agent's own control plane. (The read tools survived
because they deliberately use the reserved superuser slots.)

**Decision.**
- **Isolation:** the store runs on a **separate PostgreSQL instance** (`.run/agent-pg`, port
  base + 433, role `agent`). Sandbox reset and fault injection never touch it.
- **Fail closed:** any failure to reach it raises `store.AuditUnavailable`. The executor writes
  the request, authorisation and "executing" records before every effect, so no action runs
  without a record. The acting agent records the step as `blocked_no_audit`.
- **Failure after an effect:** if the store fails *after* an effect, the outcome is reported
  truthfully (`audit_final_record_missing`), never as blocked. The audit trail shows the action
  stuck in `executing`, and M4's reconciliation must repair it.

**Production equivalent.** The agent's workflow and audit store is separate infrastructure from
any system the agent can remediate: a different database cluster, account or region.

Tests: `tests/test_executor.py`. They check the store works with the sandbox database stopped, an
unreachable store blocks actions, the agent fails closed, and a failure after the effect is
reported honestly.
