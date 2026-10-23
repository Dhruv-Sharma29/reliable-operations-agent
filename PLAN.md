# 02 — Reliable operations agent: execution plan

Brief: [SPEC.md](SPEC.md). The value is **correct, auditable, non-repeating actions**, not the number of agents.

**Needs:** endpoint from 01 (only from M2 on). **Produces:** sandbox tools + tool schemas + evaluation cases reused by 04; telemetry shape reused by 05.

## Target layout

```text
02-reliable-operations-agent/
  target-app/          the system that breaks: api/, worker/, postgres, OTel collector
  faults/              one script per incident type: inject.sh / clear.sh / verify.sh
  runbooks/            human-readable runbooks + scripted deterministic baseline
  agent/
    tools/read/        health, logs, metrics, runbook lookup (read-only)
    tools/act/         allowlisted sandbox actions with idempotency keys
    schemas/           JSON schemas for every tool argument
    policy/            authorization: allowlist, approval-required list
    workflow/          explicit state machine + persistence (Postgres)
  ui/                  incident report: evidence, trace, action, uncertainty
  eval/                incident cases (dev / held-out), scorer, health verifier
  results/raw/  results/summary/  docs/
```

## Progress (2026-10-08) — complete; definition of done met

- **M1 done.** Sandbox app (API, worker, Postgres, payments dependency, traffic, exporter) as local
  processes behind a `Runtime` interface (Compose later); 10 incidents × dev/held-out variants;
  independent verifier; no-op control; scripted baseline 20/20 dev and 10/10 held-out (see README).
- **M2 done (exit met; accuracy is low).** Plain-Python loop over the 01 gateway (Qwen2.5-7B Q4_K_M,
  llama.cpp stopgap). Diagnosis correct **7/20 dev, 5/10 held-out** (baseline 20/20, 10/10); citations
  valid in every submitted diagnosis. Failure analysis: `results/summary/m2-failure-analysis.md`.
  Main cause: the model never calls `config_history`/`deploy_history`, and misreads evidence it has.
- **M3 done.** Policy, executor with approval, append-only audit store on its own PostgreSQL
  (fail closed), acting agent.
  - **Results:** success **9/20 dev, 5/10 held-out**; healthy after (verified) 12/20 and 6/10.
    Details: `results/summary/m3-acting.md`.
  - **Injection demo passed with both agents:** v1 was misled and approval stopped it; v2 wasn't
    misled.
- **M5 done.** Scripted 30/30, simple agent 15/30, durable agent 14/30 (no significant difference;
  durability buys crash safety, not accuracy). Schema-enforced final answer: 0 of 30 diagnoses lost.
- **M4 done.** Live crash demo with the real model passed (`results/summary/crash_demo.json`). Idempotent actions keyed by an
  executor-chosen action id recorded with the effect, a persisted incident workflow that resumes
  without re-asking the model, timeout reconciliation, and `docs/guarantees.md`.
  - **Proof:** 8 durability tests kill real processes between effect and record. A mutation check
    confirmed they fail when idempotency is removed.
- **M2 v2 (prompt tuned on dev):** 12/20 dev, 5/10 held-out. **No demonstrated improvement**
  (McNemar p = 0.33); see the failure analysis.
- **Deviations:** no Docker on the host, so processes instead of Compose; JSON logs/metrics
  instead of OpenTelemetry for M1; "clear" is a full reset.

## Milestones

### M1 — Target app + deterministic baseline (1–2 weeks, **no LLM — can start alongside 01**)
- [x] Small app: FastAPI API → Postgres-backed job queue → worker. (JSON logs + metrics; OpenTelemetry deferred.)
- [x] Catalogue ~10 incident types. Starting list (refine): worker crashed; worker hung; bad config value; DB connection exhaustion; queue backlog; slow dependency; disk/log fill (sandboxed); bad deploy (version flag); expired sandbox credential; **healthy system with misleading alert**.
- [x] Each incident: `inject`, `clear`, and an **independent** `verify_healthy` check (not the agent's opinion).
- [x] Scripted runbook baseline that diagnoses + fixes each from deterministic rules.
- **Exit:** every incident reproduces on demand and the scripted baseline's results are recorded.

### M2 — Read-only agent (1 week)
- [x] Decision gate: plain Python loop chosen (docs/architecture.md). A hands-on trial of Strands / Agent Framework was **not** done; the decision record says why.
- [x] Agent gets read tools only. Output: structured diagnosis `{incident_type, evidence: [ref], confidence, unknowns}`.
- [x] Scorer compares diagnosis to ground truth; evidence refs must point to real log/metric entries.
- [x] Prompt v2 / submit budget fix, tuned on dev only, then one held-out run (no demonstrated gain).
- **Exit:** diagnoses scored automatically on dev incidents.

### M3 — Permitted execution (1 week)
- [x] Every action argument validated against its JSON schema **before** policy check.
- [x] Policy: allowlist (auto) / approval-required / forbidden. Policy reads only from config + authenticated context, never from model output or log text.
- [x] Audit record per action: action_id, incident_id, tool, args, policy decision, requester, timestamps, result.
- **Exit:** out-of-allowlist action blocks until approved; prompt-injection log line cannot widen permissions (test). Met: unit tests plus `eval/injection_demo.py`.
- [x] `llm-act` evaluation (dev + held-out), scored by the independent verifier.

### M4 — Durability (1–2 weeks) — the core of the project
Explicit states: `OPEN → INVESTIGATING → DIAGNOSED → PROPOSED → AUTHORIZED → EXECUTING → VERIFYING → RESOLVED | ESCALATED`.
Built as `agent/workflow.py`. AUTHORIZED lives in the executor's action states; the workflow adds `AWAITING_APPROVAL`.
- [x] Persist state transition + `action_id` **before** executing an effect.
- [x] Idempotency at the effect boundary: the action tool records `action_id` in the target system (or checks observable state) so re-execution is a no-op.
- [x] Ambiguous completion (timeout): reconcile by reading actual service state before any retry.
- [x] Write `docs/guarantees.md`: which actions are naturally idempotent, which are made idempotent, what remains at-least-once.

Tests:
- [x] Kill workflow after action succeeds, before result recorded → resume does not repeat effect.
- [x] Tool times out after effect happened → reconciliation detects it, no second effect.
- [x] Prompt-injection in logs → no new tool permission (M3 tests + live demo).
- [x] Insufficient evidence → `ESCALATED`, no action.
- [x] Healthy system + misleading alert → any action counted as a failure (scorer test).

### M5 — Evaluation + UI (1–2 weeks)
- [x] ~100 incident runs; dev vs **held-out** scenarios split before tuning prompts (120 in the final round: 4 responders × 30).
- [x] Compare: scripted runbook vs simple prompted agent vs durable agent (`results/summary/m5-comparison.md`).
- [x] Metrics: correct diagnosis, verified recovery (by `verify_healthy`, not model claim), unnecessary actions, model calls, time to recovery.
- [x] UI: evidence, tool trace, action result, remaining uncertainty (static report, `eval/report_ui.py`).
- [x] Repeatable demo: break → diagnose → authorized fix → verified health → injected interruption survived (`eval/crash_demo.py`).

## Risks / open questions
- Small local models may be weak at multi-step tool use — that's a finding, and motivates project 04.
- Agent-framework APIs move fast; pin versions and record them.
- Keep one model loaded; target app containers must stay light (memory is shared).

## Definition of done
- [x] Demo end-to-end with interruption (`results/summary/crash_demo.json`: effect once, resumed without the model, verified healthy)
- [x] 3-way comparison on held-out cases (`results/summary/m5-comparison.md`)
- [x] Failed scenarios published (per-case evidence in `results/ui/m5_report.html`; `results/summary/m2-failure-analysis.md`; superseded runs kept in `results/raw/superseded/`)
- [x] Audit trace sample (`results/summary/audit_trace_sample.json`, from `eval/audit_sample.py`: every executor path plus append-only check)
- [x] `docs/guarantees.md`

**Closed 2026-10-08.**
