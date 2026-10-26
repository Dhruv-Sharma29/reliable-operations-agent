# Runbook — the steps that need PostgreSQL running or a model server

[start.md](start.md) covers everything that runs offline. Everything below starts daemons: two
PostgreSQL clusters and the sandbox's app processes, and from § 5 on, calls to a model server.
These are the commands that produced `results/`, in the order they depend on each other.

**Host assumptions.** The published numbers come from a Linux arm64 host (README § macOS calls it
the GX10). `scripts/setup_postgres.sh` downloads PostgreSQL 16.15 packages for Ubuntu 24.04 arm64
with `apt-get download`, so it needs network access and those apt sources. On other Linux hosts,
point `PG_BIN` at an existing PostgreSQL `bin/` directory instead (`sandbox/paths.py`). macOS uses
Homebrew and is untested on a real Mac (§ 8).

**Several steps overwrite committed results.** `eval.injection_demo`, `eval.crash_demo` and
`eval.audit_sample` write fixed paths under `results/summary/` and have no `--out`. Run them on a
branch, or restore afterwards with `git checkout results/summary/`.

---

## 1. PostgreSQL in user space

```bash
scripts/setup_postgres.sh
```

Unpacks `postgresql-16`, `postgresql-client-16` and `libpq5` into `.tools/pg`; nothing is installed
system-wide, and `rm -rf .tools` undoes it. Idempotent. When run as root, PostgreSQL is started as
`nobody`, because it refuses to run as root (`sandbox/runtime.py`).

## 2. The full test suite

```bash
uv run pytest
```

**114 tests.** With PostgreSQL unpacked, the 51 integration tests run: `test_sandbox.py` (8) starts
the sandbox and injects real faults; `test_executor.py` (25) exercises the audit store, including
with the sandbox database stopped; `test_durability.py` (9) kills processes with `os._exit` between
an effect and its record; `test_store_auth.py` (7) checks the store's password auth, and, when run as
root, runs the executor as a separate uid; `test_deploy.py` (2, root and a non-root account needed)
hands a sandbox to that account and fixes an incident as it (port base 16500, `.run-test-deploy/`). Tests use port base 17000 and `.run-test/` (`tests/conftest.py`), so they
don't touch a development sandbox or an evaluation in progress.

### The audit store's credentials

The agent's store (port base + 433) requires a password for every role. On first start it writes
one random password per role to `.run/agent-secrets/` (0600 files): `postgres.pw` for the user that
set it up, `agent.pw` for `$AGENT_OS_UID` (default: the same user). To run the agent as its own OS user:

```bash
AGENT_OS_UID=<agent uid> uv run python -m agent.store init   # as the setup user: start, schema, hand over agent.pw
# then start the agent as that uid; it logs in as `agent` and can't read postgres.pw
```

To run the sandbox *and* the agent as that user, which is what a deployment would do, see § 3a.

A store created before 2026-10-26 (trust auth) is migrated on its next start, data kept. Rotation
isn't automated: deleting `.run/agent-secrets/` on its own locks setup out of the existing store, so
delete `.run/agent-pg/` with it (which drops the log) or change the passwords with `ALTER ROLE` first.

## 3. The sandbox

```bash
uv run python -m sandbox.runtime up              # start PostgreSQL + app components
uv run python -m sandbox.runtime status          # one JSON line per component
uv run python -m sandbox.runtime reset --workers 2
uv run python -m sandbox.runtime down            # stop app components
uv run python -m sandbox.runtime pg-stop         # also stop both PostgreSQL clusters
```

Loopback ports with the default base 18000: API 18080, payments 18070, workers 18091+, sandbox
PostgreSQL 18432, agent store 18433. Change the base with `SANDBOX_PORT_BASE` and keep it below the
kernel's ephemeral range ([docs/09](docs/09-implementation-notes.md) § 15). Logs and state live
in `.run/`.

## 3a. Running as a separate OS user

```bash
export SANDBOX_RUN_DIR=$PWD/.run-agent SANDBOX_PORT_BASE=16000        # optional: keep it apart from .run/
python -m sandbox.deploy prepare --agent-uid 1000                       # as root
python -m sandbox.deploy exec -- .venv/bin/python -m eval.run --responder baseline-exec --split dev \
    --out-dir $SANDBOX_RUN_DIR/results                                  # runs as uid 1000
python -m sandbox.deploy exec -- .venv/bin/python -m sandbox.runtime pg-stop   # stops its sandbox, not the store
python -c "from sandbox.runtime import Runtime; Runtime().agent_db_stop()"       # as root: the store
```

`prepare` stops what runs from that run directory, starts the audit store as the setup user, hands
`agent.pw` and the sandbox's directories (including its PostgreSQL cluster) to the agent's uid, and
records the uid in `deploy.json`. `exec` drops to that uid and runs the command, as a service
manager would. The uid must be a real account (initdb looks it up), not root and not `nobody`. The
agent's user can't write `results/raw/`, hence `--out-dir`; copy the file across afterwards.
`prepare --agent-uid 0` hands the sandbox back to root. Use `.venv/bin/python` rather than `uv run`
under `exec`: the agent's user has no uv cache. Result of the full run:
`results/summary/nonroot-deployment.md`.

## 4. Controls and the scripted baseline — reproduces M1

```bash
uv run python -m eval.run --responder noop --split dev
uv run python -m eval.run --responder baseline --split dev
uv run python -m eval.run --responder noop --split heldout
uv run python -m eval.run --responder baseline --split heldout
NOOP_WAIT_S=90 uv run python -m eval.run --responder noop-wait --split dev --only queue_backlog
uv run python -m eval.run --responder baseline-exec --split dev    # baseline's decisions, through the executor
uv run python -m eval.summarize results/raw/<run>.jsonl --out /tmp/ops-agent-check/m1
```

Each run writes `results/raw/<UTC timestamp>-<responder>-<split>[-<label>].jsonl` and prints one
line per case. `--only <incident>` runs one incident; `--label` tags the file name. Expected: no-op
healthy only on the false alarms; baseline 20/20 and 10/10 (`results/summary/m1.md`). A case that
does not start healthy and alert-free is recorded as a harness error and not scored.

## 5. The model endpoint

The agent speaks the OpenAI-compatible chat API. The published runs used the companion project 01
gateway in front of llama.cpp serving Qwen2.5-7B-Instruct Q4_K_M (`results/summary/m5-comparison.md`);
that project is not in this repository.

```bash
export AGENT_LLM_URL=http://127.0.0.1:21091      # default; any /v1/chat/completions endpoint
export AGENT_LLM_MODEL=qwen2.5-7b-instruct       # default
# optional: AGENT_PROMPT=v1|v2 (default v2), AGENT_FINAL=schema|tool (default schema)
```

The server must support `tools` and `response_format` of type `json_schema`; the default final
answer depends on the second ([docs/02](docs/02-investigation.md) § Loop mechanics). Requests use
temperature 0 and 400 output tokens per call. Expect run-to-run variation anyway
([docs/07](docs/07-risks.md) § R10), and timings that depend on the GPU's other load.

## 6. LLM runs and demos — reproduces M2–M5

```bash
uv run python -m eval.run --responder llm-readonly --split dev     # M2
uv run python -m eval.run --responder llm-act      --split dev     # M3 / M5 simple agent
uv run python -m eval.run --responder durable      --split dev     # M5 durable agent
# ... and the same with --split heldout, once per frozen prompt version
uv run python -m eval.summarize_m2 results/raw/<llm run>.jsonl --baseline results/raw/<baseline run>.jsonl
uv run python -m eval.injection_demo                               # writes results/summary/injection_demo.json
uv run python -m eval.crash_demo                                   # writes results/summary/crash_demo.json
```

`llm-act` and `durable` approve approval-required actions as `eval-approver`, a stand-in for the
on-call human; each approval is in the audit log. The injection demo runs with **no** approver.
The crash demo reports "inconclusive" if the model's proposal never reaches an effect (e.g. it
escalates); re-run it. To compare your own runs, copy `results/summary/m5_manifest.json`, point it
at your files, and run `eval.compare` with `--out` ([start.md](start.md) § 3).

## 6a. Start-up recovery

```bash
uv run python -m agent.supervisor               # resume every unfinished incident; one JSON line each
```

Run it after a crash or restart. Actions needing approval keep waiting; approve with
`Executor("oncall-human").approve(action_id, "oncall-human")`.

## 7. The audit trace sample

```bash
uv run python -m eval.audit_sample               # writes results/summary/audit_trace_sample.json
```

Needs only the agent store, no model — but it reads back the paths recorded by § 6's acting runs
and the crash demo's action (whose id it takes from `results/summary/crash_demo.json`). Run it after
§ 6 on the same store. By reading the code (not run), on a fresh store it would fail looking up that
action. It then generates the forbidden, invalid and denied paths with action functions that fail
if called, and tries UPDATE, DELETE and TRUNCATE on `audit_events`; it prints `passed` when all are refused.
Since 2026-10-23 the refusal comes from privileges ("permission denied"), with the trigger behind it;
the committed sample predates that ([docs/03](docs/03-permissions-and-audit.md)).

## 8. macOS (Apple silicon)

```bash
brew install postgresql@17 && export PATH="$(brew --prefix postgresql@17)/bin:$PATH"   # as in the README
scripts/setup_postgres.sh && uv sync && uv run pytest
```

On macOS the script only checks for `pg_config` and prints the Homebrew bin directory;
`sandbox/paths.py` finds PostgreSQL through `pg_config` (or `PG_BIN`). The script's own hint says
`postgresql@16` while the README says `postgresql@17` **[verify]**. Mac timings are not comparable
with the published numbers.

## Teardown

```bash
uv run python -m sandbox.runtime pg-stop         # app components and both PostgreSQL clusters
rm -rf .run .run-test                            # sandbox, agent store and test state
rm -rf .tools                                    # the unpacked PostgreSQL
```

Removing `.run/` deletes the agent store, i.e. the audit trail of every local run.
