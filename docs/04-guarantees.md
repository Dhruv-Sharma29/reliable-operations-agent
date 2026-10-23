# 04 — Execution guarantees

What the agent guarantees about its actions when processes crash, calls time out, or work is
retried. Retries alone never give exactly-once effects. Each guarantee below comes from a specific
mechanism, and each has a test in `tests/test_durability.py` or `tests/test_executor.py`.

This is the core of the project (PLAN.md § M4). The executor it builds on is in
[03](03-permissions-and-audit.md); the workflow states are tabulated in [01](01-architecture.md) § State tables.

## The short version

- **No action runs without an audit record.** Request, decision, approval and "executing" are
  committed before the effect. If the audit store is unreachable, nothing runs (fail closed).
- **An interrupted action is never repeated blindly.** Its id is fixed before execution. On resume,
  the executor reads the target system first (reconciliation), and the action itself ignores a
  repeat under the same id.
- **A finished model decision is never re-asked.** Diagnosis and proposal are persisted, so
  resuming an incident doesn't call the model for steps it already completed.
- **The effect is exactly-once; the record is at-least-once.** If the process dies after an
  effect, the effect is not repeated, but its final record is written later by reconciliation.

## Where the action id comes from

`workflow.action_id_for(incident_id)` is `uuid5(incident, "remediation")`. It's derived, not
random, and persisted at `PROPOSED`, before anything executes. The model never sees or chooses it:
it isn't in any tool schema, and a test enforces that.

## Per action

| Action | Repeat without protection | How it's made safe | Reconciliation can observe it? |
| --- | --- | --- | --- |
| `rollback_config` | **Harmful**: rolls back to `history[-2]`, the bad config | action id written in the same config-history line as the change; a repeat only finishes missing restarts | yes (config history) |
| `rollback_release` | **Harmful**: "rolls back" to the bad release | action id in the same deploy-history line | yes (deploy history) |
| `rotate_credential` | Issues a second token and invalidates the first | action id sent as the provider's **idempotency key**; the provider returns the same token | yes (secret metadata) |
| `restart_service` | Extra restart = extra downtime | action id recorded in the pid file of the process it started; a repeat skips instances it already restarted | yes (pid file) |
| `escalate` | Duplicate notification | escalation log deduplicated by action id | yes (escalation log) |
| `scale_service` | Harmless | **naturally idempotent**: sets an absolute replica count | no (re-run is safe) |
| `stop_service` | Harmless | **naturally idempotent** | no (re-run is safe) |
| `cleanup_spool` | Harmless | **naturally idempotent**: deletes `*.tmp` | no (re-run is safe) |

"Same line as the change" matters. The effect and the proof of who made it are one append, so no
crash can separate them. The secret file and its metadata are two writes. A crash between them
makes reconciliation miss the rotation, so the action re-runs, and the provider's idempotency key
returns the same token, so the outcome is still one rotation.

## Ambiguous completion

A timeout from an action (`TimeoutError`, `httpx.TimeoutException`) means *unknown*, not
*failed*:
1. The action is recorded as `unknown`.
2. The executor asks the target system whether the effect happened (`act.observe`).
3. Observed → `succeeded` (audited as `reconciling`, then `succeeded`).
4. Not observed → re-run under the same id, which is safe by the table above.
5. Still ambiguous after one reconciliation → stays `unknown` for a human.

## Crash points (kill -9 equivalent, `os._exit`)

| Crash after… | On resume |
| --- | --- |
| INVESTIGATING (reads in progress) | investigation re-runs; read tools have no side effects |
| DIAGNOSED / PROPOSED committed | continues from the stored diagnosis or proposal; **no model call** |
| the action's effect, before its result is recorded | action found in `executing` → reconciled → `succeeded`; effect not repeated |
| AWAITING_APPROVAL | waits; when a different identity approves, `run()` continues |

## What is NOT guaranteed

- **Effects outside these tools.** If someone fixes the system by hand while an action is
  `unknown`, reconciliation can't tell who did it. Naturally idempotent actions just re-run.
- **Restart idempotency depends on the pid file.** If the instance was restarted by something else
  after this action, a repeat restarts it again. That's at most one extra restart, never a wrong
  state.
- **The audit record after a crash is delayed, not lost.** Until resume runs, the action shows
  `executing`. Nothing runs `resume_all()` automatically yet; a supervisor would call it at start-up.
- **One incident, one remediation.** The workflow proposes a single action per incident (fixed
  step name `remediation`). Multi-step plans would need one id per step.
- **The agent's store is a single PostgreSQL instance.** It's isolated from the sandbox, but not
  replicated. Losing its disk loses the audit trail.

## Where each guarantee is tested

The durability tests kill real processes with `os._exit` (no clean-up) at the named point and count
the effect **in the target system** — config-history lines, deploy records, secret metadata, pid
files — not in the agent's own records (`tests/test_durability.py`, helpers in `tests/crash_helpers.py`).

| Guarantee | Test |
|---|---|
| Repeating a rollback *without* an id reintroduces the fault (the hazard is real) | `test_repeating_a_rollback_without_an_action_id_reintroduces_the_fault` |
| Repeating it *with* its id is a no-op | `test_repeating_a_rollback_with_its_action_id_is_a_no_op` |
| Release rollback and credential rotation are idempotent | `test_rollback_release_and_rotation_are_idempotent` |
| Timeout after the effect → reconciled, no second effect | `test_timeout_after_the_effect_is_reconciled_without_a_second_effect` |
| Crash after effect, before record → resume does not repeat it | `test_crash_after_effect_then_resume_does_not_repeat_it` |
| Crash after proposal → resume does not call the model | `test_crash_after_proposal_resumes_without_asking_the_model_again` |
| Approval wait survives a restart and resumes once a human approves | `test_approval_wait_survives_and_resumes_after_a_human_approves` |
| Insufficient evidence → `ESCALATED`, no action | `test_insufficient_evidence_escalates_without_any_action` |
| No action without an audit record (fail closed) | `tests/test_executor.py::test_unreachable_audit_store_blocks_the_action` |
| The model never sees or chooses the action id | `tests/test_tools.py::test_schemas_match_function_signatures` |

PLAN.md and the README report a mutation check: with idempotency removed, these tests fail. No
script or log for it is committed, so it cannot be re-run from the repository as it stands **[verify]**.

## The live crash demo

`python -m eval.crash_demo` (needs the model and PostgreSQL, [RUNBOOK](../RUNBOOK.md) § 6) runs
the same crash point with the real model on a real bad-config incident
(`results/summary/crash_demo.json`):

| Step | Recorded |
|---|---|
| Diagnosis, proposal | `bad_config`, `rollback_config`, action id `c86cfc68…` |
| Child killed after the effect | exit 137, state `EXECUTING`, audit stops at `executing` |
| Effects after the crash | 1 config record |
| Resume | `reconciling → succeeded`, **0 model calls** (the resume client raises if called), 1.2 s |
| Effects after the resume | still 1 config record; verifier healthy, "probes completed at full capacity" |
| History | `OPEN → INVESTIGATING → DIAGNOSED → PROPOSED → EXECUTING → VERIFYING → RESOLVED` |

One run. If the model had misdiagnosed and escalated, the crash point would never be reached and
the demo reports "inconclusive" rather than a pass. The tests carry the general claim.

## Where the workflow's own verdict is not used

In evaluation the runner verifies health itself, so the durable responder is given a stub for
`VERIFYING`. Its `RESOLVED` state in the M5 records is therefore **not** a health verdict
(`dev-queue_backlog-0` is `RESOLVED` while the probe says unhealthy); every published number uses
the runner's probe (`results/summary/m5-comparison.md` § Durable agent in evaluation).
