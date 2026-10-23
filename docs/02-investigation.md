# 02 — Investigation: the read-only loop and the propose step

The model's whole job, in two call sites: `agent/llm_agent.py::investigate` (read, cite,
diagnose) and `agent/acting_agent.py::propose` (pick one next step). Neither can execute an action.
Why it is a plain loop rather than a framework: [01](01-architecture.md) § Decision record.

## Inputs and outputs

**In:** the fired alerts as JSON, nothing else — no incident name, no hint
(`eval/run.py::run_case`).

**Out:** `submit_diagnosis{incident_type, evidence, confidence, unknowns}`, validated against
`agent/tools/schemas.py`. `incident_type` is an enum of the ten incidents plus `unknown`;
`evidence` is 1–20 ids matching `^ev-[0-9]+$`; `unknowns` is at most 3 × 160 characters (the cap
exists because v1's 10 × 300 overflowed the output budget, [09](09-implementation-notes.md) § 11).

## Read tools

| Tool | Returns | Decides (per `runbooks/baseline.py`) |
|---|---|---|
| `service_status` | process state and `/health` of every instance | crashed vs hung worker |
| `api_metrics` | requests by status and route, p95, last 60 s | 4xx-only false alarm; 5xx on one route |
| `worker_metrics` | payments latency, status codes, job totals, last error | slow dependency |
| `queue_stats` | pending, oldest age, done/failed, p95 duration | backlog |
| `worker_heartbeats` | seconds since each worker's loop last wrote | hung worker |
| `db_connections` | open connections by application and state | exporter leak |
| `search_logs` | recent JSON log records of one instance, optional filter | `connection refused`, `token_expired`, `spool quota exceeded` |
| `config_history` | recent config versions and changed keys | bad config |
| `deploy_history` | recent releases | bad deploy |
| `runbook` | sections of `runbooks/RUNBOOKS.md` | — (it *is* the baseline's rules, written for humans) |

Read tools use PostgreSQL's reserved superuser slots, so investigation still works while the
incident has exhausted the application's connections (`tests/test_sandbox.py::test_investigation_tools_still_work_when_app_connections_are_exhausted`).

## The evidence ledger

`agent/evidence.py::EvidenceLedger.call` validates the arguments, runs the read tool, stores the
full result and returns `{evidence_id: "ev-N", result}`. A diagnosis is `citations_valid` only if it
cites at least one id and every cited id exists in this run's ledger. That turns "the model says it
saw X" into "the model points at entry ev-4, which the scorer can read".

What it cannot catch, measured in v2: a summary that names a tool never called, or contradicts its
own answer, while citing real ids (`results/summary/m2-failure-analysis.md` § New failure classes).

## Loop mechanics

| Budget / rule | Value | Where |
|---|---|---|
| Steps | 10 model calls, then finalise | `MAX_STEPS` |
| Output tokens per call | 400 | `MAX_TOKENS` |
| Tool result passed back | 1,800 characters, then `[truncated]` | `MAX_RESULT_CHARS` |
| Wall time | 900 s, then finalise | `TIME_BUDGET_S` |
| Final answer budget | 350 tokens, schema mode | `FINAL_MAX_TOKENS` |
| Temperature | 0 | `_call_model` |

- **Tool filter before execution.** In read-only mode only read tools and `submit_diagnosis` exist;
  any other name — e.g. an action recalled from the runbook — is refused and counted as invalid.
  During finalisation only `submit_diagnosis` is accepted.
- **Schema check before execution.** Invalid arguments are returned to the model as an error and
  counted; the loop continues (`tests/test_llm_agent.py::test_invalid_arguments_are_rejected_and_the_loop_continues`).
- **Finalisation.** A prose reply, a budget hit, a server error, or a reply cut off at
  `max_tokens` triggers it. In the default `schema` mode the final answer is requested with
  `response_format` = the diagnosis JSON Schema, no tools, and its own budget. The older `tool`
  mode relies on `tool_choice="required"`, which the llama.cpp build used does not enforce.
- **Truncated replies are dropped, never appended.** A cut-off partial tool call in the history
  made llama.cpp reject every later request before generating
  (`tests/test_llm_agent.py::test_reply_cut_off_at_max_tokens_is_not_added_to_the_history`).
- **A model-server error ends the case, not the run** (`stop_reason="model_error"`), after one
  schema-mode attempt on the evidence gathered so far.
- **Everything is recorded** for failure analysis: tool calls, trimmed tool results
  (`evidence_log`), the model's own words (`transcript`), tokens, prompt version, final mode.

Environment: `AGENT_LLM_URL` (default `http://127.0.0.1:21091`, the project 01 gateway),
`AGENT_LLM_MODEL` (default `qwen2.5-7b-instruct`), `AGENT_PROMPT` (`v1` | `v2`, default `v2`),
`AGENT_FINAL` (`schema` | `tool`, default `schema`).

## Prompt versions

| Version | What changed | Dev | Held-out | Source |
|---|---|---|---|---|
| v1 | generic on-call prompt; "usually 3-6 tool calls are enough" | 7/20 | 5/10 | `results/summary/m2-dev.md`, `m2-heldout.md` |
| v2 | read the runbook for each alert, check what changed, read the affected component's logs; `unknowns` capped | 12/20 | 5/10 | `m2-dev-v2.md`, `m2-heldout-v2.md` |
| v2s | v2 + schema-enforced final answer + drop truncated replies | 11/20 | 6/10 | `m2-failure-analysis.md` § v2s |

v2 is **not a demonstrated improvement**: 11 cases went wrong → right and 6 right → wrong over the
30 paired cases (exact McNemar p = 0.33), and the held-out score did not move. It shifted the
model's attention rather than widening it — `runbook` calls on dev went 0 → 20, `service_status`
5 → 0, `db_connections` 17 → 4 (`m2-failure-analysis.md`). v2s fixed what it targeted: **0 of 30
diagnoses lost** to output format, versus 2 per 30 before. All three versions are scored by the
read-only runner; the prompt is the same on dev and held-out within a version.

Note on v2: the runbook is the scripted baseline's rules written for humans, so v2 measures "can it
follow a runbook against evidence", not "diagnose from scratch" (`m2-failure-analysis.md`).

## The propose step

`propose()` makes **one** model call with the diagnosis, confidence, unknowns and the *cited*
evidence (each result trimmed to 600 characters). It offers the eight action tools plus
`no_action{reason}` with `tool_choice="required"`, under this instruction (`REMEDIATE`): fix the
diagnosed cause, or `escalate` if it is outside your control or the evidence is insufficient, or
`no_action` if nothing is broken; choose the least disruptive fix.

It returns `{tool, args, state}` and executes nothing. What happens next depends on the caller:

| Caller | On `unknown` / invalid citations | On a proposed action |
|---|---|---|
| Simple agent (`acting_agent.remediate`, responder `llm-act`) | still asks `propose()` | `executor.request()` once, random action id |
| Durable agent (`workflow.run`, responder `durable`) | `ESCALATED`, no model call | persist proposal + `uuid5` action id, then `executor.request()` |

That one difference is why the durable agent escalated 9 of 30 cases against the simple agent's
5 (counted from the four M5 raw files listed in `results/summary/m5-comparison.md` § Runs).

## What the model is never given

The action id, the requesting identity, the incident id, the approver, or the policy. Its arguments
are validated against enums that encode scope: `restart_service.service ∈ {api, worker,
report-exporter}`, `instance` matches `^worker-[1-4]$`, `scale_service.replicas ≤ 4`,
`cleanup_spool.pattern = "*.tmp"`, `rotate_credential.name = "payments"`. The external payments
service is in no action enum. The action functions refuse out-of-scope targets again, independently
of the schemas (`tests/test_units.py::test_actions_refuse_out_of_scope_targets_without_side_effects`).
