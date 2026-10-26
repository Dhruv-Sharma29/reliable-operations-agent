# Start here

A guide to checking this project's claims on your own machine, and to what each step shows. The
[README](README.md) presents the results; [docs/](docs/README.md) is the design record.

Everything in this file runs **offline: no PostgreSQL running, no model server, no network after
install**. That is deliberate. The policy, schemas, evidence ledger, agent loop (against a scripted
fake model) and scorer are all testable without a daemon, and every published comparison can be
recomputed from the committed raw records. Steps that need PostgreSQL running or a live model are in
[RUNBOOK.md](RUNBOOK.md).

---

## 1. Install

Python 3.12 is required (`pyproject.toml`: `>=3.12,<3.13`). With [uv](https://docs.astral.sh/uv/):

```bash
uv sync
```

This installs FastAPI, uvicorn, httpx, psycopg (binary), jsonschema and pytest from `uv.lock`.

---

## 2. Run the tests that need no daemon

```bash
uv run pytest tests/test_policy.py tests/test_units.py tests/test_tools.py tests/test_llm_agent.py
```

Expect **63 passed**. What they cover:

| File | Tests | Shows |
|---|---|---|
| `test_policy.py` | 15 | the decision table; default forbid; `decide()` takes no text input; policy file matches the action tools |
| `test_tools.py` | 19 | schemas match function signatures; `action_id` is in no model-facing schema; invalid and injected arguments rejected; the evidence ledger refuses actions |
| `test_units.py` | 14 | the scorer: false alarm fails on any change, escalate case needs escalation and no change; actions refuse out-of-scope targets; incident ports don't collide with the sandbox's |
| `test_llm_agent.py` | 15 | the loop against a scripted fake model: citations, budgets, forced finalisation, server errors, truncated replies |

The other 51 tests (`test_sandbox.py`, `test_executor.py`, `test_durability.py`, `test_store_auth.py`, `test_deploy.py`) start real
PostgreSQL instances and app processes. They **skip** when PostgreSQL hasn't been unpacked
(`reason="run scripts/setup_postgres.sh"`). Once it has, plain `uv run pytest` runs all 114 and
starts those daemons on ports 17xxx — see [RUNBOOK.md](RUNBOOK.md) § 2.

---

## 3. Recompute the published comparison from raw records

```bash
mkdir -p /tmp/ops-agent-check
uv run python -m eval.compare results/summary/m5_manifest.json --out /tmp/ops-agent-check/m5
diff /tmp/ops-agent-check/m5.md results/summary/m5.md && echo identical
```

`--out` matters: the default writes over `results/summary/m5.*`. The manifest names the six raw
files behind the M5 table (scripted runbook, simple agent, durable agent × dev/held-out), with the
backlog cases of the baseline taken from the later `*-sustained.jsonl` runs ("later file wins").
Expect `identical`.

| Responder | Success, dev + held-out |
|---|---|
| Scripted runbook | 30/30 |
| Simple agent | 15/30 |
| Durable agent | 14/30 |

The same works for the earlier milestones:

```bash
uv run python -m eval.summarize results/raw/20261006T094258Z-baseline-dev.jsonl --out /tmp/ops-agent-check/m1
uv run python -m eval.summarize_m2 results/raw/20261008T081651Z-llm-readonly-dev-v2s.jsonl \
    --baseline results/raw/20261006T094258Z-baseline-dev.jsonl --out /tmp/ops-agent-check/m2
```

The second prints the v2s read-only dev result: LLM correct **11/20**, baseline 20/20, valid
citations 20/20.

---

## 4. Read one case end to end

```bash
uv run python -m eval.report_ui results/raw/20261008T100015Z-durable-dev-v2s.jsonl \
    --out /tmp/ops-agent-check/durable-dev.html
```

Open the HTML file in a browser (or the committed `results/ui/m5_report.html`, which covers all
four M5 LLM runs). For each case it shows the alerts, the diagnosis with confidence and citation
check, the model's remaining `unknowns`, the action with its policy decision and state, what each
*cited* tool call returned, the full tool trace, the model's own words, and the independent
verifier's verdict next to it. For the durable runs, `stop` is the workflow's final state. Things
to look for:

- a case where the action **succeeded** and the verifier says **unhealthy** — the claimed-vs-verified
  gap ([docs/05](docs/05-evaluation.md) § Reading it);
- `dev-worker_crashed-0`: diagnosis `unknown`, no action, stop `ESCALATED` — the durable workflow
  refusing to act on an `unknown` diagnosis ([docs/02](docs/02-investigation.md) § The propose step);
- `dev-queue_backlog-0`: diagnosed `worker_crashed`, restarted `worker-1`, stop `RESOLVED`, and
  **unhealthy** by the probe — why the workflow's own verdict is never used for scoring
  ([docs/04](docs/04-guarantees.md) § Where the workflow's own verdict is not used).

---

## 5. Read the crash and the audit trail

Two committed records need nothing to read:

- `results/summary/crash_demo.json` — the process killed after the config write (`child_exit: 137`,
  state `EXECUTING`), then the resume: `reconciling → succeeded`, `model_calls: 0`, one config record
  before and after.
- `results/summary/audit_trace_sample.json` — every path through the executor, plus the refused
  UPDATE, DELETE and TRUNCATE on the audit table.

What each field means: [docs/04](docs/04-guarantees.md) § The live crash demo and
[docs/03](docs/03-permissions-and-audit.md) § Audit trace sample.

---

## 6. Where to read next

| If you want… | Read |
|---|---|
| The argument in one page | [docs/README.md](docs/README.md) § The one-paragraph version |
| Why a second rollback is harmful, and how it is prevented | [docs/04-guarantees.md](docs/04-guarantees.md) § Per action |
| What the model may and may not do | [docs/03-permissions-and-audit.md](docs/03-permissions-and-audit.md) |
| Why the LLM gets about half right | `results/summary/m2-failure-analysis.md`, [docs/05-evaluation.md](docs/05-evaluation.md) |
| What is weaker than it looks | [docs/07-risks.md](docs/07-risks.md) § Claim hygiene, [docs/09-implementation-notes.md](docs/09-implementation-notes.md) |
| Running the sandbox, the baseline or the model | [RUNBOOK.md](RUNBOOK.md) |

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| `uv sync` refuses the interpreter | `pyproject.toml` pins Python 3.12 (`>=3.12,<3.13`) |
| `uv run pytest` starts processes or takes minutes | PostgreSQL is unpacked under `.tools/`, so the 36 integration tests run; use the file list in § 2 for the offline set |
| `eval.compare` changed `results/summary/m5.md` | `--out` was left at its default; restore with `git checkout results/summary/m5.md results/summary/m5.json` |
| `eval.report_ui` wrote `results/ui/report.html` | same: pass `--out` |
