"""Batch report exporter. Normally opens one short connection every 5 s.

Fault injection: if .run/faults/exporter_leak exists at start-up (a bad build), it consumes the
flag and keeps taking free connection slots (never releasing them) until only `keep_free` slots
(flag contents) remain for everyone else. A restart clears it.
"""

from __future__ import annotations

import time

import psycopg

from sandbox import paths
from target_app.common import JsonLog

log = JsonLog()


def leak(keep_free: int) -> None:
    """Grab every connection slot that becomes free, never releasing, until only `keep_free`
    non-reserved slots remain. Converges regardless of what other clients hold at start-up."""
    with psycopg.connect(paths.dsn("postgres", "report-exporter-admin"), autocommit=True) as admin:
        max_conn = int(admin.execute("SHOW max_connections").fetchone()[0])
        reserved = int(admin.execute("SHOW superuser_reserved_connections").fetchone()[0])
    target = max_conn - reserved - keep_free
    held = []
    log.info("export batch started")
    while True:
        if len(held) < target:
            try:
                held.append(psycopg.connect(paths.dsn("app", "report-exporter")))
                continue
            except psycopg.OperationalError:
                pass
        time.sleep(0.05)


def main() -> None:
    flag = paths.FAULTS / "exporter_leak"
    if flag.exists():
        keep_free = int(flag.read_text().strip() or 0)
        flag.unlink()
        leak(keep_free)
    log.info("report exporter started")
    while True:
        try:
            with psycopg.connect(paths.dsn("app", "report-exporter")) as conn:
                done = conn.execute("SELECT count(*) FROM jobs WHERE status = 'done'").fetchone()[0]
            log.info("export batch complete", jobs_exported=done)
        except psycopg.OperationalError as e:
            log.error("export failed", error=str(e).strip().splitlines()[0])
        time.sleep(5)


if __name__ == "__main__":
    main()
