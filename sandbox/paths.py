"""Filesystem layout and ports for the sandbox. Everything lives under .run/ (gitignored)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUN = Path(os.environ.get("SANDBOX_RUN_DIR", ROOT / ".run"))
LOGS = RUN / "logs"
PIDS = RUN / "pids"
STATE = RUN / "state"
SECRETS = STATE / "secrets"
FAULTS = RUN / "faults"
SPOOL = RUN / "spool"
PG_DIR = RUN / "pg"
# The agent's own state (actions, audit) lives in a separate PostgreSQL instance, outside the
# sandbox's failure domain: an incident that exhausts or breaks the app database must not take
# away the agent's audit trail or its ability to record approvals.
AGENT_PG_DIR = RUN / "agent-pg"
# Passwords for the agent's store, one file per role (sandbox/runtime.py::agent_db_start). The
# superuser's stays with the OS user that runs setup; the agent role's goes to $AGENT_OS_UID.
AGENT_SECRETS = RUN / "agent-secrets"

def _pg_bin() -> Path:
    """PostgreSQL binaries: $PG_BIN if set, else the user-space copy from scripts/setup_postgres.sh
    (Linux), else whatever `pg_config` reports (e.g. Homebrew's postgresql@16 on macOS)."""
    if os.environ.get("PG_BIN"):
        return Path(os.environ["PG_BIN"])
    local = ROOT / ".tools/pg/usr/lib/postgresql/16/bin"
    if (local / "postgres").exists():
        return local
    pg_config = shutil.which("pg_config")
    if pg_config:
        return Path(subprocess.run([pg_config, "--bindir"], capture_output=True, text=True).stdout.strip())
    return local


PG_BIN = _pg_bin()
PG_LIB = ROOT / ".tools/pg/usr/lib/aarch64-linux-gnu"  # only used with the Linux user-space copy
# macOS has no C.UTF-8 locale; plain C (with UTF8 encoding) behaves the same for this sandbox.
PG_LOCALE = "C" if sys.platform == "darwin" else "C.UTF-8"

# Must stay below the kernel ephemeral range (32768-60999 here): ports inside it can be taken by
# outgoing connections, which made a worker fail to bind during development.
PORT_BASE = int(os.environ.get("SANDBOX_PORT_BASE", "18000"))
PG_PORT = PORT_BASE + 432
AGENT_PG_PORT = PORT_BASE + 433
API_PORT = PORT_BASE + 80
PAYMENTS_PORT = PORT_BASE + 70
WORKER_PORT_BASE = PORT_BASE + 90  # worker-1 -> +91, worker-2 -> +92, ...
PG_MAX_CONNECTIONS = 40

API_URL = f"http://127.0.0.1:{API_PORT}"
PAYMENTS_URL = f"http://127.0.0.1:{PAYMENTS_PORT}"


def dsn(user: str, application_name: str, port: int = PG_PORT) -> str:
    # The sandbox's own database uses trust auth on loopback: it is the system under repair, and the
    # agent's read tools are meant to see all of it. The agent's store needs a password (agent_store_dsn).
    return f"host=127.0.0.1 port={port} dbname=ops user={user} application_name={application_name} connect_timeout=2"


def agent_secret(role: str) -> str:
    """Raises PermissionError when this OS user isn't meant to act as `role` (e.g. the agent user
    reading the superuser's password)."""
    return (AGENT_SECRETS / f"{role}.pw").read_text().strip()


def agent_store_dsn(user: str, application_name: str) -> str:
    return dsn(user, application_name, port=AGENT_PG_PORT) + f" password={agent_secret(user)}"


def traverse_groups(path: Path) -> list[int]:
    """Groups of the ancestors of `path` that other users can't traverse: what a process running as
    another user (Postgres as `nobody`, a separate agent user) needs to reach it, without changing
    any permission on disk."""
    return sorted({p.stat().st_gid for p in [path, *path.parents] if p.exists() and not p.stat().st_mode & 0o001})


def ensure_dirs() -> None:
    for d in (LOGS, PIDS, STATE, SECRETS, FAULTS, SPOOL):
        d.mkdir(parents=True, exist_ok=True)
