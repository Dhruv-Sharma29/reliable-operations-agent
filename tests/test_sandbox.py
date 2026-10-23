"""Integration tests against a real sandbox on separate ports and a separate run directory, so
they never touch an evaluation in progress. Needs scripts/setup_postgres.sh to have run."""

from __future__ import annotations

import os
import time

import pytest

from agent.tools import act, read
from eval.verify import verify
from incidents.catalog import INCIDENTS
from ops.alerts import evaluate
from sandbox import paths, state
from sandbox.runtime import Runtime

pytestmark = pytest.mark.skipif(not (paths.PG_BIN / "postgres").exists(), reason="run scripts/setup_postgres.sh")


@pytest.fixture(scope="module")
def rt():
    runtime = Runtime()
    yield runtime
    runtime.down()
    runtime.postgres_stop()


@pytest.fixture
def clean(rt):
    rt.reset(workers=1)
    time.sleep(2)
    return rt


def test_reset_gives_a_healthy_sandbox_with_no_alerts(clean):
    assert verify(timeout_s=15)["healthy"]
    assert evaluate() == []
    assert os.environ["SANDBOX_PORT_BASE"] == "17000" and paths.API_PORT == 17080


def test_crash_is_detected_and_restart_recovers(clean):
    clean.kill_instance("worker-1")
    result = verify(timeout_s=2)
    assert not result["healthy"] and "not running" in result["reason"]
    assert act.restart_service("worker", "worker-1")["ok"]
    assert verify(timeout_s=15)["healthy"]


def test_hung_worker_passes_liveness_but_heartbeat_goes_stale(clean):
    (paths.FAULTS / "worker_hang_worker-1").touch()
    time.sleep(7)
    worker = next(s for s in read.service_status() if s["instance"] == "worker-1")
    assert worker["running"] and worker["health"] == "ok"
    beat = next(h for h in read.worker_heartbeats() if h["worker_id"] == "worker-1")
    assert beat["age_s"] > 5
    assert any(a["alert"] == "WorkerHeartbeatStale" for a in evaluate())


def test_investigation_tools_still_work_when_app_connections_are_exhausted(clean):
    INCIDENTS["db_connection_exhaustion"].inject(clean, {"keep_free": 0})
    time.sleep(3)
    conns = read.db_connections()  # superuser reserved slots
    exporter = sum(r["count"] for r in conns["by_application"] if r["application"] == "report-exporter")
    assert exporter >= conns["max_connections"] // 2
    assert not verify(timeout_s=2)["healthy"]
    act.restart_service("report-exporter")
    assert verify(timeout_s=20)["healthy"]


def test_config_rollback_restores_previous_version(clean):
    INCIDENTS["bad_config"].inject(clean, {"bad_port": paths.PG_PORT + 1, "author": "t", "note": "t"})
    assert read.config_history(last=1)[-1]["changed"] == {"api.db_port": {"from": paths.PG_PORT, "to": paths.PG_PORT + 1}}
    assert act.rollback_config()["ok"]
    assert state.current_config()["api"]["db_port"] == paths.PG_PORT
    assert verify(timeout_s=15)["healthy"]


# Regression tests for verifier gaps found by the no-op control (see eval/verify.py docstring).

def test_half_capacity_is_not_healthy(rt):
    rt.reset(workers=2)
    time.sleep(2)
    (paths.FAULTS / "worker_hang_worker-1").touch()
    time.sleep(7)
    result = verify(timeout_s=3)
    assert not result["healthy"] and "worker-1" in result["reason"]


def test_bad_deploy_is_visible_to_the_verifier(clean):
    INCIDENTS["bad_deploy"].inject(clean, {"note": "t"})
    result = verify(timeout_s=3)
    assert not result["healthy"] and "GET /jobs/{id} -> 500" in result["reason"]
    assert act.rollback_release()["ok"]
    assert verify(timeout_s=15)["healthy"]


def _pending() -> int:
    import psycopg
    with psycopg.connect(paths.dsn("postgres", "test"), autocommit=True) as c:
        return c.execute("SELECT count(*) FROM jobs WHERE status='pending'").fetchone()[0]


def test_backlog_keeps_growing_without_a_fix_and_reset_stops_the_import(clean):
    """The burst-only backlog drained by itself in ~100 s, rewarding slow responders. The sustained
    import must keep it growing with one worker, and must not survive a reset into the next case."""
    from incidents.catalog import inject
    inject(clean, INCIDENTS["queue_backlog"], {"workers": 1, "jobs": 200, "rate": 60})
    first = _pending()
    time.sleep(6)
    assert _pending() > first + 50            # arrival above one worker's capacity: still growing
    assert clean.is_running("bulk-import")
    assert "bulk-import" not in {r["instance"] for r in clean.status()}   # agent-visible status unchanged
    clean.reset(workers=1)
    assert not clean.is_running("bulk-import")
    time.sleep(3)
    assert _pending() < 30                    # only background traffic remains
