"""Evidence ledger: every read-tool call gets an ID (ev-1, ev-2, ...) and its full result is kept.

A diagnosis must cite these IDs. The scorer can then check that cited evidence exists and was
actually observed in this run, instead of trusting the model's description of what it saw.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from agent.tools import read
from agent.tools.schemas import READ_TOOLS, validate_call

DEFAULT_FUNCTIONS: dict[str, Callable] = {name: getattr(read, name) for name in READ_TOOLS}


class EvidenceLedger:
    def __init__(self, functions: dict[str, Callable] | None = None):
        self._functions = functions or DEFAULT_FUNCTIONS
        self.entries: list[dict] = []

    def call(self, name: str, args: dict | None = None) -> dict:
        """Validate, run a read tool, record it. Invalid calls raise before anything runs."""
        args = args or {}
        if name not in READ_TOOLS:
            raise PermissionError(f"{name!r} is not a read tool")
        validate_call(name, args)
        result = self._functions[name](**args)
        entry = {"evidence_id": f"ev-{len(self.entries) + 1}", "tool": name, "args": args,
                 "ts": time.time(), "result": result}
        self.entries.append(entry)
        return {"evidence_id": entry["evidence_id"], "result": result}

    def check_citations(self, cited: list[str]) -> dict:
        known = {e["evidence_id"] for e in self.entries}
        missing = [c for c in cited if c not in known]
        return {"valid": bool(cited) and not missing, "missing": missing}
