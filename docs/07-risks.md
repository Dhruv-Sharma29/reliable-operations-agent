# 07 — Risks and failure modes

Two kinds: **thesis risks** (the system runs but does not show what it claims) and **delivery
risks** (it fails in operation or cannot be reproduced). The first kind is more dangerous, because
the tables look complete either way.

## Thesis risks

### R1 — The controls are only tested because the model is weak *(certain, moderate impact)*

With a 7B model the policy, approval and verifier do a lot of visible work. A stronger model would
make fewer wrong proposals, and most of the safety story would go untested in evaluation.

**Detection:** the claimed-vs-verified column (9 of 60 LLM cases, `results/summary/m5-comparison.md`)
and the count of wrong approval-required actions (3 of 12, `results/summary/m3-acting.md`).

**Mitigation:** the guarantees do not depend on the model being wrong. They are tested with
scripted fakes and process kills ([04](04-guarantees.md)), and the generated audit paths
(forbidden, invalid, denied) are exercised without any model ([03](03-permissions-and-audit.md)
§ Audit trace sample).

### R2 — A self-healing incident scores as a recovery *(happened once, fatal if missed)*

The original backlog drained by itself in about the time the LLM took to respond.

**Detection:** the `noop-wait` control. **Mitigation:** sustained import above one worker's
capacity; re-run of every backlog case; superseded rows documented (`results/raw/superseded/README.md`).
In the 27 slow non-acting cases, no other incident type recovered on its own (README § Results M1).

### R3 — The scripted baseline makes the LLM look worse than it is *(certain, it's by design)*

The baseline's rules come from the catalogue; 30/30 is a ceiling, not a peer. The v2 prompt also
hands the LLM those same rules through the `runbook` tool.

**Mitigation:** both are stated wherever the numbers appear. The comparison that matters for the
durability claim is simple vs durable agent, not agent vs script ([05](05-evaluation.md) § Reading it).

### R4 — Injection resistance is anecdotal *(high likelihood of overclaiming)*

One run per prompt version. v1 was misled; v2 was not.

**Mitigation:** the claim is about the *controls*, not the model: an injected action is
approval-required regardless of what the logs say, and unknown fields are schema-rejected before
policy. Never claim the model resists injection.

### R5 — Tuning on dev overfits *(medium, observed)*

v2 gained 5 dev cases and 0 held-out (McNemar p = 0.33, `m2-failure-analysis.md`).

**Mitigation:** dev-only tuning, one held-out run per frozen version, and the gain reported as
"not demonstrated".

### R6 — The stand-in approver approves everything *(certain in evaluation)*

`eval-approver` turns the approval gate into a recording point; the acting results include actions
a human might have refused.

**Mitigation:** results count how many actions needed approval and how many of those were wrong;
the injection demo runs with **no** approver, which is the case the gate exists for.

### R7 — Identity and append-only are local-sandbox strength *(medium if over-read)*

Requester and approver are strings. The `agent` role no longer owns the audit tables and can't drop
or disable their triggers (2026-10-23). Since 2026-10-26 the store requires a password for every role,
and an agent running as its own OS user (`AGENT_OS_UID`) can't read the superuser's
([03](03-permissions-and-audit.md) § Password auth). `sandbox/deploy.py` runs the sandbox and the agent
as that user end to end; all 30 incidents succeed that way ([03](03-permissions-and-audit.md) § A
separate agent user). In the default single-user sandbox the agent can still read both passwords,
and root on the host can do anything.

**Mitigation:** the triggers protect against the executor's own bugs, and that is what is claimed.
A deployment needs authenticated principals and a writer role without DDL rights
([03](03-permissions-and-audit.md) § The audit store).

## Delivery risks

### R8 — A crash leaves an action in `executing` until the supervisor runs *(low)*

`python -m agent.supervisor` calls `workflow.resume_all()` at start-up (2026-10-23). **Remaining:**
something has to start it (a service manager or the next deploy); there is no watchdog in this repo.

### R9 — Single-instance agent store *(low in the sandbox)*

Isolated from the sandbox but not replicated; losing its disk loses the audit trail
([04](04-guarantees.md) § What is NOT guaranteed).

### R10 — Run-to-run variation at temperature 0 *(certain)*

Tool results contain counts, ages and timestamps, so prompts differ between runs; the overflow
crash in M2 did not recur on re-run (`results/raw/superseded/README.md`). **Mitigation:** paired
tests, counts not percentages, and no claim resting on a single case.

### R11 — Timings depend on someone else's GPU load *(certain)*

~42 vs ~5.5 tokens/s between runs (`m5-comparison.md`). **Mitigation:** times compared within a run
only; model calls reported as the stable cost.

### R12 — Platform-specific setup *(medium for reproducers)*

`scripts/setup_postgres.sh` downloads Ubuntu arm64 packages of PostgreSQL 16.15; macOS uses
Homebrew and has not been run on a real Mac (README § macOS). **Mitigation:** the offline path in
[start.md](../start.md) needs neither; [RUNBOOK.md](../RUNBOOK.md) states the host assumptions.

## Claim hygiene

| Don't claim | Do claim |
|---|---|
| "The agent resolves incidents" | "The durable agent succeeded on 14/30 live incidents (4/10 held-out) with a 7B model; the scripted runbook 30/30" |
| "Durability improves the agent" | "Durability makes interrupted actions resume without repeating their effect; accuracy was unchanged (5 discordant cases, McNemar p = 1.0)" |
| "Exactly-once actions" | "A resumed action does not repeat its effect under the same action id — with the limits in 04 (e.g. at most one extra restart if something else restarted the instance); the audit record is at-least-once and can be delayed" |
| "Resistant to prompt injection" | "An injected request was routed to approval and never ran; the model itself was fooled once in one run" |
| "Tamper-proof audit log" | "Append-only by database triggers against the executor's own writes, on a store outside the sandbox's failure domain" |
| "Human-in-the-loop approval" | "Approval by a different identity is enforced; in evaluation a stand-in approved everything" |
| "v2 prompt improved diagnosis" | "v2 gained on dev and not on held-out; no demonstrated improvement" |
| "Containerized, with OpenTelemetry" | "Local processes behind a `Runtime` interface; JSON logs and metrics" |

## The question to have a real answer for

> *"Your script gets 30/30 and your agent gets 14/30. Why would anyone use the agent?"*

Because the script only knows the ten incidents it was written for, and the project is not about
the 14. It is about what happens on the other 16: every wrong proposal went through a schema, a
policy file and an audit log; the risky ones waited for a different identity; the ones that
"succeeded" without fixing anything were caught by a probe that doesn't trust the agent; and a
crash in the middle of a config rollback did not roll back twice. Those properties hold for any
model you put in that slot. Then say what would change the 14: a larger model and two cheap
agent-side checks ([06](06-roadmap.md) § Next) — and that 30 cases cannot show a small gain either way.
