"""Tool schemas match the real functions, reject bad arguments, and the evidence ledger works."""

from __future__ import annotations

import inspect

import pytest

from agent.evidence import EvidenceLedger
from agent.tools import act, read
from agent.tools.schemas import ACTION_TOOLS, READ_TOOLS, ToolArgumentError, openai_tools, validate_call


@pytest.mark.parametrize("module,tools", [(read, READ_TOOLS), (act, ACTION_TOOLS)])
def test_schemas_match_function_signatures(module, tools):
    """Guards against drift: every schema property is a real parameter and required == no default."""
    for name, (_, schema) in tools.items():
        params = dict(inspect.signature(getattr(module, name)).parameters)
        # action_id comes from the executor, never the model: it must not be in any model-facing schema
        assert "action_id" not in schema["properties"], name
        params.pop("action_id", None)
        assert set(schema["properties"]) == set(params), name
        required = {p for p, v in params.items() if v.default is inspect.Parameter.empty}
        assert set(schema["required"]) == required, name


@pytest.mark.parametrize("name,args", [
    ("restart_service", {"service": "payments"}),                       # external dependency
    ("restart_service", {"service": "worker", "instance": "worker-9"}),
    ("restart_service", {"service": "api", "force": True}),             # unknown property
    ("scale_service", {"service": "worker", "replicas": 10}),
    ("scale_service", {"service": "worker", "replicas": "3"}),          # wrong type
    ("cleanup_spool", {"pattern": "*"}),
    ("search_logs", {"instance": "../../etc/passwd"}),
    ("search_logs", {}),                                                # missing required
    ("submit_diagnosis", {"incident_type": "worker_hung", "evidence": [], "confidence": "high", "unknowns": []}),
    ("drop_database", {}),
])
def test_invalid_arguments_are_rejected(name, args):
    with pytest.raises(ToolArgumentError):
        validate_call(name, args)


@pytest.mark.parametrize("name,args", [
    ("restart_service", {"service": "worker", "instance": "worker-2"}),
    ("scale_service", {"service": "worker", "replicas": 3}),
    ("search_logs", {"instance": "api", "level": "error", "last": 20}),
    ("submit_diagnosis", {"incident_type": "bad_deploy", "evidence": ["ev-3"], "confidence": "medium", "unknowns": []}),
])
def test_valid_arguments_pass(name, args):
    validate_call(name, args)


def test_openai_tool_format():
    tools = openai_tools(READ_TOOLS)
    assert all(t["type"] == "function" and t["function"]["parameters"]["type"] == "object" for t in tools)
    assert {t["function"]["name"] for t in tools} == set(READ_TOOLS)


def test_ledger_assigns_ids_and_checks_citations():
    ledger = EvidenceLedger({"queue_stats": lambda: {"pending": 5}, "search_logs": lambda **kw: [kw]})
    assert ledger.call("queue_stats") == {"evidence_id": "ev-1", "result": {"pending": 5}}
    assert ledger.call("search_logs", {"instance": "api"})["evidence_id"] == "ev-2"
    assert ledger.check_citations(["ev-1", "ev-2"]) == {"valid": True, "missing": []}
    assert ledger.check_citations(["ev-1", "ev-7"]) == {"valid": False, "missing": ["ev-7"]}
    assert ledger.check_citations([])["valid"] is False


def test_ledger_refuses_actions_and_invalid_args_before_running():
    calls = []
    ledger = EvidenceLedger({"search_logs": lambda **kw: calls.append(kw)})
    with pytest.raises(PermissionError):
        ledger.call("restart_service", {"service": "api"})
    with pytest.raises(ToolArgumentError):
        ledger.call("search_logs", {"instance": "api", "last": 1000})
    assert calls == [] and ledger.entries == []
