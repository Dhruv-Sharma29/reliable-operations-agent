"""Shared helpers for the sandbox app: JSON-line logging and sliding-window metrics."""

from __future__ import annotations

import json
import math
import os
import time
from collections import deque

from sandbox import paths

INSTANCE = os.environ.get("SANDBOX_INSTANCE", "dev")


class JsonLog:
    """One JSON object per line in .run/logs/<instance>.log — what the agent's log tool reads."""

    def __init__(self, instance: str = INSTANCE):
        paths.LOGS.mkdir(parents=True, exist_ok=True)
        self._f = open(paths.LOGS / f"{instance}.log", "a", buffering=1)
        self.instance = instance

    def _write(self, level: str, msg: str, **fields) -> None:
        record = {"ts": round(time.time(), 3), "level": level, "instance": self.instance, "msg": msg, **fields}
        self._f.write(json.dumps(record, default=str) + "\n")

    def info(self, msg: str, **fields) -> None:
        self._write("info", msg, **fields)

    def warning(self, msg: str, **fields) -> None:
        self._write("warning", msg, **fields)

    def error(self, msg: str, **fields) -> None:
        self._write("error", msg, **fields)


class Window:
    """Events from the last `horizon_s` seconds, for rate and percentile metrics."""

    def __init__(self, horizon_s: float = 60.0):
        self.horizon_s = horizon_s
        self._events: deque[tuple[float, dict]] = deque()

    def add(self, **fields) -> None:
        self._events.append((time.time(), fields))
        self._trim()

    def _trim(self) -> None:
        cutoff = time.time() - self.horizon_s
        while self._events and self._events[0][0] < cutoff:
            self._events.popleft()

    def events(self) -> list[dict]:
        self._trim()
        return [e for _, e in self._events]


def p95(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)], 1)
