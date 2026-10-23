"""Jobs API: accepts jobs into a Postgres-backed queue and reports their status.

Release 2.3.1 contains a deliberate bug (GET /jobs/{id} assumes every payload has `customer`) used
by the bad-deploy incident. /health is liveness only, as in many real services.
"""

from __future__ import annotations

import os
import time
import traceback
from collections import Counter

import psycopg
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from psycopg.types.json import Jsonb

from sandbox import paths, state
from target_app.common import INSTANCE, JsonLog, Window, p95

RELEASE = os.environ.get("APP_RELEASE", state.STABLE_RELEASE)
CONFIG = state.current_config()["api"]
log = JsonLog()
window = Window()
app = FastAPI()
_spool_cache = {"at": 0.0, "mb": 0.0}

log.info("api starting", release=RELEASE, db_port=CONFIG["db_port"])


async def db() -> psycopg.AsyncConnection:
    return await psycopg.AsyncConnection.connect(paths.dsn("app", "api", port=CONFIG["db_port"]), autocommit=True)


def spool_usage_mb() -> float:
    if time.time() - _spool_cache["at"] > 1.0:
        total = sum(p.stat().st_size for p in paths.SPOOL.iterdir() if p.is_file()) if paths.SPOOL.exists() else 0
        _spool_cache.update(at=time.time(), mb=total / 2**20)
    return _spool_cache["mb"]


@app.middleware("http")
async def record(request: Request, call_next):
    t0 = time.perf_counter()
    route = "POST /jobs" if request.method == "POST" else ("GET /jobs/{id}" if request.url.path.startswith("/jobs/") else None)
    try:
        response = await call_next(request)
    except Exception as e:  # unhandled: log with traceback, return 500
        log.error("unhandled exception", route=route, error=f"{type(e).__name__}: {e}",
                  traceback=traceback.format_exc(limit=4))
        response = JSONResponse({"error": "internal_error"}, status_code=500)
    if route:
        window.add(route=route, status=response.status_code, ms=(time.perf_counter() - t0) * 1000)
    return response


@app.post("/jobs")
async def create_job(request: Request):
    try:
        body = await request.json()
    except ValueError:
        body = None
    if not isinstance(body, dict) or not isinstance(body.get("kind"), str) or not isinstance(body.get("payload"), dict):
        log.warning("rejected invalid job request")
        return JSONResponse({"error": "invalid_request"}, status_code=400)
    usage = spool_usage_mb()
    if usage > CONFIG["spool_quota_mb"]:
        log.error("spool quota exceeded", usage_mb=round(usage, 1), quota_mb=CONFIG["spool_quota_mb"],
                  spool_dir=str(paths.SPOOL))
        return JSONResponse({"error": "insufficient_storage"}, status_code=507)
    try:
        conn = await db()
        async with conn:
            row = await (await conn.execute(
                "INSERT INTO jobs (kind, payload, probe) VALUES (%s, %s, %s) RETURNING id",
                (body["kind"], Jsonb(body["payload"]), bool(body.get("probe"))))).fetchone()
    except psycopg.OperationalError as e:
        log.error("database error", route="POST /jobs", error=str(e).strip().splitlines()[0])
        return JSONResponse({"error": "database_unavailable"}, status_code=500)
    return JSONResponse({"id": row[0]}, status_code=201)


@app.get("/jobs/{job_id}")
async def get_job(job_id: int):
    try:
        conn = await db()
        async with conn:
            row = await (await conn.execute(
                "SELECT id, kind, payload, status, error FROM jobs WHERE id = %s", (job_id,))).fetchone()
    except psycopg.OperationalError as e:
        log.error("database error", route="GET /jobs/{id}", error=str(e).strip().splitlines()[0])
        return JSONResponse({"error": "database_unavailable"}, status_code=500)
    if row is None:
        return JSONResponse({"error": "not_found"}, status_code=404)
    job = {"id": row[0], "kind": row[1], "status": row[3], "error": row[4]}
    if RELEASE == "2.3.1":
        job["customer"] = row[2]["customer"]  # bug shipped in 2.3.1: not every payload has a customer
    return job


@app.get("/health")
async def health():
    return {"status": "ok", "instance": INSTANCE, "release": RELEASE}


@app.get("/metrics")
async def metrics():
    events = window.events()
    by_status = Counter(str(e["status"]) for e in events)
    by_route = Counter(f'{e["route"]} {e["status"]}' for e in events)
    return {"window_s": window.horizon_s, "requests": len(events), "by_status": dict(by_status),
            "by_route_status": dict(by_route), "latency_p95_ms": p95([e["ms"] for e in events]),
            "release": RELEASE}
