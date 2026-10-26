"""The agent's store needs a password for every role, and an agent running as its own OS user can
log in as `agent` but can't reach the superuser (agent/store.py § Authentication)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

import psycopg
import pytest

from agent import store, workflow
from sandbox import paths
from sandbox.runtime import Runtime

pytestmark = pytest.mark.skipif(not (paths.PG_BIN / "postgres").exists(), reason="run scripts/setup_postgres.sh")

AGENT_UID = 64000   # no passwd entry needed; must differ from root and from `nobody`, which runs Postgres


@pytest.fixture(scope="module", autouse=True)
def agent_db():
    workflow.ensure()
    yield
    Runtime().agent_db_stop()
    store._ready = False


def test_a_running_trust_auth_store_is_migrated_in_place():
    """Regression: ensure() only ran the setup when the store was down, so a store already running
    with trust auth (created before 2026-10-26) never got its passwords."""
    rt = Runtime()
    hba = paths.AGENT_PG_DIR / "data" / "pg_hba.conf"
    owner = hba.stat()
    hba.write_text("local all all trust\nhost all all 127.0.0.1/32 trust\n")
    os.chown(hba, owner.st_uid, owner.st_gid)
    rt._pg("pg_ctl", "-D", str(paths.AGENT_PG_DIR / "data"), "reload")
    with psycopg.connect(paths.dsn("postgres", "test", port=paths.AGENT_PG_PORT), dbname=store.DB) as conn:
        conn.execute("ALTER ROLE postgres PASSWORD NULL")   # as it was before the change
    store._ready = False
    store.ensure()
    assert "trust" not in hba.read_text()
    test_no_role_gets_in_without_its_password("postgres")
    with store.connect() as conn:
        conn.execute("SELECT count(*) FROM audit_events")


@pytest.mark.parametrize("role", ["postgres", "agent", "audit_owner"])
def test_no_role_gets_in_without_its_password(role):
    """Regression: loopback connections used trust auth, so any local process could log in as the superuser."""
    with pytest.raises(psycopg.OperationalError, match="password"):
        psycopg.connect(paths.dsn(role, "test", port=paths.AGENT_PG_PORT), dbname=store.DB, passfile=os.devnull)


def test_one_roles_password_does_not_open_another():
    conninfo = paths.dsn("postgres", "test", port=paths.AGENT_PG_PORT) + f" password={paths.agent_secret('agent')}"
    with pytest.raises(psycopg.OperationalError, match="password authentication failed"):
        psycopg.connect(conninfo, dbname=store.DB)


def test_password_files_are_private():
    for role in ("postgres", "agent"):
        assert (paths.AGENT_SECRETS / f"{role}.pw").stat().st_mode & 0o777 == 0o600


AS_AGENT_USER = """
import json, psycopg
from agent import store
from agent.executor import Executor
from sandbox import paths

out = {"uid_is_admin": store.is_admin()}
for name, path in [("superuser_password", paths.AGENT_SECRETS / "postgres.pw"), ("store_data_dir", paths.AGENT_PG_DIR / "data")]:
    try:
        path.read_text() if path.is_file() else list(path.iterdir())
        out[name] = "READABLE"
    except PermissionError:
        out[name] = "denied"
try:
    psycopg.connect(paths.dsn("postgres", "test", port=paths.AGENT_PG_PORT), dbname=store.DB, passfile="/dev/null").close()
    out["superuser_login"] = "ALLOWED"
except psycopg.OperationalError:
    out["superuser_login"] = "refused"
ex = Executor("ops-agent", actions={"restart_service": lambda action_id=None, **kw: {"ok": True}})
out["action"] = ex.request("inc-uid-test", "restart_service", {"service": "worker", "instance": "worker-1"}).state
with store.connect() as conn:
    try:
        conn.execute("DROP TABLE audit_events")
        out["drop_audit_log"] = "ALLOWED"
    except psycopg.errors.InsufficientPrivilege:
        out["drop_audit_log"] = "refused"
print(json.dumps(out))
"""


@pytest.mark.skipif(os.geteuid() != 0 or not shutil.which("setpriv"), reason="needs root to switch to another uid")
def test_agent_as_its_own_os_user_can_act_but_cannot_reach_the_superuser():
    secret = paths.AGENT_SECRETS / "agent.pw"
    owner = secret.stat().st_uid
    os.chown(secret, AGENT_UID, -1)   # what AGENT_OS_UID does at setup
    groups = ",".join(str(g) for g in paths.traverse_groups(paths.ROOT)) or str(AGENT_UID)
    try:
        run = subprocess.run(["setpriv", f"--reuid={AGENT_UID}", f"--regid={AGENT_UID}", f"--groups={groups}",
                              sys.executable, "-c", AS_AGENT_USER], cwd=paths.ROOT, capture_output=True, text=True,
                             env={**os.environ, "HOME": "/nonexistent", "PYTHONPATH": str(paths.ROOT),
                                  "PYTHONDONTWRITEBYTECODE": "1"}, timeout=60)
    finally:
        os.chown(secret, owner, -1)
    assert run.returncode == 0, run.stderr[-2000:]
    assert json.loads(run.stdout.strip().splitlines()[-1]) == {
        "uid_is_admin": False, "superuser_password": "denied", "store_data_dir": "denied",
        "superuser_login": "refused", "action": "succeeded", "drop_audit_log": "refused"}
