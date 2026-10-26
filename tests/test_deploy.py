"""The sandbox and the agent run as their own OS user end to end (sandbox/deploy.py): a real incident
is injected, diagnosed and fixed through the executor, with an approval, and nothing the agent's
user runs can reach the audit store's superuser or stop the store."""

from __future__ import annotations

import json
import os
import pwd
import shutil
import subprocess
import sys
from pathlib import Path

import psycopg
import pytest

from sandbox import paths

RUN_DIR = paths.ROOT / ".run-test-deploy"
PORT_BASE = 16500   # not the tests' 17000 or the development sandbox's 18000
ENV = {**os.environ, "SANDBOX_RUN_DIR": str(RUN_DIR), "SANDBOX_PORT_BASE": str(PORT_BASE)}


def _agent_uid() -> int | None:
    if os.environ.get("DEPLOY_TEST_UID"):
        return int(os.environ["DEPLOY_TEST_UID"])
    users = [p.pw_uid for p in pwd.getpwall() if 1000 <= p.pw_uid < 65534]
    return min(users) if users else None


AGENT_UID = _agent_uid()
pytestmark = [
    pytest.mark.skipif(not (paths.PG_BIN / "postgres").exists(), reason="run scripts/setup_postgres.sh"),
    pytest.mark.skipif(os.geteuid() != 0 or not shutil.which("setpriv"), reason="needs root to hand the sandbox over"),
    pytest.mark.skipif(AGENT_UID is None, reason="needs a non-root account (or DEPLOY_TEST_UID)"),
]


def py(*args: str, as_agent: bool = False, timeout: int = 300) -> subprocess.CompletedProcess:
    cmd = [sys.executable, *args]
    if as_agent:
        cmd = [sys.executable, "-m", "sandbox.deploy", "exec", "--", *cmd]
    return subprocess.run(cmd, cwd=paths.ROOT, env=ENV, capture_output=True, text=True, timeout=timeout)


def uid_of(pid: int) -> int:
    with open(f"/proc/{pid}/status") as f:
        return next(int(line.split()[1]) for line in f if line.startswith("Uid:"))


def postmaster(cluster: str) -> int:
    return int((RUN_DIR / cluster / "data/postmaster.pid").read_text().split()[0])


@pytest.fixture(scope="module")
def deployed():
    shutil.rmtree(RUN_DIR, ignore_errors=True)
    run = py("-m", "sandbox.deploy", "prepare", "--agent-uid", str(AGENT_UID))
    assert run.returncode == 0, run.stderr[-2000:]
    yield json.loads(run.stdout.strip().splitlines()[-1])
    py("-c", "from sandbox.runtime import Runtime; r = Runtime(); r.down(); r.postgres_stop(); r.agent_db_stop()")
    shutil.rmtree(RUN_DIR, ignore_errors=True)


def test_agent_user_fixes_an_incident_that_needs_approval(deployed):
    assert deployed["agent_password_owner"] == AGENT_UID and deployed["superuser_password_owner"] == 0
    run = py("-m", "eval.run", "--responder", "baseline-exec", "--split", "dev", "--only", "bad_deploy",
             "--out-dir", str(RUN_DIR / "results"), as_agent=True, timeout=600)
    assert run.returncode == 0, run.stderr[-2000:]
    out = Path(run.stdout.strip().splitlines()[-1])
    assert out.stat().st_uid == AGENT_UID
    cases = [r for r in map(json.loads, out.read_text().splitlines()) if r["type"] == "case"]
    assert cases and all(c["success"] for c in cases)
    actions = [a for c in cases for a in c["response"]["actions"]]
    assert actions and all(a["result"]["executor_state"] == "succeeded" for a in actions)

    # Everything the agent started runs as the agent's user; the audit store doesn't.
    pids = [json.loads(f.read_text())["pid"] for f in (RUN_DIR / "pids").glob("*.json")]
    assert pids and {uid_of(p) for p in pids} == {AGENT_UID}
    assert uid_of(postmaster("pg")) == AGENT_UID
    assert uid_of(postmaster("agent-pg")) == 65534

    # The audit trail was written by the agent's role, approvals by someone else.
    password = (RUN_DIR / "agent-secrets/postgres.pw").read_text().strip()
    with psycopg.connect(host="127.0.0.1", port=PORT_BASE + 433, dbname="agentstate", user="postgres",
                         password=password) as conn:
        rows = conn.execute("SELECT tool, requested_by, approver, state FROM actions WHERE action_id = ANY(%s)",
                            ([a["result"]["action_id"] for a in actions],)).fetchall()
    assert rows and all(r[1] == "ops-agent" and r[3] == "succeeded" for r in rows)
    assert {r[2] for r in rows if r[0] == "rollback_release"} == {"eval-approver"}


def test_agent_user_cannot_stop_the_audit_store(deployed):
    run = py("-m", "sandbox.runtime", "pg-stop", as_agent=True)
    assert run.returncode == 0, run.stderr[-2000:]
    assert not (RUN_DIR / "pg/data/postmaster.pid").exists()          # its own sandbox database: stopped
    assert uid_of(postmaster("agent-pg")) == 65534                       # the audit store: still up
    kill = py("-c", f"import os, signal; os.kill({postmaster('agent-pg')}, signal.SIGTERM)", as_agent=True)
    assert "PermissionError" in kill.stderr
