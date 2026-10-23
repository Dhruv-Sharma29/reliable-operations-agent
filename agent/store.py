"""Agent-side persistence: action records and an append-only audit log, in database `agentstate`
on the agent's OWN PostgreSQL instance (port base + 433), not the sandbox's.

It used to share the sandbox's server. A connection-exhaustion incident then made the store
unreachable, and the acting agent crashed instead of fixing it. The incident it was meant to
remediate had taken out its control plane.

Fail closed: any failure to reach the store raises AuditUnavailable. The executor writes the
audit record before every effect, so no action runs without a record.
M4 builds workflow state and idempotency on the same tables.

Ownership: the database and every table belong to `audit_owner`, a role that can't log in, and the
schema is applied by the cluster's superuser (apply_schema). The `agent` role gets only the rows it
needs to write: it can append audit events but not change, delete or truncate them, and it can't
drop or disable the append-only triggers, because only a table's owner can."""

from __future__ import annotations

import subprocess

import psycopg

from sandbox import paths

DB = "agentstate"
OWNER = "audit_owner"
# What the agent may do per table; everything else (DDL, TRUNCATE, UPDATE/DELETE on logs) is denied.
AGENT_GRANTS = {"actions": "SELECT, INSERT, UPDATE", "audit_events": "SELECT, INSERT"}
SCHEMA = """
CREATE TABLE IF NOT EXISTS actions (
    action_id    text PRIMARY KEY,
    incident_id  text NOT NULL,
    tool         text NOT NULL,
    args         jsonb NOT NULL,
    requested_by text NOT NULL,
    decision     text NOT NULL,           -- allow | approval | forbid | invalid
    rule         text,
    policy_version int,
    state        text NOT NULL,           -- requested | authorized | pending_approval | approved | denied | rejected
                                          -- | executing | unknown (ambiguous: reconcile) | succeeded | failed
    approver     text,
    reason       text,
    result       jsonb,
    requested_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    decided_at   timestamptz,
    approved_at  timestamptz,
    started_at   timestamptz,
    finished_at  timestamptz
);
CREATE TABLE IF NOT EXISTS audit_events (
    id        bigserial PRIMARY KEY,
    action_id text NOT NULL,
    at        timestamptz NOT NULL DEFAULT clock_timestamp(),
    event     text NOT NULL,
    detail    jsonb NOT NULL DEFAULT '{}'
);
-- append-only: the agent role may insert audit events, never change or delete them
CREATE OR REPLACE FUNCTION audit_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'audit_events is append-only'; END $$;
DROP TRIGGER IF EXISTS audit_no_change ON audit_events;
CREATE TRIGGER audit_no_change BEFORE UPDATE OR DELETE ON audit_events
    FOR EACH ROW EXECUTE FUNCTION audit_append_only();
DROP TRIGGER IF EXISTS audit_no_truncate ON audit_events;   -- row triggers don't fire on TRUNCATE
CREATE TRIGGER audit_no_truncate BEFORE TRUNCATE ON audit_events
    FOR EACH STATEMENT EXECUTE FUNCTION audit_append_only();
"""


class AuditUnavailable(RuntimeError):
    """The audit store can't be reached: callers must not perform any action."""


_ready = False
generation = 0   # bumped each time ensure() (re)initialises the store; lets dependants re-apply their schema


def ensure() -> None:
    """Start the agent's database if needed and apply the schema (once per process)."""
    global _ready, generation
    if _ready:
        return
    from sandbox.runtime import Runtime
    try:
        rt = Runtime()
        if not rt.agent_db_running():
            rt.agent_db_start()
        apply_schema(SCHEMA, AGENT_GRANTS)
    except (psycopg.Error, OSError, subprocess.CalledProcessError) as e:
        raise AuditUnavailable(f"agent store unavailable: {type(e).__name__}: {str(e)[:200]}") from e
    _ready = True
    generation += 1


def apply_schema(sql: str, grants: dict[str, str]) -> None:
    """Run DDL as the cluster superuser, give the tables to OWNER, and grant the agent exactly
    `grants` (table -> privileges) plus the sequences its INSERTs need. Idempotent, and it also
    migrates a store created before 2026-10-23, when the agent role owned everything."""
    with psycopg.connect(paths.dsn("postgres", "agent-setup", port=paths.AGENT_PG_PORT), dbname=DB,
                         autocommit=True) as conn, conn.transaction():
        conn.execute(f"ALTER DATABASE {DB} OWNER TO {OWNER}")
        conn.execute(sql)
        for table, privileges in grants.items():
            conn.execute(f"ALTER TABLE {table} OWNER TO {OWNER}")
            conn.execute(f"REVOKE ALL ON {table} FROM agent, PUBLIC")
            conn.execute(f"GRANT {privileges} ON {table} TO agent")
        conn.execute("GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO agent")


def _connect() -> psycopg.Connection:
    return psycopg.connect(paths.dsn("agent", "ops-agent", port=paths.AGENT_PG_PORT), dbname=DB, autocommit=True)


def connect() -> psycopg.Connection:
    ensure()
    try:
        return _connect()
    except psycopg.OperationalError as e:
        raise AuditUnavailable(f"agent store unavailable: {str(e)[:200]}") from e
