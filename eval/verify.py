"""Independent health verification. Judges recovery by observed system state and end-to-end
behaviour, never by anything the responder reports. Used before injection (to prove a clean
start) and after the response (to score recovery).

Healthy means, for two consecutive rounds:
  - every configured api/payments/worker instance is running;
  - probe jobs shaped like real traffic (with and without `customer`) are accepted, readable
    through the API, and completed by a worker;
  - every configured worker has a fresh heartbeat (full capacity, not just "some worker works");
  - the non-probe backlog is bounded.

History: the first version used a single round of probes that always had a `customer` field and
no capacity check. The no-op control showed it scored a half-capacity system (one of two workers
hung) as healthy, and it could not see the 2.3.1 bug, which only affects payloads without
`customer`.
"""

from __future__ import annotations

import time

import httpx
import psycopg

from sandbox import paths
from sandbox.runtime import Runtime

PROBE_PAYLOADS = [{"amount": 1, "customer": "probe"}, {"amount": 2}, {"amount": 3, "customer": "probe"}]
MAX_HEALTHY_BACKLOG = 200
HEARTBEAT_FRESH_S = 5
CONSECUTIVE_ROUNDS = 2


def _probe_round(client: httpx.Client) -> tuple[bool, str]:
    rt = Runtime()
    down = [i for s in ("api", "payments", "worker") for i in rt.instances(s) if not rt.is_running(i)]
    if down:
        return False, f"not running: {down}"
    ids = []
    for payload in PROBE_PAYLOADS:
        r = client.post("/jobs", json={"kind": "probe", "payload": payload, "probe": True})
        if r.status_code != 201:
            return False, f"POST /jobs -> {r.status_code}"
        ids.append(r.json()["id"])
    for job_id in ids:
        r = client.get(f"/jobs/{job_id}")
        if r.status_code != 200:
            return False, f"GET /jobs/{{id}} -> {r.status_code}"
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        with psycopg.connect(paths.dsn("postgres", "verifier"), autocommit=True) as conn:
            statuses = [r[0] for r in conn.execute("SELECT status FROM jobs WHERE id = ANY(%s)", (ids,))]
            pending = conn.execute("SELECT count(*) FROM jobs WHERE status = 'pending' AND NOT probe").fetchone()[0]
            beats = dict(conn.execute("SELECT worker_id, extract(epoch FROM now() - beat_at) FROM worker_heartbeats"))
        if "failed" in statuses:
            return False, "probe job failed"
        if all(s == "done" for s in statuses):
            if pending > MAX_HEALTHY_BACKLOG:
                return False, f"backlog {pending} > {MAX_HEALTHY_BACKLOG}"
            stale = [w for w in rt.instances("worker") if float(beats.get(w, 1e9)) > HEARTBEAT_FRESH_S]
            if stale:
                return False, f"workers not processing (stale heartbeat): {stale}"
            return True, "probes completed at full capacity"
        time.sleep(0.25)
    return False, "probe jobs not completed within 15s"


def verify(timeout_s: float = 30.0) -> dict:
    """Healthy once CONSECUTIVE_ROUNDS probe rounds pass in a row within timeout_s (one extra round
    may finish after the deadline). Returns the last failure reason otherwise."""
    t0 = time.monotonic()
    rounds, streak, reason = 0, 0, ""
    with httpx.Client(base_url=paths.API_URL, timeout=5) as client:
        while True:
            rounds += 1
            try:
                ok, reason = _probe_round(client)
            except (httpx.HTTPError, psycopg.OperationalError) as e:
                ok, reason = False, f"{type(e).__name__}"
            streak = streak + 1 if ok else 0
            healthy = streak >= CONSECUTIVE_ROUNDS
            if healthy or (time.monotonic() - t0 > timeout_s and not ok):
                return {"healthy": healthy, "reason": reason, "rounds": rounds,
                        "elapsed_s": round(time.monotonic() - t0, 1)}
            time.sleep(0.5 if ok else 1.0)
