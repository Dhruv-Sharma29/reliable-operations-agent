"""Start-up recovery: continue every unfinished incident.

    python -m agent.supervisor

Run it when the agent starts, e.g. after a crash or a host restart. Each unfinished incident resumes
from its last persisted state (agent/workflow.py); actions that already ran are reconciled, not
repeated (docs/04-guarantees.md). No approver is attached here, so an action that needs approval
keeps waiting for a human (`Executor.approve`). Prints one line per incident.
"""

from __future__ import annotations

import json

from agent import workflow
from agent.executor import Executor


def main() -> None:
    for r in workflow.resume_all(Executor("ops-agent")):
        print(json.dumps({"incident_id": r.get("incident_id"), "state": r.get("state"), "error": r.get("error")}))


if __name__ == "__main__":
    main()
