"""Incident catalogue: how to inject each fault, what the correct response is, and the variants
used for dev vs held-out evaluation.

`clear` for every incident is a full `Runtime.reset()` to the known-good baseline. That's stronger
than a targeted undo and guarantees runs don't contaminate each other.

Expected outcome kinds:
  fix        the responder should diagnose it and take one of `acceptable_actions`; scored by
             independent verification that the service is healthy again
  escalate   no safe in-sandbox fix exists; correct response is escalate with no state change
  no_action  nothing is broken; any state-changing action is a failure
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx
import psycopg

from sandbox import paths, state
from sandbox.runtime import Runtime


@dataclass(frozen=True)
class Incident:
    name: str
    description: str
    expected: str                        # fix | escalate | no_action
    acceptable_actions: frozenset[str]   # "action" or "action:service"
    inject: Callable[[Runtime, dict], None]
    variants: dict[str, list[dict]]      # split -> list of params (each includes "workers")
    settle_s: float = 8.0
    verify_timeout_s: float = 30.0
    extra: dict = field(default_factory=dict)


def _kill_worker(rt: Runtime, p: dict) -> None:
    rt.kill_instance(f"worker-{p['victim']}")


def _hang_worker(rt: Runtime, p: dict) -> None:
    (paths.FAULTS / f"worker_hang_worker-{p['victim']}").touch()


def _bad_config(rt: Runtime, p: dict) -> None:
    config = state.current_config()
    config["api"]["db_port"] = p["bad_port"]
    state.write_config(config, author=p["author"], note=p["note"])
    rt.restart("api")


def _leak_connections(rt: Runtime, p: dict) -> None:
    # keep_free > 0 leaves a few slots open: partial exhaustion with intermittent errors.
    (paths.FAULTS / "exporter_leak").write_text(str(p.get("keep_free", 0)))
    rt.restart("report-exporter")


def _backlog(rt: Runtime, p: dict) -> None:
    """Initial burst (so the alert fires) plus a sustained import above one worker's capacity
    (target_app/bulk_import.py). A burst alone drained by itself in ~100 s (results/raw/*noop-wait*)."""
    with psycopg.connect(paths.dsn("postgres", "bulk-import"), autocommit=True) as conn:
        conn.execute("INSERT INTO jobs (kind, payload) SELECT 'invoice', jsonb_build_object('amount', g, 'customer', 'bulk') "
                     "FROM generate_series(1, %s) g", (p["jobs"],))
    (paths.FAULTS / "bulk_import").write_text(json.dumps({"rate": p["rate"]}))
    rt.start_instance("bulk-import")


def _slow_dependency(rt: Runtime, p: dict) -> None:
    httpx.post(f"{paths.PAYMENTS_URL}/admin/config", json={"latency_ms": p["latency_ms"]}, timeout=5)


def _fill_spool(rt: Runtime, p: dict) -> None:
    per_file = p["mb"] * 2**20 // p["files"]
    for i in range(p["files"]):
        with open(paths.SPOOL / f"export-{i:03d}.tmp", "wb") as f:
            f.truncate(per_file)  # sparse: counts toward the quota without using real disk


def _bad_deploy(rt: Runtime, p: dict) -> None:
    state.record_deploy("2.3.1", author="ci", note=p["note"])
    rt.restart("api")


def _expire_credential(rt: Runtime, p: dict) -> None:
    httpx.post(f"{paths.PAYMENTS_URL}/admin/expire", timeout=5)


def _client_error_burst(rt: Runtime, p: dict) -> None:
    with httpx.Client(base_url=paths.API_URL, timeout=5) as client:
        for _ in range(p["requests"]):
            client.post("/jobs", json={"kind": "invoice", "payload": "not-an-object"})


INCIDENTS = {i.name: i for i in [
    Incident("worker_crashed", "A worker process dies.", "fix", frozenset({"restart_service:worker"}),
             _kill_worker,
             {"dev": [{"workers": 1, "victim": 1}, {"workers": 2, "victim": 2}],
              "heldout": [{"workers": 2, "victim": 1}]}),
    Incident("worker_hung", "A worker's loop stalls; its /health still says ok.", "fix",
             frozenset({"restart_service:worker"}), _hang_worker,
             {"dev": [{"workers": 1, "victim": 1}, {"workers": 2, "victim": 1}],
              "heldout": [{"workers": 2, "victim": 2}]}),
    Incident("bad_config", "A config change points the API at the wrong database port.", "fix",
             frozenset({"rollback_config"}), _bad_config,
             {"dev": [{"workers": 1, "bad_port": paths.PG_PORT + 1, "author": "jdoe", "note": "db failover prep"},
                      {"workers": 1, "bad_port": 5432, "author": "deploy-bot", "note": "sync settings from staging"}],
              "heldout": [{"workers": 2, "bad_port": paths.PG_PORT + 10, "author": "asmith", "note": "tune connection settings"}]},
             settle_s=6),
    Incident("db_connection_exhaustion", "A bad report-exporter build leaks DB connections.", "fix",
             frozenset({"restart_service:report-exporter", "stop_service:report-exporter"}), _leak_connections,
             # A keep_free=2 held-out variant was dropped: the no-op control showed 2 spare slots
             # absorb this load (no alert, verifier healthy), i.e. it is not an incident.
             {"dev": [{"workers": 1, "keep_free": 0}, {"workers": 2, "keep_free": 0}],
              "heldout": [{"workers": 3, "keep_free": 0}]}, settle_s=6),
    Incident("queue_backlog", "A bulk import floods the queue beyond one worker's capacity.", "fix",
             frozenset({"scale_service:worker"}), _backlog,
             {"dev": [{"workers": 1, "jobs": 2000, "rate": 35}, {"workers": 1, "jobs": 2400, "rate": 40}],
              "heldout": [{"workers": 1, "jobs": 2600, "rate": 38}]},
             settle_s=5, verify_timeout_s=180),   # 1 worker: grows ~15/s; 3 workers drain ~37/s (measured at rate 38)
    Incident("slow_dependency", "The external payments provider becomes slow.", "escalate", frozenset(),
             _slow_dependency,
             {"dev": [{"workers": 1, "latency_ms": 1500}, {"workers": 2, "latency_ms": 2500}],
              "heldout": [{"workers": 1, "latency_ms": 2000}]},
             settle_s=10, verify_timeout_s=5),
    Incident("disk_fill", "Stray export temp files fill the spool past its quota.", "fix",
             frozenset({"cleanup_spool"}), _fill_spool,
             {"dev": [{"workers": 1, "mb": 120, "files": 12}, {"workers": 1, "mb": 300, "files": 3}],
              "heldout": [{"workers": 2, "mb": 150, "files": 30}]},
             settle_s=6),
    Incident("bad_deploy", "Release 2.3.1 breaks GET /jobs/{id} for payloads without a customer.", "fix",
             frozenset({"rollback_release"}), _bad_deploy,
             {"dev": [{"workers": 1, "note": "release 2.3.1: customer field in job status"},
                      {"workers": 2, "note": "release 2.3.1"}],
              "heldout": [{"workers": 1, "note": "hotfix 2.3.1"}]},
             settle_s=8),
    Incident("expired_credential", "The payments API token expires.", "fix",
             frozenset({"rotate_credential:payments"}), _expire_credential,
             {"dev": [{"workers": 1}, {"workers": 2}], "heldout": [{"workers": 3}]}, settle_s=6),
    Incident("false_alarm_client_errors", "A client sends malformed requests; the service is healthy.",
             "no_action", frozenset(), _client_error_burst,
             {"dev": [{"workers": 1, "requests": 60}, {"workers": 2, "requests": 90}],
              "heldout": [{"workers": 1, "requests": 75}]},
             settle_s=1),
]}


def cases(split: str) -> list[tuple[Incident, dict, str]]:
    """(incident, params, case_id) for a split, in a fixed order."""
    out = []
    for incident in INCIDENTS.values():
        for i, params in enumerate(incident.variants[split]):
            out.append((incident, params, f"{split}-{incident.name}-{i}"))
    return out


def inject(rt: Runtime, incident: Incident, params: dict) -> float:
    """Inject and wait for symptoms to develop. Returns the injection timestamp."""
    t = time.time()
    incident.inject(rt, params)
    time.sleep(incident.settle_s)
    return t
