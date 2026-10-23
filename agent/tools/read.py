"""Read-only investigation tools. The scripted baseline uses these now; the LLM agent gets the
same functions as tools in M2, so both see exactly the same evidence.

Every function returns JSON-serialisable data and never changes system state.
"""

from __future__ import annotations

import json
import time

import httpx
import psycopg

from sandbox import paths, state
from sandbox.runtime import Runtime

RUNBOOKS = paths.ROOT / "runbooks" / "RUNBOOKS.md"


def _admin():
    # The superuser role keeps reserved connection slots, so investigation still works when the
    # application role has exhausted the pool.
    return psycopg.connect(paths.dsn("postgres", "ops-tools"), autocommit=True)


def service_status() -> list[dict]:
    """Process state of every component instance, plus its /health answer if it serves HTTP."""
    rows = Runtime().status()
    for row in rows:
        if row["running"] and row["port"]:
            try:
                r = httpx.get(f"http://127.0.0.1:{row['port']}/health", timeout=1)
                row["health"] = r.json().get("status") if r.status_code == 200 else f"http_{r.status_code}"
            except httpx.HTTPError as e:
                row["health"] = f"unreachable: {type(e).__name__}"
    return rows


def api_metrics() -> dict:
    try:
        return httpx.get(f"{paths.API_URL}/metrics", timeout=2).json()
    except httpx.HTTPError as e:
        return {"error": f"unreachable: {type(e).__name__}"}


def worker_metrics() -> dict:
    out = {}
    for inst in Runtime().instances("worker"):
        port = paths.WORKER_PORT_BASE + int(inst.split("-")[1])
        try:
            out[inst] = httpx.get(f"http://127.0.0.1:{port}/metrics", timeout=2).json()
        except httpx.HTTPError as e:
            out[inst] = {"error": f"unreachable: {type(e).__name__}"}
    return out


def queue_stats() -> dict:
    with _admin() as conn:
        pending, oldest = conn.execute(
            "SELECT count(*), extract(epoch FROM now() - min(created_at)) FROM jobs WHERE status = 'pending'").fetchone()
        done_60, failed_60, p95 = conn.execute(
            "SELECT count(*) FILTER (WHERE status = 'done'), count(*) FILTER (WHERE status = 'failed'), "
            "percentile_disc(0.95) WITHIN GROUP (ORDER BY extract(epoch FROM finished_at - started_at)) "
            "FROM jobs WHERE finished_at > now() - interval '60 seconds'").fetchone()
    return {"pending": pending, "oldest_pending_age_s": round(float(oldest), 1) if oldest is not None else None,
            "done_last_60s": done_60, "failed_last_60s": failed_60,
            "job_duration_p95_s": round(float(p95), 3) if p95 is not None else None}


def worker_heartbeats() -> list[dict]:
    with _admin() as conn:
        rows = conn.execute("SELECT worker_id, extract(epoch FROM now() - beat_at), jobs_done "
                            "FROM worker_heartbeats ORDER BY worker_id").fetchall()
    expected = set(Runtime().instances("worker"))
    return [{"worker_id": w, "age_s": round(float(age), 1), "jobs_done": n, "expected": w in expected}
            for w, age, n in rows]


def db_connections() -> dict:
    with _admin() as conn:
        rows = conn.execute("SELECT coalesce(nullif(application_name, ''), '(none)'), state, count(*) "
                            "FROM pg_stat_activity WHERE datname = 'ops' GROUP BY 1, 2 ORDER BY 3 DESC").fetchall()
        max_conn = int(conn.execute("SHOW max_connections").fetchone()[0])
    return {"max_connections": max_conn, "total": sum(r[2] for r in rows),
            "by_application": [{"application": a, "state": s, "count": c} for a, s, c in rows]}


def search_logs(instance: str, contains: str | None = None, level: str | None = None, last: int = 50) -> list[dict]:
    """Most recent matching log records (JSON lines) for one component instance."""
    path = paths.LOGS / f"{instance}.log"
    if not path.exists():
        return []
    matches = []
    for line in path.read_text(errors="replace").splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if level and rec.get("level") != level:
            continue
        if contains and contains.lower() not in line.lower():
            continue
        matches.append(rec)
    return matches[-last:]


def config_history(last: int = 5) -> list[dict]:
    """Recent config versions with the keys that changed from the previous version."""
    history = state.config_history()
    out = []
    for i, entry in enumerate(history):
        prev = history[i - 1]["config"] if i else {}
        changed = {f"{section}.{key}": {"from": prev.get(section, {}).get(key), "to": value}
                   for section, values in entry["config"].items() for key, value in values.items()
                   if prev.get(section, {}).get(key) != value} if i else {}
        out.append({"version": entry["version"], "age_s": round(time.time() - entry["ts"], 1),
                    "author": entry["author"], "note": entry["note"], "changed": changed})
    return out[-last:]


def deploy_history(last: int = 5) -> list[dict]:
    return [{"release": d["release"], "service": d["service"], "age_s": round(time.time() - d["ts"], 1),
             "author": d["author"], "note": d["note"]} for d in state.deploy_history()[-last:]]


def runbook(topic: str | None = None) -> str:
    text = RUNBOOKS.read_text()
    if not topic:
        return text
    sections = text.split("\n## ")
    hits = [s for s in sections if topic.lower() in s.lower()]
    return ("## " + "\n## ".join(hits)) if hits else f"No runbook section mentions '{topic}'."
