"""Process runtime for the sandbox: start/stop/restart/scale/kill app components and Postgres.

This plays the role Docker Compose will play once a container runtime is available; the action
tools only use this interface, so swapping the backend later does not change the agent's tools.

Components run as detached sessions with pid files under .run/pids, so they outlive the CLI
invocation that started them (like containers outliving `docker compose up -d`).

    python -m sandbox.runtime up|down|status|reset [--workers N]
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
import psycopg
from psycopg import sql

from . import paths, state

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id          bigserial PRIMARY KEY,
    kind        text NOT NULL,
    payload     jsonb NOT NULL,
    probe       boolean NOT NULL DEFAULT false,
    status      text NOT NULL DEFAULT 'pending',   -- pending | running | done | failed
    attempts    int NOT NULL DEFAULT 0,
    worker_id   text,
    error       text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    started_at  timestamptz,
    finished_at timestamptz
);
CREATE INDEX IF NOT EXISTS jobs_status_id ON jobs (status, id);
CREATE TABLE IF NOT EXISTS worker_heartbeats (
    worker_id text PRIMARY KEY,
    beat_at   timestamptz NOT NULL,
    jobs_done bigint NOT NULL DEFAULT 0
);
"""


@dataclass(frozen=True)
class Spec:
    module: str          # python module run for this component
    http: bool           # served by uvicorn with a /health endpoint
    scalable: bool = False

    def port(self, instance: str) -> int | None:
        if not self.http:
            return None
        if instance.startswith("worker-"):
            return paths.WORKER_PORT_BASE + int(instance.split("-")[1])
        return {"api": paths.API_PORT, "payments": paths.PAYMENTS_PORT}[instance]


SPECS = {
    "payments": Spec("target_app.payments", http=True),
    "api": Spec("target_app.api", http=True),
    "worker": Spec("target_app.worker", http=True, scalable=True),
    "report-exporter": Spec("target_app.report_exporter", http=False),
    "loadgen": Spec("target_app.loadgen", http=False),
    "bulk-import": Spec("target_app.bulk_import", http=False),
}
START_ORDER = ["payments", "api", "worker", "report-exporter", "loadgen"]
FAULT_ONLY = ["bulk-import"]   # started only by fault injection; stopped by down/reset; not in status
REPLICAS_FILE = paths.STATE / "replicas.json"


def service_of(instance: str) -> str:
    return "worker" if instance.startswith("worker-") else instance


# --- process helpers -------------------------------------------------------------------------

def _proc_state(pid: int) -> str:
    """One-letter process state ('R', 'S', 'Z', ...; 'X' = gone). Uses /proc on Linux, ps elsewhere (macOS)."""
    if os.path.isdir("/proc"):
        try:
            with open(f"/proc/{pid}/stat") as f:
                return f.read().split(") ", 1)[1][0]
        except (FileNotFoundError, IndexError):
            return "X"
    out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return out[:1] if out else "X"


def _alive(pid: int) -> bool:
    try:
        os.waitpid(pid, os.WNOHANG)  # reap if it is our own exited child
    except ChildProcessError:
        pass
    return _proc_state(pid) not in ("Z", "X")


def _pidfile(instance: str) -> Path:
    return paths.PIDS / f"{instance}.json"


def _read_pid(instance: str) -> dict | None:
    try:
        return json.loads(_pidfile(instance).read_text())
    except (FileNotFoundError, ValueError):
        return None


class Runtime:
    # --- Postgres --------------------------------------------------------------------------

    def _pg_env_prefix(self, pg_dir: Path) -> list[str]:
        """Postgres refuses to run as root. When root, run it as the owner of the cluster's
        directory: `nobody` for a cluster root created, the agent's user for a sandbox handed to it
        (sandbox/deploy.py). Adds the group of any ancestor directory, of its data or its binaries,
        that is not world-traversable (no permission changes on disk)."""
        if os.geteuid() != 0:
            return []
        owner = pg_dir.stat() if pg_dir.exists() and pg_dir.stat().st_uid != 0 else None
        uid, gid = (owner.st_uid, owner.st_gid) if owner else (65534, 65534)
        groups = set(paths.traverse_groups(pg_dir)) | set(paths.traverse_groups(paths.PG_BIN))
        group_arg = ",".join(str(g) for g in sorted(groups)) or str(gid)
        return ["setpriv", f"--reuid={uid}", f"--regid={gid}", f"--groups={group_arg}"]

    def _pg(self, tool: str, *args: str, pg_dir: Path = paths.PG_DIR, check: bool = True) -> subprocess.CompletedProcess:
        env = {**os.environ, "LD_LIBRARY_PATH": str(paths.PG_LIB)}
        cmd = [*self._pg_env_prefix(pg_dir), str(paths.PG_BIN / tool), *args]
        return subprocess.run(cmd, env=env, capture_output=True, text=True, check=check)

    def _cluster_running(self, pg_dir: Path, port: int) -> bool:
        return self._pg("pg_isready", "-h", "127.0.0.1", "-p", str(port), pg_dir=pg_dir, check=False).returncode == 0

    def _cluster_start(self, pg_dir: Path, port: int, max_connections: int, superuser_password: str | None = None) -> None:
        """With `superuser_password`, a new cluster requires scram-sha-256 for every connection;
        without it, loopback connections are trusted (the sandbox's own database)."""
        if not (paths.PG_BIN / "postgres").exists():
            sys.exit(f"Postgres binaries not found at {paths.PG_BIN}: on Linux run scripts/setup_postgres.sh; "
                     "on macOS `brew install postgresql@16` (or set PG_BIN)")
        data = pg_dir / "data"
        if not data.exists():
            pg_dir.mkdir(parents=True, exist_ok=True)
            if os.geteuid() == 0:
                os.chown(pg_dir, 65534, 65534)
            auth = ["--auth=trust"]
            if superuser_password:
                pwfile = pg_dir / "initdb.pw"   # initdb runs as the cluster's user, so it must be able to read it
                pwfile.write_text(superuser_password)
                pwfile.chmod(0o600)
                if os.geteuid() == 0:
                    os.chown(pwfile, 65534, 65534)
                auth = ["--auth=scram-sha-256", f"--pwfile={pwfile}"]
            try:
                self._pg("initdb", "-D", str(data), "-U", "postgres", *auth, "-E", "UTF8", f"--locale={paths.PG_LOCALE}",
                         pg_dir=pg_dir)
            finally:
                (pg_dir / "initdb.pw").unlink(missing_ok=True)
        if not self._cluster_running(pg_dir, port):
            opts = (f"-p {port} -k {pg_dir} -c listen_addresses=127.0.0.1 "
                    f"-c max_connections={max_connections} -c superuser_reserved_connections=3")
            self._pg("pg_ctl", "-D", str(data), "-l", str(pg_dir / "postgres.log"), "-o", opts, "-w", "start", pg_dir=pg_dir)

    def _cluster_stop(self, pg_dir: Path, port: int) -> None:
        if self._cluster_running(pg_dir, port):
            self._pg("pg_ctl", "-D", str(pg_dir / "data"), "-m", "fast", "-w", "stop", pg_dir=pg_dir)

    def postgres_running(self) -> bool:
        return self._cluster_running(paths.PG_DIR, paths.PG_PORT)

    def postgres_start(self) -> None:
        self._cluster_start(paths.PG_DIR, paths.PG_PORT, paths.PG_MAX_CONNECTIONS)
        with psycopg.connect(paths.dsn("postgres", "sandbox", ), dbname="postgres", autocommit=True) as conn:
            if not conn.execute("SELECT 1 FROM pg_roles WHERE rolname='app'").fetchone():
                conn.execute("CREATE ROLE app LOGIN")
            if not conn.execute("SELECT 1 FROM pg_database WHERE datname='ops'").fetchone():
                conn.execute("CREATE DATABASE ops OWNER app")
        with psycopg.connect(paths.dsn("app", "sandbox"), autocommit=True) as conn:
            conn.execute(SCHEMA)

    def postgres_stop(self) -> None:
        self._cluster_stop(paths.PG_DIR, paths.PG_PORT)

    # --- agent state database (not part of the sandbox: reset and incidents never touch it) ---

    def agent_db_running(self) -> bool:
        return self._cluster_running(paths.AGENT_PG_DIR, paths.AGENT_PG_PORT)

    def _agent_secrets(self) -> None:
        """One random password per role, each in a 0600 file. The superuser's belongs to the user
        running setup; the agent role's to $AGENT_OS_UID (default: the same user). Run the agent as a
        separate OS user and it can log in as `agent` but can't read the superuser's password."""
        d = paths.AGENT_SECRETS
        d.mkdir(parents=True, exist_ok=True)
        d.chmod(0o711)   # others may open a file they own, but not list the directory
        for role in ("postgres", "agent"):
            f = d / f"{role}.pw"
            if not f.exists():
                fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                os.write(fd, secrets.token_urlsafe(32).encode())
                os.close(fd)
        uid = int(os.environ.get("AGENT_OS_UID", os.geteuid()))
        if (d / "agent.pw").stat().st_uid != uid:
            os.chown(d / "agent.pw", uid, -1)

    def _require_password_auth(self, pg_dir: Path, port: int) -> None:
        """Migrate a store created with trust auth (before 2026-10-26): set the superuser's password
        while trust still lets us in, then require scram-sha-256 for every connection."""
        hba = pg_dir / "data" / "pg_hba.conf"
        rules = [line.split() for line in hba.read_text().splitlines() if line.strip() and not line.lstrip().startswith("#")]
        if not any(r[-1] == "trust" for r in rules):
            return
        with psycopg.connect(paths.dsn("postgres", "agent-setup", port=port), dbname="postgres", autocommit=True) as conn:
            conn.execute(sql.SQL("ALTER ROLE postgres PASSWORD {}").format(paths.agent_secret("postgres")))
        owner = hba.stat()
        hba.write_text("local all all scram-sha-256\nhost all all 127.0.0.1/32 scram-sha-256\n"
                       "host all all ::1/128 scram-sha-256\n")
        os.chown(hba, owner.st_uid, owner.st_gid)
        self._pg("pg_ctl", "-D", str(pg_dir / "data"), "reload", pg_dir=pg_dir)

    def agent_db_start(self) -> None:
        self._agent_secrets()
        self._cluster_start(paths.AGENT_PG_DIR, paths.AGENT_PG_PORT, 20, superuser_password=paths.agent_secret("postgres"))
        self._require_password_auth(paths.AGENT_PG_DIR, paths.AGENT_PG_PORT)
        with psycopg.connect(paths.agent_store_dsn("postgres", "agent-setup"), dbname="postgres", autocommit=True) as conn:
            if not conn.execute("SELECT 1 FROM pg_roles WHERE rolname='agent'").fetchone():
                conn.execute("CREATE ROLE agent LOGIN")
            conn.execute(sql.SQL("ALTER ROLE agent PASSWORD {}").format(paths.agent_secret("agent")))
            # Owns the agent store; can't log in, so the agent process can't act as it (agent/store.py).
            if not conn.execute("SELECT 1 FROM pg_roles WHERE rolname='audit_owner'").fetchone():
                conn.execute("CREATE ROLE audit_owner NOLOGIN")
            if not conn.execute("SELECT 1 FROM pg_database WHERE datname='agentstate'").fetchone():
                conn.execute("CREATE DATABASE agentstate OWNER audit_owner")

    def agent_db_stop(self) -> None:
        self._cluster_stop(paths.AGENT_PG_DIR, paths.AGENT_PG_PORT)

    def reset_database(self) -> None:
        with psycopg.connect(paths.dsn("postgres", "sandbox"), autocommit=True) as conn:
            conn.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = 'app'")
            conn.execute("TRUNCATE jobs, worker_heartbeats RESTART IDENTITY")

    # --- app components --------------------------------------------------------------------

    def desired_replicas(self, service: str) -> int:
        try:
            return json.loads(REPLICAS_FILE.read_text()).get(service, 1)
        except (FileNotFoundError, ValueError):
            return 1

    def _set_replicas(self, service: str, n: int) -> None:
        try:
            data = json.loads(REPLICAS_FILE.read_text())
        except (FileNotFoundError, ValueError):
            data = {}
        data[service] = n
        REPLICAS_FILE.write_text(json.dumps(data))

    def instances(self, service: str) -> list[str]:
        if SPECS[service].scalable:
            return [f"{service}-{i}" for i in range(1, self.desired_replicas(service) + 1)]
        return [service]

    def is_running(self, instance: str) -> bool:
        info = _read_pid(instance)
        return bool(info) and _alive(info["pid"])

    def start_instance(self, instance: str, wait: bool = True, action_id: str | None = None) -> None:
        """`action_id`: the agent action that started this process, recorded in its pid file so a
        repeated restart under the same action can see it already happened."""
        if self.is_running(instance):
            return
        spec = SPECS[service_of(instance)]
        port = spec.port(instance)
        env = {**os.environ, "SANDBOX_INSTANCE": instance, "APP_RELEASE": state.current_release(),
               "PYTHONUNBUFFERED": "1"}
        if spec.http:
            cmd = [sys.executable, "-m", "uvicorn", f"{spec.module}:app", "--host", "127.0.0.1",
                   "--port", str(port), "--log-level", "warning", "--no-access-log"]
        else:
            cmd = [sys.executable, "-m", spec.module]
        out = open(paths.LOGS / f"{instance}.out", "a")
        proc = subprocess.Popen(cmd, cwd=paths.ROOT, env=env, stdout=out, stderr=subprocess.STDOUT,
                                start_new_session=True)
        _pidfile(instance).write_text(json.dumps({"pid": proc.pid, "started_at": time.time(), "port": port,
                                                  **({"action_id": action_id} if action_id else {})}))
        if wait and spec.http:
            self._wait_healthy(instance, port)

    def _wait_healthy(self, instance: str, port: int, timeout_s: float = 15) -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                if httpx.get(f"http://127.0.0.1:{port}/health", timeout=1).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            if not self.is_running(instance):
                break
            time.sleep(0.1)
        raise RuntimeError(f"{instance} did not become healthy; see {paths.LOGS / (instance + '.out')}")

    def stop_instance(self, instance: str, grace_s: float = 3.0) -> None:
        info = _read_pid(instance)
        if info and _alive(info["pid"]):
            try:
                os.killpg(info["pid"], signal.SIGTERM)
            except ProcessLookupError:
                pass
            deadline = time.monotonic() + grace_s
            while _alive(info["pid"]) and time.monotonic() < deadline:
                time.sleep(0.05)
            if _alive(info["pid"]):
                os.killpg(info["pid"], signal.SIGKILL)
                while _alive(info["pid"]):
                    time.sleep(0.02)
        _pidfile(instance).unlink(missing_ok=True)

    def kill_instance(self, instance: str) -> None:
        """Fault injection only: SIGKILL without cleanup, leaving a stale pid file like a crash."""
        info = _read_pid(instance)
        if info and _alive(info["pid"]):
            os.killpg(info["pid"], signal.SIGKILL)
            while _alive(info["pid"]):
                time.sleep(0.02)

    def start(self, service: str) -> None:
        for instance in self.instances(service):
            self.start_instance(instance)

    def stop(self, service: str) -> None:
        for instance in self._known_instances(service):
            self.stop_instance(instance)

    def restart(self, service: str, instance: str | None = None, action_id: str | None = None) -> list[str]:
        """Restart targets; with `action_id`, skip any instance this action already restarted and
        that is still running (idempotent under retry). Returns the instances actually restarted."""
        targets = [instance] if instance else self.instances(service)
        done = []
        for inst in targets:
            if action_id and self.started_by(inst) == action_id and self.is_running(inst):
                continue
            self.stop_instance(inst)
            self.start_instance(inst, action_id=action_id)
            done.append(inst)
        return done

    def started_by(self, instance: str) -> str | None:
        return (_read_pid(instance) or {}).get("action_id")

    def scale(self, service: str, replicas: int) -> None:
        if not SPECS[service].scalable:
            raise ValueError(f"{service} is not scalable")
        current = self.instances(service)
        self._set_replicas(service, replicas)
        wanted = self.instances(service)
        for inst in current:
            if inst not in wanted:
                self.stop_instance(inst)
        for inst in wanted:
            self.start_instance(inst)

    def _known_instances(self, service: str) -> list[str]:
        prefix = f"{service}-" if SPECS[service].scalable else service
        found = {p.stem for p in paths.PIDS.glob("*.json")
                 if p.stem == service or (SPECS[service].scalable and p.stem.startswith(prefix))}
        return sorted(found | set(self.instances(service)))

    def status(self) -> list[dict]:
        rows = []
        for service in START_ORDER:
            for instance in self._known_instances(service):
                info = _read_pid(instance) or {}
                running = bool(info) and _alive(info["pid"])
                rows.append({"instance": instance, "service": service, "running": running,
                             "pid_file": bool(info), "port": info.get("port"),
                             "uptime_s": round(time.time() - info["started_at"], 1) if running else None})
        return rows

    # --- whole sandbox ---------------------------------------------------------------------

    def down(self) -> None:
        for service in FAULT_ONLY + list(reversed(START_ORDER)):
            self.stop(service)

    def up(self) -> None:
        paths.ensure_dirs()
        self.postgres_start()
        if not state.CONFIG_HISTORY.exists():
            state.reset()
        for service in START_ORDER:
            self.start(service)

    def reset(self, workers: int = 1) -> None:
        """Return to the known-good baseline: every evaluation case starts from here."""
        self.down()
        paths.ensure_dirs()
        self.postgres_start()
        self.reset_database()
        state.reset()
        for f in list(paths.FAULTS.iterdir()) + list(paths.SPOOL.iterdir()) + list(paths.LOGS.glob("*.log")):
            f.unlink()
        REPLICAS_FILE.write_text(json.dumps({"worker": workers}))
        for service in START_ORDER:
            self.start(service)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["up", "down", "status", "reset", "pg-stop"])
    parser.add_argument("--workers", type=int, default=1)
    args = parser.parse_args()
    rt = Runtime()
    if args.command == "up":
        rt.up()
    elif args.command == "down":
        rt.down()
    elif args.command == "reset":
        rt.reset(workers=args.workers)
    elif args.command == "pg-stop":
        from agent import store
        rt.down()
        rt.postgres_stop()
        if store.is_admin():   # the agent's own OS user can't stop the audit store, by design
            rt.agent_db_stop()
    for row in rt.status():
        print(json.dumps(row))


if __name__ == "__main__":
    main()
