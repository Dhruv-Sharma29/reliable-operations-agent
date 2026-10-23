"""Agent-side persistence: action records and an append-only audit log, in database `agentstate`
on the agent's OWN PostgreSQL instance (port base + 433), not the sandbox's.

It used to share the sandbox's server. A connection-exhaustion incident then made the store
unreachable, and the acting agent crashed instead of fixing it. The incident it was meant to
remediate had taken out its control plane.

Fail closed: any failure to reach the store raises AuditUnavailable. The executor writes the
audit record before every effect, so no action runs without a record.
M4 builds workflow state and idempotency on the same tables."""

from __future__ import annotations

import subprocess

import psycopg

from sandbox import paths

DB = "agentstate"
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


def ensure() -> None:
    """Start the agent's database if needed and apply the schema (once per process)."""
    global _ready
    if _ready:
        return
    from sandbox.runtime import Runtime
    try:
        rt = Runtime()
        if not rt.agent_db_running():
            rt.agent_db_start()
        with _connect() as conn:
            conn.execute(SCHEMA)
    except (psycopg.Error, OSError, subprocess.CalledProcessError) as e:
        raise AuditUnavailable(f"agent store unavailable: {type(e).__name__}: {str(e)[:200]}") from e
    _ready = True


def _connect() -> psycopg.Connection:
    return psycopg.connect(paths.dsn("agent", "ops-agent", port=paths.AGENT_PG_PORT), dbname=DB, autocommit=True)


def connect() -> psycopg.Connection:
    ensure()
    try:
        return _connect()
    except psycopg.OperationalError as e:
        raise AuditUnavailable(f"agent store unavailable: {str(e)[:200]}") from e
