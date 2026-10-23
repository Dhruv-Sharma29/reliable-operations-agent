"""Authorization policy: decisions come only from the policy file and validated tool + arguments."""

from __future__ import annotations

import inspect

import pytest

from agent.policy import ALLOW, APPROVAL, FORBID, decide, load_policy
from agent.tools.schemas import ACTION_TOOLS

POLICY = load_policy()


@pytest.mark.parametrize("tool,args,expected", [
    ("restart_service", {"service": "worker", "instance": "worker-2"}, ALLOW),
    ("restart_service", {"service": "report-exporter"}, ALLOW),
    ("cleanup_spool", {"pattern": "*.tmp"}, ALLOW),
    ("scale_service", {"service": "worker", "replicas": 3}, ALLOW),
    ("scale_service", {"service": "worker", "replicas": 4}, APPROVAL),   # above the auto limit
    ("restart_service", {"service": "api"}, APPROVAL),
    ("rollback_config", {}, APPROVAL),
    ("rollback_release", {}, APPROVAL),
    ("rotate_credential", {"name": "payments"}, APPROVAL),
    ("escalate", {"reason": "x"}, ALLOW),
    ("search_logs", {"instance": "api"}, FORBID),                       # read tool, not an action
    ("drop_database", {}, FORBID),
])
def test_decisions(tool, args, expected):
    assert decide(POLICY, tool, args).decision == expected


def test_unlisted_action_falls_to_default_forbid():
    policy = {**POLICY, "rules": {k: v for k, v in POLICY["rules"].items() if k != "cleanup_spool"}}
    d = decide(policy, "cleanup_spool", {"pattern": "*.tmp"})
    assert (d.decision, d.rule) == (FORBID, "default")


def test_decision_has_no_text_inputs():
    """Nothing the model writes or reads (justifications, log lines) can reach the decision."""
    assert list(inspect.signature(decide).parameters) == ["policy", "tool", "args"]


def test_policy_file_is_consistent_with_the_action_tools():
    assert POLICY["default"] == FORBID
    for key, rule in POLICY["rules"].items():
        assert key.split(":")[0] in ACTION_TOOLS, key
        assert rule["decision"] in (ALLOW, APPROVAL, FORBID)
