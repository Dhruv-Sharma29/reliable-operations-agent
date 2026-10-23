# Reliable operations agent — Documentation

An incident-response agent for a small jobs service, run against a sandbox with injected faults:
it investigates the fired alerts with read-only tools, cites the evidence it observed, proposes one
recovery action that a deterministic policy and an approver must let through, survives being killed
mid-action without repeating the effect, and is scored by a probe that never reads its claims.

These docs are the plan and the design record. Read them in order the first time; use them as
reference afterwards. [SPEC.md](../SPEC.md) is the original brief and [PLAN.md](../PLAN.md) the
milestone plan with its progress log.

## Reading order

| # | Doc | What it settles |
|---|---|---|
| 00 | [Charter](00-charter.md) | Problem, scope, non-goals, success criteria mapped to tests and results |
| 01 | [Architecture](01-architecture.md) | Sandbox, evaluation loop, incident catalogue, the three rules, state tables, repo layout, decision records |
| 02 | [Investigation](02-investigation.md) | The read-only LLM loop: tools, evidence ledger, budgets, prompt versions, the final-answer fix |
| 03 | [Permissions and audit](03-permissions-and-audit.md) | Schema → policy → approval → append-only audit; why the store lives outside the sandbox; the injection demo |
| 04 | [Execution guarantees](04-guarantees.md) | Action ids, idempotency per action, reconciliation, crash points, what is *not* guaranteed |
| 05 | [Evaluation](05-evaluation.md) | Cases, controls, scoring, the verifier, every published number, and how much n=30 can show |
| 06 | [Roadmap](06-roadmap.md) | Milestones M1–M5 with their exit criteria and what closed them; what's next |
| 07 | [Risks](07-risks.md) | Thesis vs delivery risks, claim hygiene, the question to have an answer for |
| 08 | [Learning checklist](08-learning-checklist.md) | Skills per milestone and interview talking points mapped to what was built |
| 09 | [Implementation notes](09-implementation-notes.md) | Where the code departs from SPEC/PLAN, and why |

Hands-on: [start.md](../start.md) (offline, no daemon, no model) and [RUNBOOK.md](../RUNBOOK.md)
(the steps that need PostgreSQL running or a model server).

## The one-paragraph version

An LLM that can restart services is only as safe as the code around it. This project puts the
model in the smallest possible role — it reads, cites, and *proposes* — and moves every decision
that matters into code that can be tested: a policy file decides what may run, a different identity
must approve risky actions, every transition is written to an append-only audit log *before* the
effect, actions carry an executor-chosen id recorded in the same write as their effect so a crashed
run resumes without repeating it, and recovery is judged by probe jobs through the public API, not
by the agent. The non-trivial parts are the ones demos skip: crash points between effect and
record, a timeout that means *unknown* rather than *failed*, an incident that takes out the agent's
own control plane, and a scorer that catches the agent reporting success on a still-broken service.

## Status

**Closed 2026-10-08; all five milestones done** ([PLAN.md](../PLAN.md) § Progress). The headline
numbers, each from `results/summary/`:

- Scripted runbook **30/30**; simple LLM agent **15/30**; durable LLM agent **14/30** on the same 30
  live incidents (20 dev, 10 held-out), Qwen2.5-7B-Instruct Q4_K_M. The two agents are
  statistically indistinguishable (5 differing cases, exact McNemar p = 1.0). Durability buys crash
  safety, not accuracy (`results/summary/m5-comparison.md`).
- In 9 of 60 LLM cases an action reported success while the probe found the service still broken.
- Live crash demo: process killed after the config write, resumed with no model call, effect
  applied once, verified healthy (`results/summary/crash_demo.json`).

The LLM agent is weak at diagnosis; that is a measured negative result, not a gap in the build.
What would change it is in [06](06-roadmap.md) § Next; where the build departs from the plan is in
[09](09-implementation-notes.md).

## Conventions used in these docs

- **MUST / SHOULD / MAY** are load-bearing. MUST items are acceptance criteria.
- **[verify]** marks a claim not confirmed against a primary source or a committed artifact in
  this repository. Confirm before relying on it.
- Every number cites the file it comes from. Counts are reported as counts (`14/30`), not
  percentages, because the case sets are small ([05](05-evaluation.md) § Resolution).
- Code snippets are shapes. The authoritative definitions are `agent/policy.json`,
  `agent/tools/schemas.py`, `agent/store.py` and `agent/workflow.py`.
