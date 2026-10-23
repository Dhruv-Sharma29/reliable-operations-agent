# 09 — Implementation notes: where the code departs from the plan

SPEC.md and PLAN.md are the plan. Each entry below is a place where the build departs from them,
or where building it showed a part of the plan (or of the code's own comments) to be wrong, with the
reason. Sources are PLAN.md § Progress and § Deviations, `results/raw/superseded/README.md`,
`results/summary/m2-failure-analysis.md`, the decision records in [01](01-architecture.md), and
the code. Nothing here changes the thesis.

## Stack: deferred, not dropped

### 1. Local processes, not Docker Compose
SPEC proposes Compose. The host had no Docker, so the sandbox runs as local processes behind
`sandbox/runtime.py::Runtime` (start/stop/restart/scale/kill — what Compose would provide). Action
tools only call `Runtime`, so a container backend would not change the tool surface the agent sees.

### 2. JSON logs and metrics, not OpenTelemetry
The read tools consume JSON log files and 60 s JSON metric windows served by each component.
Enough for diagnosis; OTel deferred (PLAN.md § M1).

### 3. Faults are Python functions; "clear" is a full reset
PLAN.md's `faults/<incident>/inject.sh | clear.sh | verify.sh` became `inject` callables in
`incidents/catalog.py`, one shared verifier, and `Runtime.reset()` as clear. A full reset is
stronger than a targeted undo and guarantees cases don't contaminate each other.

### 4. No agent framework, and no trial of one
PLAN.md § M2 called for checking Strands / Microsoft Agent Framework against the local model. The
choice was made on reasoning, not a hands-on trial ([01](01-architecture.md) § Decision record).

### 5. llama.cpp, not vLLM, for every published number
PLAN.md calls llama.cpp a stopgap; the README's tool-layer section describes the schemas as "the
OpenAI function-calling format that vLLM serves". All runs used Qwen2.5-7B-Instruct Q4_K_M on
llama.cpp behind the project 01 gateway (`results/summary/m5-comparison.md`). Two llama.cpp
behaviours shaped the loop: entries 12 and 13.

### 6. The UI is a static report
`eval/report_ui.py` writes self-contained HTML (inline CSS, `<details>` only). No server, no
live view of a running incident.

## Harness corrections found by the controls

### 7. Verifier v1 was too easy to pass
Single round, probes always carried `customer`, no capacity check. The no-op control showed it
scored one-of-two-workers-hung as healthy and could not see the 2.3.1 bug, which only affects
payloads without `customer`. v2: two consecutive rounds, both payload shapes, every worker's
heartbeat fresh (`eval/verify.py` docstring). The v1 run was deleted before completion.

### 8. The connection leak didn't reliably leak, and one held-out variant wasn't an incident
The exporter stopped leaking at its first refused connection, so the amount depended on what other
clients held at that instant. It now keeps taking freed slots until `keep_free` remain. Separately,
held-out `{workers: 2, keep_free: 2}` produced no alert and a healthy verifier; replaced with
`{workers: 3, keep_free: 0}` and the held-out split re-run (`results/raw/superseded/README.md`).

### 9. The bad deploy never alerted
About 10 % of requests failed, at the generic `ApiErrorRateHigh` threshold. Added
`Api5xxRateHigh` (> 2 % 5xx, at least 3) in `ops/alerts.py`.

### 10. The backlog healed itself
Found in M2: a one-off burst of ~2,400 jobs that one worker drains in ~100 s, against ~90 s of LLM
latency. Confirmed with `noop-wait`; replaced by burst + sustained import at ~1.5× one worker. Older
runs keep their files; their backlog rows are superseded by `*-sustained.jsonl`, and M2 v1-vs-v2
comparisons on that incident are confounded by the change.

## Agent-loop bugs

### 11. A truncated tool call crashed the whole run
The model hit its 400-token budget inside `submit_diagnosis`; llama.cpp returned 500, the gateway
502, and the loop raised. Fixed twice: a model error now ends only that case
(`stop_reason="model_error"`), and `unknowns` is capped at 3 × 160 characters (v1 allowed 10 × 300,
≈ 750 tokens, more than the budget).

### 12. `tool_choice="required"` is not enforced by the llama.cpp build used
Told not to call a tool, the model answered in text 3/3 times with `required` set; two v2 held-out
answers and one M3 dev answer came back as prose. An earlier code comment claimed grammar
enforcement; it was wrong and was corrected. Fix: the final answer is requested under
`response_format` = the diagnosis schema with its own 350-token budget (v2s, `AGENT_FINAL=schema`).

### 13. A cut-off reply poisoned the conversation
In the first schema-mode run, a reply truncated mid tool call was appended to the history; llama.cpp
then rejected every later request *before generating* (HTTP 500 in 8–25 ms). Fix: a reply with
`finish_reason == "length"` is never appended. That run was stopped at 19/20 and re-run.

### 14. v1 runs did not record tool results
So the v1 failure analysis could confirm the decisive tool was *called* but not quote what it
returned. v2 records `evidence_log`, which is what exposed the "claims a check it never made"
failure class.

## Design corrections

### 15. Ports moved below the ephemeral range
An earlier 56xxx base collided with outgoing connection ports and a worker failed to bind. Now base
18000 (`SANDBOX_PORT_BASE`); tests use 17000 and their own run directory (`tests/conftest.py`).

### 16. The audit store left the sandbox's database
First `llm-act` run: the store lived on the sandbox PostgreSQL, the exhaustion incident used its
slots, the executor couldn't open its store, and the run aborted at 6/20. Now a separate instance
that fails closed ([03](03-permissions-and-audit.md)). The acting evaluation was re-run from scratch.

### 17. `AUTHORIZED` is an action state, not a workflow state
PLAN.md § M4 lists it in the incident workflow. Authorization is per action, so it lives in the
executor; the workflow instead gained `AWAITING_APPROVAL` for the per-incident wait.

### 18. The durable responder's `VERIFYING` is stubbed in evaluation
The runner verifies with its own probe, so `eval/run.py::_durable` passes a stub that always says
healthy. The workflow's `RESOLVED` in M5 records is therefore not a health verdict; no published
number uses it ([04](04-guarantees.md) § Where the workflow's own verdict is not used).

### 19. Audit events in `_execute` and `_reconcile` are not in the same transaction as the state update
Found while writing these docs, by reading the code; not covered by a test. `agent/executor.py`'s
docstring says every transition is written to the audit log "in the same transaction". That holds
in `request`, `approve` and `deny` (`conn.transaction()`), but the store connects with
`autocommit=True`, and `_execute`/`_reconcile` use `with store.connect() as conn:` without a
transaction, so the `UPDATE actions` and the `INSERT audit_events` commit separately. A crash
between the two leaves the action row ahead of its audit trail. Safety is unaffected — resume reads
`actions.state`, which is committed before the effect — but the audit trail can miss one event.
Fix: wrap those blocks in `conn.transaction()`. Not changed here (docs-only pass).

## Honest downgrades

### 20. Approval in evaluation is a stand-in
`eval-approver` approves everything approval-required so the agent's choices can be scored. The
injection demo is the only run with no approver.

### 21. The mutation check is not reproducible from the repository
PLAN.md and the README say the durability tests fail with idempotency removed. No script, patch or
log for that check is committed **[verify]**.

### 22. Injection evidence is one run per prompt version
See [03](03-permissions-and-audit.md) § Injection demo. The claim is about the controls.

### 23. macOS support is untested on a Mac
The macOS code paths (`pg_config` lookup, `ps` instead of `/proc`) are tested on Linux only. The
README's macOS block says `brew install postgresql@17`; `scripts/setup_postgres.sh` prints
`brew install postgresql@16` when `pg_config` is missing **[verify]** which one works.

### 24. Doc paths in code comments follow the renumbering
`agent/llm_agent.py`, `agent/tools/act.py` and `results/summary/m3-acting.md` referred to
`docs/architecture.md` and `docs/guarantees.md`. On 2026-10-23 those references were changed to
[01-architecture.md](01-architecture.md) and [04-guarantees.md](04-guarantees.md) (comment and
path text only; no code or result changed).

## Deferred, not dropped (behaviour)

### 25. No supervisor runs `workflow.resume_all()`
Crash recovery works, but needs a manual resume. A supervisor would call it at start-up.

### 26. One remediation step per incident
The action id is `uuid5(incident, "remediation")`. Multi-step plans need one id per step.

### 27. No cloud deployment
SPEC § GX10 and deployment suggests an AWS or Azure deployment with narrow permissions and teardown
instructions. Not started.
