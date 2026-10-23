# 05 — Evaluation

## What question the eval answers

Not "is the agent good". Specifically:

> **On live injected incidents, how often does each responder reach a verified-healthy service with
> the right diagnosis and only necessary, permitted actions — and how often does an agent's own
> report of success disagree with the service?**

Durability is *not* measured by this eval. It is a correctness property, tested by killing
processes ([04](04-guarantees.md)). The comparison checks only that adding it did not change
accuracy.

## Cases

Ten incidents (`incidents/catalog.py`), each with **2 dev variants and 1 held-out variant**: 20 dev
+ 10 held-out = 30 cases per responder. Variants differ in parameters: worker count, which worker
fails, the bad port and its change author, backlog size and import rate, provider latency, spool
size and file count; `tests/test_units.py::test_catalogue_is_complete_and_splits_differ`
checks the splits differ.

| Expected | Incidents | Fix cases (dev / held-out) |
|---|---|---|
| `fix` | worker crashed, worker hung, bad config, DB connection exhaustion, queue backlog, disk fill, bad deploy, expired credential | 16 / 8 |
| `escalate` | slow external payments provider | — |
| `no_action` | client sending malformed requests (false alarm) | — |

Several incidents share an alert on purpose ([01](01-architecture.md) § Incident catalogue).
The held-out split was fixed before any prompt was tuned; each frozen prompt version runs on it once.

## Responders

| Responder (`--responder`) | What it is | Role in the eval |
|---|---|---|
| `noop` | does nothing | proves each incident stays broken; false alarms stay healthy |
| `noop-wait` | does nothing for `NOOP_WAIT_S` (default 90 s ≈ LLM latency) | proves an incident doesn't heal while the agent thinks |
| `baseline` | scripted runbook, `runbooks/baseline.py` | ceiling for this evidence; rules written from the catalogue |
| `llm-readonly` | investigator only ([02](02-investigation.md)) | M2 diagnosis accuracy |
| `llm-act` | investigator + one step through the executor: the **simple agent** | M3, M5 |
| `durable` | the same, inside the persisted workflow: the **durable agent** | M5 |

The two acting responders approve approval-required actions as `eval-approver`
([03](03-permissions-and-audit.md) § Approval).

## Scoring (`eval/run.py::score`)

State changes are the executed actions other than `escalate`; *unnecessary* are changes not in the
incident's `acceptable_actions`.

| Expected | Success requires |
|---|---|
| `fix` | correct diagnosis **and** verified healthy **and** at least one change **and** no unnecessary change |
| `escalate` | escalated **and** no change |
| `no_action` | no change **and** verified healthy |

Consequences worth knowing when reading the tables: success needs the *label* to be right on fix
cases (6 M3 cases fixed the service with the wrong label and failed, `results/summary/m3-acting.md`);
and on the escalate and false-alarm cases success does not need the label, so a responder can have
more successes than correct diagnoses (simple agent, held-out: 6 vs 5).

## The verifier (`eval/verify.py`)

Healthy means, for **two consecutive rounds**: every configured api/payments/worker instance is
running; probe jobs shaped like real traffic — with *and without* `customer` — are accepted,
readable through the API and completed by a worker; every configured worker has a fresh heartbeat
(≤ 5 s); the non-probe backlog is ≤ 200. It reads no responder output. It also runs *before*
injection: a case that doesn't start healthy and alert-free is a harness error, not scored.

## What the controls caught

Four harness defects, each of which would have produced a misleading score
(`results/raw/superseded/README.md`, `eval/verify.py` docstring, README § Results M1):

| Defect | Found by | Fix |
|---|---|---|
| Verifier v1 scored half capacity as healthy and could not see the 2.3.1 bug (affects payloads without `customer`) | no-op control | two rounds, both payload shapes, capacity check |
| Exporter leak stopped at its first refused connection → no alert, healthy without help | no-op control | leak keeps taking freed slots until `keep_free` remain |
| Bad deploy (~10 % of requests failing) never crossed the generic error threshold | no-op control | 5xx-specific alert `Api5xxRateHigh` |
| Held-out exhaustion params `{workers: 2, keep_free: 2}` were not an incident | no-op control | `{workers: 3, keep_free: 0}`, held-out re-run |
| Backlog was a one-off burst one worker drains in ~100 s; the LLM takes ~90 s | `noop-wait` | burst + sustained import at ~1.5× one worker; sustained cases: baseline 3/3, `noop` 0/3, `noop-wait` 0/3 |

The fifth row is the one to remember: a slow responder looked like a successful one.

## Results

### M1 — controls and scripted baseline (`results/summary/m1.md`)

| Run | Cases | Healthy after | Success |
|---|---|---|---|
| No-op, dev / held-out | 20 / 10 | 2 / 1 (the false alarms) | 2 / 1 |
| Scripted baseline, dev / held-out | 20 / 10 | 18 / 9 (slow dependency is escalated, not fixed) | 20 / 10 |

### M2 — read-only diagnosis (`results/summary/m2-*.md`, `m2-failure-analysis.md`)

Correct diagnoses: v1 **7/20, 5/10**; v2 **12/20, 5/10**; v2s **11/20, 6/10**; baseline 20/20, 10/10.
Every submitted diagnosis cited only observed evidence. Detail and the v1→v2 McNemar test:
[02](02-investigation.md) § Prompt versions.

### M3 — acting agent, prompt v2 (`results/summary/m3-acting.md`)

| | Dev (20) | Held-out (10) |
|---|---|---|
| Success | **9** | **5** |
| Correct diagnosis | 9 | 6 |
| Healthy after, verified | 12 | 6 |
| Approval-required actions executed (stand-in) | 8 | 4 |
| Unnecessary or wrong actions | 4 | 1 |
| No diagnosis (output format) | 2 | 0 |

### M5 — the three-way comparison (`results/summary/m5-comparison.md`, `m5.json`)

Prompt v2s, same 30 cases per responder.

| Responder | Split | Success | Correct diag. | Verified recovery (fix cases) | Unnecessary | Claimed ≠ verified | Median model calls | Median time to recovery |
|---|---|---|---|---|---|---|---|---|
| Scripted runbook | dev | **20/20** | 20 | 16/16 | 0 | 0 | 0 | 2.0 s |
| Scripted runbook | held-out | **10/10** | 10 | 8/8 | 0 | 0 | 0 | 1.9 s |
| Simple agent | dev | **9/20** | 11 | 10/16 | 5 | 4 | 8 | 15.7 s |
| Simple agent | held-out | **6/10** | 5 | 5/8 | 2 | 3 | 7.5 | 14.6 s |
| Durable agent | dev | **10/20** | 11 | 10/16 | 4 | 1 | 8 | 15.7 s |
| Durable agent | held-out | **4/10** | 7 | 6/8 | 2 | 1 | 7 | 15.6 s |

Totals: scripted **30/30**, simple **15/30**, durable **14/30**.

**Same-day rerun (2026-10-23).** Project 04's transfer check ran the simple agent again on all 30
cases with the same base model: **16/30** (raw: `results/raw/20261023T14*-llm-act-*-transfer-base-7b.jsonl`),
in line with 15/30. The same run with project 04's fine-tuned 7B scored 12/30 (paired with base:
9 vs 5 cases, p = 0.42); the analysis is in project 04 (`results/summary/m5-transfer.md`).

**Reproducible offline.** `python -m eval.compare results/summary/m5_manifest.json --out <dir>/m5`
regenerates this table byte-for-byte from the six raw files the manifest names (checked
2026-10-23; [start.md](../start.md) § 3).

## Reading it

- **The scripted runbook wins outright.** That is its job: it shows the evidence suffices. It says
  nothing about unseen incidents.
- **Simple vs durable: no detectable difference.** 5 cases differ, 3 favour simple and 2 durable;
  exact McNemar p = 1.0 (recomputed from `m5.json` § `cases`). Run-to-run diagnosis variation at
  temperature 0 — tool results contain counts and timestamps, so prompts differ — explains it.
- **Behaviour differs where the code differs.** The durable workflow escalates on an `unknown`
  diagnosis instead of proposing anyway: 9 escalations vs 5 of 30. It also had fewer
  claimed-but-unhealthy cases (2 vs 7); at n = 30 that is a hypothesis, not a finding.
- **Claimed vs verified.** In **9 of 60** LLM cases an action reported success but the probe found
  the service still broken: wrong target, wrong action, or not enough capacity. The M3 runs show
  what those look like: `dev-worker_crashed-1` restarted `worker-1` when `worker-2` had crashed;
  `heldout-queue_backlog-0` scaled to 2 workers when the sustained import needs 3 (`m3-acting.md`).
  A scorer that trusted the agent would have counted all nine.
- **Wrong actions did no damage — by luck of the scenario.** In M3 three wrong rollbacks failed on
  the tools' own preconditions ("no previous version"); all three needed approval. With a previous
  version present, they would have run (`m3-acting.md`).

## Resolution

With 30 paired cases, only large differences are detectable; with 10 held-out cases, one case is
10 points. Counts are reported, not percentages. Paired comparisons use the exact McNemar test on
discordant cases. "Dev improved, held-out didn't" (v2) is reported as no demonstrated gain.

## Time is not comparable across runs

The model ran on a shared GPU. The M5 LLM rows ran back to back on an idle GPU (~42 tokens/s);
earlier runs saw ~5.5 tokens/s under other users' load, about 8× slower
(`m5-comparison.md`). Times compare within one run only. Model calls are the stable cost measure.

## Where the evidence is

| What | Where |
|---|---|
| Every case, every responder, every run | `results/raw/*.jsonl` — 24 files, 252 case records (counted 2026-10-23) |
| Runs excluded, and why | `results/raw/superseded/` and its README |
| Per-case evidence, tool trace, action, uncertainty for the M5 LLM runs | `results/ui/m5_report.html` |
| Why the LLM fails | `results/summary/m2-failure-analysis.md`: decisive evidence never fetched (7 of 13 v1 errors) vs decisive tool called, wrong conclusion (6 of 13) |
| Console logs of each batch | `results/*.log` |
