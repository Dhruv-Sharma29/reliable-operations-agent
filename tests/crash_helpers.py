"""Run a workflow step in a child process that kills itself at a chosen point (os._exit: no
cleanup, no exception handlers, like a SIGKILL or power loss). Used by tests/test_durability.py.

    python -m tests.crash_helpers <incident_id> <crash_point>

crash_point: "after_effect"  - the action's effect happened; its result was never recorded
             "PROPOSED"      - the model's proposal is persisted; nothing executed yet
"""

from __future__ import annotations

import json
import os
import sys

import httpx


def scripted_model(steps: list[list[tuple[str, dict]]], calls: list | None = None) -> httpx.Client:
    """Fake OpenAI-compatible model: returns the scripted tool calls in order."""
    n = {"i": 0}

    def handler(request):
        if calls is not None:
            calls.append(json.loads(request.content))
        step = steps[min(n["i"], len(steps) - 1)]
        n["i"] += 1
        msg = {"role": "assistant", "content": "", "tool_calls": [
            {"id": f"c{n['i']}{k}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
            for k, (name, args) in enumerate(step)]}
        return httpx.Response(200, json={"choices": [{"message": msg}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}})
    return httpx.Client(transport=httpx.MockTransport(handler), base_url="http://fake")


def bad_config_model(calls: list | None = None) -> httpx.Client:
    diag = {"incident_type": "bad_config", "evidence": ["ev-1"], "confidence": "high", "unknowns": []}
    return scripted_model([[("config_history", {})], [("submit_diagnosis", diag)], [("rollback_config", {})]], calls)


def no_model() -> httpx.Client:
    """A model that must not be called: resuming a persisted step needs no new model decision."""
    def handler(request):
        raise AssertionError("the model was called during resume")
    return httpx.Client(transport=httpx.MockTransport(handler), base_url="http://fake")


def main() -> None:
    from agent import executor as executor_mod
    from agent import workflow
    incident_id, point = sys.argv[1], sys.argv[2]
    if point == "after_effect":
        executor_mod._after_effect = lambda action_id: os._exit(137)
    else:
        workflow._after_transition = lambda inc, state: os._exit(137) if state == point else None
    approver = os.environ.get("CRASH_APPROVER") or None
    workflow.run(incident_id, executor_mod.Executor("ops-agent"), client=bad_config_model(),
                 approver=(lambda outcome: approver) if approver else None, verify=lambda: {"healthy": True})
    sys.exit("crash point never reached")


if __name__ == "__main__":
    main()
