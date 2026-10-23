# Superseded runs

Kept as evidence for harness fixes; not used in any reported result.

- `20261006T091259Z-noop-dev.jsonl`: no-op control, verifier v2. It exposed:
  1. `db_connection_exhaustion` (2 workers): no alert, healthy without intervention. The exporter
     leak stopped at its first refused connection, so the amount leaked depended on what other
     clients held at that instant. Fixed: the leak keeps taking freed slots until only `keep_free` remain.
  2. `bad_deploy`: broken (verifier fails) but **no alert fired**. The bug affects ~10% of
     requests, at the ApiErrorRateHigh threshold. Fixed: added a 5xx-specific alert rule.
- An earlier run with verifier v1 was deleted before completion; its gap (half-capacity counted
  as healthy, probes that couldn't see the 2.3.1 bug) is recorded in `eval/verify.py`.
- Held-out no-op/baseline runs made before 2026-10-06 ~10:00 used `db_connection_exhaustion`
  held-out params `{"workers": 2, "keep_free": 2}`. The no-op control showed no alert and a healthy
  verifier: two spare connection slots absorb this load, so it was not an incident. Replaced with
  `{"workers": 3, "keep_free": 0}` and the held-out split re-run.

## 20261007T050255Z-llm-readonly-heldout.jsonl (incomplete, 3 of 10 cases)

The run crashed on case 4 (`heldout-db_connection_exhaustion-0`; the truncated arguments ended in `"db_connection_exhaustion`). The model hit its 400-token output budget in
the middle of a `submit_diagnosis` call. llama.cpp couldn't parse the truncated arguments and
returned a 500, the gateway returned a 502, and the agent loop raised, ending the whole run. Fix:
`agent/llm_agent.py` now records a model error as a failed case with `stop_reason="model_error"`
(test: `test_model_server_error_ends_the_case_without_crashing_the_run`). The prompt and budgets
are unchanged, so the held-out split was rerun in full with the same agent.
In the rerun the same case completed normally. Temperature 0 doesn't make runs repeatable here:
tool results (counts, ages, timestamps) differ between runs, so the prompts differ. So the overflow
is intermittent. Its root cause is still there: the schema allows up to 10 `unknowns` of 300 characters each (about 750 tokens), more than the 400-token output budget.

## Backlog cases in runs before 2026-10-07T10:00Z (burst-only injection)

The `queue_backlog` cases in `20261006T*-noop-*`, `20261006T*-baseline-*`, the M2 v1
`20261007T042004Z`/`20261007T051557Z` runs and `20261007T06*-noop-wait-*-90s` used a one-off burst
that one worker drains in ~100 s. The `-90s` runs show a do-nothing responder that waits 90 s gets
"healthy after" on all three. Those files stay in `results/raw/`, because their other incidents are
valid. Their backlog rows are superseded by `*-sustained.jsonl`: burst plus continuous import,
see `incidents/catalog.py`. M2 v1's backlog *diagnoses* were made against the old injection;
v1 vs v2 comparisons on that incident are confounded by the change.

## 20261007T*-llm-act-dev-v2.jsonl (aborted after 6 of 20 cases)

The acting agent crashed on `dev-db_connection_exhaustion-0`. The agent's audit store
(`agentstate`) lived on the sandbox's own PostgreSQL, so the exhaustion incident made it
unreachable, and creating the executor raised before the agent could act. The incident took out
the agent's control plane.

Fix (user-approved):
- **Isolation:** the store runs on a separate PostgreSQL instance (`.run/agent-pg`, port base +
  433) that sandbox resets and incidents never touch.
- **Fail closed:** if the store is unreachable, no action runs, the step is recorded as
  `blocked_no_audit`, and the run continues.

Tests are in `tests/test_executor.py`. The acting-agent evaluation was rerun from scratch.

## 20261008T071410Z-llm-readonly-dev-v2s.jsonl (stopped after 19 of 20 cases)

This was the first run with the schema-enforced final answer. It was stopped on purpose after
`dev-db_connection_exhaustion-0` lost its diagnosis anyway. A normal step hit the 400-token cap
(gateway log: `output_tokens: 400`, status 200), and the cut-off reply (prose plus the start of a
tool call) was appended to the conversation. Every later request with that history was then
rejected by llama.cpp *before generating* (HTTP 500 in 8-25 ms: "Failed to parse tool call
arguments"), the schema-enforced final answer included.

Fix: a reply with `finish_reason == "length"` is never added to the history; the agent moves
straight to the schema-enforced final answer. Rerun from scratch.
