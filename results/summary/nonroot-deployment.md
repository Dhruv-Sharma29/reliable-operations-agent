# The sandbox and the agent as their own OS user (2026-10-26)

**Question.** Does the whole control path work when the agent and the sandbox it operates run as an
unprivileged OS user, while the audit store's superuser stays with the setup user?

**Setup.** `python -m sandbox.deploy prepare --agent-uid 1000` as root, then every run through
`python -m sandbox.deploy exec`, which drops to uid 1000 (the container's `ubuntu` account) before
starting Python. Run directory `.run-agent/`, port base 16000. Responder `baseline-exec`: the scripted
runbook's decisions, with every action sent through the policy-enforcing executor and the audit
store, approvals by the stand-in `eval-approver`. No model is involved, so the run is
deterministic and isolates the deployment from model quality.

| Who | Runs as |
| --- | --- |
| Sandbox processes (payments, API, workers, report-exporter, loadgen) and their restarts | uid 1000 |
| The sandbox's PostgreSQL | uid 1000 |
| Evaluation harness, agent, executor | uid 1000 |
| Audit store PostgreSQL | `nobody` (65534) |
| Audit store superuser password (`postgres.pw`) | root only (0600) |

## Results

| Run | Cases | Success | Actions through the executor | Of which approval-required | Executor state |
| --- | --- | --- | --- | --- | --- |
| dev (`20261026T065405Z-baseline-exec-dev-nonroot-uid1000.jsonl`) | 20 | 19 | 18 | 5 | 18 succeeded |
| heldout (`20261026T070106Z-baseline-exec-heldout-nonroot-uid1000.jsonl`) | 10 | 10 | 9 | 3 | 9 succeeded |
| dev `bad_config`, after the port fix (`20261026T070607Z-baseline-exec-dev-nonroot-uid1000-portfix.jsonl`) | 2 | 2 | 2 | 2 | 2 succeeded |

Median time from response to verified recovery: 1.6 s (dev) and 1.7 s (heldout). With the fix, all
30 incidents succeed as uid 1000, the same as the scripted baseline run as root (`m1.md`).

**The one failure was a catalogue bug, not a deployment problem.** `dev-bad_config-0` pointed the API
at `PG_PORT + 1`, which is the agent store's port at every port base, ever since the store moved to
its own instance. The API reached a live server that refused it, instead of a closed port, so the
runbook's "connection refused" rule didn't match and it escalated. The scripted baseline never
showed this because it doesn't start the store. The bad port is now `PG_PORT + 2`, and
`tests/test_units.py::test_bad_config_ports_are_not_used_by_the_sandbox` guards it. Every earlier
`llm-act` and `durable` run had the store up, so in their `dev-bad_config-0` case the API showed a
database error from the wrong server rather than "connection refused". The LLM agents diagnosed
that case correctly in 5 of 6 runs. Re-run with the fixed port (next section), both agents get it
right 3 times in 3, as their published runs did, so the published M5 numbers stand.

## The LLM agents as uid 1000

Same deployment, the two published LLM agents (`llm-act` and `durable`, prompt v2 with the
schema-enforced final answer, Qwen2.5-7B-Instruct Q4_K_M at temperature 0), all 30 incidents each,
with the fixed catalogue. The model was served by llama.cpp directly with 4 slots (project 04's
`env/models.sh`), not through project 01's gateway as in the published runs.

| Agent | dev | heldout | Total | Published M5 (as root) | Cases that differ (only published / only uid 1000) | Exact McNemar |
| --- | --- | --- | --- | --- | --- | --- |
| Simple (`llm-act`) | 8/20 | 4/10 | 12/30 | 15/30 | 5 / 2 | p = 0.45 |
| Durable | 11/20 | 5/10 | 16/30 | 14/30 | 2 / 4 | p = 0.69 |

Neither agent differs measurably from its published run. The cases that changed are the model
diagnosing differently between runs (7 and 6 cases flip, in both directions), as already seen in
earlier reruns (`docs/07-risks.md` § R10), not the deployment. No harness errors. Across these runs
and the repeats, 52 actions reached an effect: 47 succeeded and 5 failed. All 5 failures were rollbacks
the incident didn't call for (a release rollback in a config or connection incident, a config
rollback for connection exhaustion); the one with a recorded reason says "no previous release". No
record contains a permission error.

**The corrected `dev-bad_config-0`**, with two extra repeats per agent
(`*-badconfig-rep2`, `*-badconfig-rep3`): 3/3 correct for each agent, the same outcome as in the
published runs. The wrong symptom didn't change that case's result. Its sibling `dev-bad_config-1`
is the unstable one: 2/3 for the simple agent and 1/3 for the durable one across the same samples.

Files: `results/raw/20261026T073335Z-llm-act-dev-nonroot-uid1000-v2s.jsonl`,
`20261026T075025Z-llm-act-heldout-…`, `20261026T075811Z-durable-dev-…`,
`20261026T082022Z-durable-heldout-…`, and the four `*-badconfig-rep*` files.

## Checks in the test suite

`tests/test_deploy.py` (root only): `prepare`, then `bad_deploy` (an approval-required rollback) end to
end as the agent's user, asserting that every sandbox process and the sandbox's Postgres run as that
uid, the audit store runs as `nobody`, the audit rows show `ops-agent` as requester and
`eval-approver` as approver, and the agent's user can stop its own sandbox database but can neither
stop nor signal the audit store. `tests/test_store_auth.py` covers the passwords.

## Not covered

- One replicate per LLM agent: enough to show no measurable effect, not to rule out a small one.
- Exited sandbox processes stay as zombies under PID 1 in this container, because its init
  (`tail -f /dev/null`) doesn't reap them. Root's processes show the same; it is the container, not
  the deployment.
- Root on the host can still do anything; the separation is between the agent's user and the store's.
