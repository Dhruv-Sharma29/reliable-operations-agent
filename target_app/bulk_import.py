"""Bulk import: a client that keeps inserting jobs faster than one worker can process them.

Fault injection only, never started by `up`/`reset`. Reads `{"rate": jobs_per_s}` from
.run/faults/bulk_import and inserts that many jobs every second until stopped (reset stops it).
A one-off burst would drain by itself in about 100 s with one worker (measured), so a slow
responder would get recovery it didn't cause. A sustained arrival rate above one worker's capacity
only recovers if the workers are scaled.
"""

from __future__ import annotations

import json
import time

import psycopg

from sandbox import paths
from target_app.common import JsonLog

log = JsonLog()


def main() -> None:
    rate = int(json.loads((paths.FAULTS / "bulk_import").read_text())["rate"])
    log.info("bulk import started", rate_per_s=rate)
    with psycopg.connect(paths.dsn("postgres", "bulk-import"), autocommit=True) as conn:
        next_tick = time.monotonic()
        while True:
            conn.execute("INSERT INTO jobs (kind, payload) SELECT 'invoice', jsonb_build_object('amount', g, 'customer', 'bulk') "
                         "FROM generate_series(1, %s) g", (rate,))
            next_tick += 1.0
            time.sleep(max(0.0, next_tick - time.monotonic()))


if __name__ == "__main__":
    main()
