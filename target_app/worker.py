"""Job worker: claims pending jobs, charges them through the payments dependency, records results.

Writes a heartbeat row every second from its main loop. Its /health endpoint is liveness only and
keeps answering even if the loop is stuck, which is why a hung worker looks healthy from outside.
"""

from __future__ import annotations

import asyncio
import time
from collections import Counter
from contextlib import asynccontextmanager

import httpx
import psycopg
from fastapi import FastAPI

from sandbox import paths, state
from target_app.common import INSTANCE, JsonLog, Window, p95

CONFIG = state.current_config()["worker"]
log = JsonLog()
window = Window()
counters = {"jobs_done": 0, "jobs_failed": 0}
last_error: dict = {}


async def db() -> psycopg.AsyncConnection:
    return await psycopg.AsyncConnection.connect(paths.dsn("app", f"worker:{INSTANCE}"), autocommit=True)


async def heartbeat() -> None:
    conn = await db()
    async with conn:
        await conn.execute(
            "INSERT INTO worker_heartbeats (worker_id, beat_at, jobs_done) VALUES (%s, now(), %s) "
            "ON CONFLICT (worker_id) DO UPDATE SET beat_at = now(), jobs_done = EXCLUDED.jobs_done",
            (INSTANCE, counters["jobs_done"]))


async def claim() -> tuple | None:
    conn = await db()
    async with conn:
        return await (await conn.execute(
            "UPDATE jobs SET status = 'running', started_at = now(), worker_id = %s, attempts = attempts + 1 "
            "WHERE id = (SELECT id FROM jobs WHERE status = 'pending' ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1) "
            "RETURNING id, kind, payload", (INSTANCE,))).fetchone()


async def finish(job_id: int, ok: bool, error: str | None) -> None:
    conn = await db()
    async with conn:
        await conn.execute("UPDATE jobs SET status = %s, error = %s, finished_at = now() WHERE id = %s",
                           ("done" if ok else "failed", error, job_id))


async def process(client: httpx.AsyncClient, job: tuple) -> None:
    job_id, kind, payload = job
    token = state.read_secret("payments_token")  # read per call, so a rotated token applies at once
    t0 = time.perf_counter()
    try:
        r = await client.post(f"{CONFIG['payments_url']}/charge", json={"job_id": job_id, "amount": payload.get("amount", 0)},
                              headers={"Authorization": f"Bearer {token}"}, timeout=CONFIG["payments_timeout_s"])
        status, error = r.status_code, None if r.status_code == 200 else f"payments returned {r.status_code}: {r.text[:120]}"
    except httpx.HTTPError as e:
        status, error = "error", f"payments call failed: {type(e).__name__}"
    window.add(dep_ms=(time.perf_counter() - t0) * 1000, dep_status=str(status))
    await finish(job_id, error is None, error)
    if error:
        counters["jobs_failed"] += 1
        last_error.update(ts=time.time(), error=error)
        log.error("job failed", job_id=job_id, error=error)
    else:
        counters["jobs_done"] += 1


async def run_loop() -> None:
    hang_flag = paths.FAULTS / f"worker_hang_{INSTANCE}"
    last_beat = 0.0
    async with httpx.AsyncClient() as client:
        while True:
            if hang_flag.exists():  # fault injection: a deadlock-like stall, no log line, no crash
                hang_flag.unlink()
                await asyncio.Event().wait()
            try:
                if time.monotonic() - last_beat >= 1.0:
                    await heartbeat()
                    last_beat = time.monotonic()
                job = await claim()
                if job is None:
                    await asyncio.sleep(0.1)
                    continue
                await process(client, job)
            except psycopg.OperationalError as e:
                msg = str(e).strip().splitlines()[0]
                last_error.update(ts=time.time(), error=msg)
                log.error("database error", error=msg)
                await asyncio.sleep(0.5)


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("worker starting")
    task = asyncio.create_task(run_loop())
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok", "instance": INSTANCE}


@app.get("/metrics")
async def metrics():
    events = window.events()
    return {"window_s": window.horizon_s, "calls": len(events),
            "dep_latency_p95_ms": p95([e["dep_ms"] for e in events]),
            "dep_status": dict(Counter(e["dep_status"] for e in events)),
            "jobs_done_total": counters["jobs_done"], "jobs_failed_total": counters["jobs_failed"],
            "last_error": last_error or None}
