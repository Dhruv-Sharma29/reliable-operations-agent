# 06 — Roadmap

The plan is [PLAN.md](../PLAN.md); this doc records what closed each milestone and what is left.
All five milestones closed between 2026-10-06 and 2026-10-08 (dates from the run ids in
`results/raw/`).

## What changed from the plan, and why

| Change | Reason |
|---|---|
| Local processes behind a `Runtime` interface, not Docker Compose | No Docker on the host; action tools only call `Runtime`, so a Compose backend would not change the agent's tool surface ([01](01-architecture.md) § Design decisions) |
| JSON logs + 60 s JSON metric windows, not OpenTelemetry | Enough for the read tools; OTel deferred ([09](09-implementation-notes.md) § 2) |
| Plain Python loop, not Strands / Microsoft Agent Framework | Guarantees are easier to prove in code the project owns; the framework trial was **not** done ([01](01-architecture.md) § Decision record) |
| Audit store moved to its own PostgreSQL in M3 | An incident disabled the agent's control plane ([03](03-permissions-and-audit.md)) |
| `AUTHORIZED` lives in the executor's action states; the workflow adds `AWAITING_APPROVAL` | Authorization is per action, waiting is per incident (PLAN.md § M4) |
| A `noop-wait` control and a sustained backlog were added in M2 | The original backlog healed itself in the time the LLM took ([05](05-evaluation.md) § What the controls caught) |

---

## M1 — Target app and deterministic baseline

**Done when** every incident reproduces on demand and the scripted baseline's results are recorded.

- [x] FastAPI API → PostgreSQL queue → workers, plus payments, exporter, bulk import, load generator.
- [x] 10 incident types, each with inject, clear (full reset) and an independent verify.
- [x] Scripted baseline: 20/20 dev, 10/10 held-out; no-op control 2/20 and 1/10, the false alarms
      only (`results/summary/m1.md`).

## M2 — Read-only agent

**Done when** diagnoses are scored automatically on dev incidents.

- [x] Loop over the project 01 gateway; structured diagnosis with evidence ids; scorer checks citations.
- [x] Results v1 7/20 and 5/10; v2 (dev-tuned) 12/20 and 5/10, no demonstrated gain (McNemar
      p = 0.33); failure analysis published (`results/summary/m2-failure-analysis.md`).
- [x] Schema-enforced final answer (v2s): 0 of 30 diagnoses lost.

## M3 — Permitted execution

**Done when** an out-of-allowlist action blocks until approved and a prompt-injection log line
cannot widen permissions.

- [x] Schema before policy; policy from file only; approval by a different identity; append-only audit.
- [x] Unit tests (`tests/test_policy.py`, `tests/test_executor.py`) and the live injection demo, passed
      with v1 and v2 (`results/summary/injection_demo*.json`).
- [x] `llm-act` evaluation: 9/20 dev, 5/10 held-out (`results/summary/m3-acting.md`).

## M4 — Durability (the core)

**Done when** retries after interruption do not repeat the effect.

- [x] State and action id persisted before the effect; id recorded with the effect; timeout reconciliation.
- [x] 8 crash/timeout tests in `tests/test_durability.py`; live crash demo with the real model
      (`results/summary/crash_demo.json`).
- [x] [04-guarantees.md](04-guarantees.md): naturally idempotent vs made idempotent vs at-least-once.

## M5 — Evaluation and UI

**Done when** the three-way comparison, failed scenarios, audit sample and demo are published.

- [x] Scripted 30/30, simple 15/30, durable 14/30 (`results/summary/m5-comparison.md`).
- [x] Static report `results/ui/m5_report.html` (`eval/report_ui.py`): evidence, tool trace, action, uncertainty.
- [x] Audit trace sample covering every executor path (`results/summary/audit_trace_sample.json`).
- [x] Repeatable demo `python -m eval.crash_demo`.

SPEC asked for "approximately 100 incident runs"; `results/raw/` holds 252 case records in 24
non-superseded files. PLAN.md counts the final round as "120 (4 responders × 30)"; the M5 table
itself compares 3 responders × 30 = 90 records.

---

## Next (not done)

Ordered by how much each would change the results. None of these is started.

| Item | Why | Evidence it matters |
|---|---|---|
| A larger model via the host vLLM path | Most remaining errors are misreading evidence the 7B model already fetched | `m2-failure-analysis.md` group B |
| Forgiving log search: report unfiltered counts when a `contains` filter matches nothing | v2 searched for `"507 spool quota exceeded"`, got nothing, and called a disk fill a false alarm | `m2-failure-analysis.md` § Lost |
| Claim check: flag diagnoses whose prose names a tool never called | Seen in `dev-worker_crashed-1`; citation checks can't catch it | `m2-failure-analysis.md` § New failure classes |
| Action-target check | One restart hit the wrong worker | `m3-acting.md` |
| ~~A supervisor calling `workflow.resume_all()` at start-up~~ | Done 2026-10-23: `python -m agent.supervisor` | [04](04-guarantees.md) § What is NOT guaranteed |
| More cases | At 30 per responder only large differences are detectable | [05](05-evaluation.md) § Resolution |
| Docker Compose backend, OpenTelemetry, cloud deployment with teardown | SPEC items deferred for host reasons | SPEC § Proposed stack, § GX10 and deployment |
| Hands-on Strands / Agent Framework trial | The decision record chose without one | [01](01-architecture.md) § Decision record |
| Run on a real Mac | macOS code paths are tested only on Linux | README § macOS |
