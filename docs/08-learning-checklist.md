# 08 — Learning checklist

Ordered by the milestone that forces each skill. "Done when" names the artifact in this repository
that shows it, so each line can be checked rather than asserted.

## M1 — sandbox, faults, baseline

| Skill | Why | Done when |
|---|---|---|
| Process supervision: start/stop/kill, pid files, process groups | The sandbox is local processes; actions restart them | `sandbox/runtime.py` CLI `up | reset | status | down`; `tests/test_sandbox.py::test_crash_is_detected_and_restart_recovers` |
| PostgreSQL in user space (unpacked `.deb`, `initdb`, run as `nobody`) | No system install, no Docker | `scripts/setup_postgres.sh`; two clusters on base+432 and base+433 |
| PostgreSQL connection limits and reserved superuser slots | Exhaustion must break the app but not the investigation | `test_investigation_tools_still_work_when_app_connections_are_exhausted` |
| Fault injection that is actually a fault | A fault that heals or never alerts gives a fake score | No-op control rows in `results/summary/m1.md`; [05](05-evaluation.md) § What the controls caught |
| Black-box health verification | Scoring must not trust the responder | `eval/verify.py`; `test_half_capacity_is_not_healthy`, `test_bad_deploy_is_visible_to_the_verifier` |
| Symptom-based alerting and its ambiguity | Several causes share one alert by design | `ops/alerts.py`; the incident table in [01](01-architecture.md) |

## M2 — read-only agent

| Skill | Why | Done when |
|---|---|---|
| OpenAI-compatible tool calling by hand (`/v1/chat/completions`, `tool_calls`, `role: tool`) | The loop is owned code, no framework | `agent/llm_agent.py`; 15 tests with a scripted fake model in `tests/test_llm_agent.py` |
| Strict JSON Schema (Draft 2020-12): enums for scope, `additionalProperties: false` | Arguments are the attack and error surface | `agent/tools/schemas.py`; drift test `test_schemas_match_function_signatures` |
| Evidence-linked outputs | "It saw X" must be checkable | `agent/evidence.py`; `test_citing_evidence_never_observed_is_flagged` |
| Server-side constrained decoding (`response_format` json_schema) vs `tool_choice` | `tool_choice="required"` was not enforced by the llama.cpp build | v2s: 0/30 lost diagnoses (`m2-failure-analysis.md`) |
| Output-budget failure modes | A truncated tool call crashed a run, then poisoned a history | `test_model_server_error_ends_the_case_without_crashing_the_run`, `test_reply_cut_off_at_max_tokens_is_not_added_to_the_history` |
| Failure analysis by evidence group | Distinguishes "never looked" from "looked and misread" | `results/summary/m2-failure-analysis.md` groups A/B |
| Paired small-n statistics (exact McNemar) | Two prompts on the same 30 cases | p = 0.33 (v1 vs v2), p = 1.0 (simple vs durable) |

## M3 — permitted execution

| Skill | Why | Done when |
|---|---|---|
| Policy as data plus a pure decision function | Authorization must not read model or log text | `agent/policy.json`; `test_decision_has_no_text_inputs` |
| Approval with separation of identities | The requester can't approve itself | `test_approval_required_action_waits_and_cannot_be_self_approved` |
| Append-only tables with triggers, including `TRUNCATE` | Row triggers don't fire on TRUNCATE | `agent/store.py`; `test_audit_log_is_append_only`; `audit_trace_sample.json` |
| Failure-domain isolation | The incident took out the control plane | Separate PostgreSQL; `test_audit_store_works_with_the_sandbox_database_down` |
| Fail-closed design | No record, no action — but report an effect that did happen | `test_unreachable_audit_store_blocks_the_action`, `test_store_failure_after_the_effect_is_reported_truthfully` |
| Prompt injection as a permissions problem | The model will read attacker text | `eval/injection_demo.py`; both demo JSONs |

## M4 — durability

| Skill | Why | Done when |
|---|---|---|
| Idempotency keys at the effect boundary | Retries alone don't give exactly-once effects | Per-action table in [04](04-guarantees.md); `test_repeating_a_rollback_with_its_action_id_is_a_no_op` |
| Deterministic ids (`uuid5`) persisted before execution | A resumed run must find the same action | `workflow.action_id_for`; `test_crash_after_effect_then_resume_does_not_repeat_it` |
| Ambiguous completion and reconciliation | A timeout is *unknown*, not *failed* | `Executor._reconcile`, `act.observe`; `test_timeout_after_the_effect_is_reconciled_without_a_second_effect` |
| Persisted state machines (commit before work) | Crash anywhere, resume without re-asking the model | `agent/workflow.py`; `test_crash_after_proposal_resumes_without_asking_the_model_again` |
| Crash testing with `os._exit` and counting effects externally | Testing the agent's own records proves nothing | `tests/test_durability.py`, `tests/crash_helpers.py` |

## M5 — evaluation and UI

| Skill | Why | Done when |
|---|---|---|
| Comparison manifests and "later file wins" merging | Backlog cases were re-run after the injection fix | `results/summary/m5_manifest.json`; `eval/compare.py` |
| Claimed vs verified as a first-class metric | The agent reports success on broken services | `claimed_not_verified` column, 9/60 |
| A static, self-contained report | Opens from disk or a CI artifact | `results/ui/m5_report.html` |

## Interview-ready talking points, mapped to what was built

| Question | Where the answer lives |
|---|---|
| "How do you stop the model doing something dangerous?" | [03](03-permissions-and-audit.md) — schema before policy, policy from a file, approval by another identity, default forbid |
| "What happens if it crashes mid-action?" | [04](04-guarantees.md) — action id in the same write as the effect, reconciliation, the live crash demo |
| "Retries are fine, right?" | [04](04-guarantees.md) § Per action — a second `rollback_config` rolls back to the bad config; the test proves it |
| "How do you know it worked?" | [05](05-evaluation.md) — probe through the public API; 9 of 60 claimed successes were false |
| "How good is the agent?" | 14/30 durable, 15/30 simple, 30/30 script, and why that's the honest floor for a 7B model |
| "Did your prompt engineering help?" | [02](02-investigation.md) § Prompt versions — no: dev up, held-out flat, p = 0.33 |
| "What about prompt injection?" | [03](03-permissions-and-audit.md) § Injection demo — the model was fooled; the request waited for approval |
| "Why no agent framework?" | [01](01-architecture.md) § Decision record — and that the hands-on trial was not done |
| "What surprised you?" | The incident that disabled the agent's own audit store; the backlog that healed itself; a truncated reply that poisoned the conversation |
| "What would you do differently?" | Commit the mutation check as a script; give the audit role no DDL rights; build `resume_all()` into a supervisor ([09](09-implementation-notes.md)) |
