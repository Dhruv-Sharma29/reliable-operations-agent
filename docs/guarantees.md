# Execution guarantees

What the agent guarantees about its actions when processes crash, calls time out, or work is
retried. Retries alone never give exactly-once effects. Each guarantee below comes from a specific
mechanism, and each has a test in `tests/test_durability.py` or `tests/test_executor.py`.

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
