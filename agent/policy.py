"""Action authorization (PLAN.md M3).

`decide()` is a pure function of (policy file, tool, validated arguments). It takes no model output,
justification, or log text as input, so nothing the model reads or writes can widen what it may
do (tests/test_policy.py checks this). Unknown tools and anything not listed fall to the default:
forbid.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from agent.tools.schemas import ACTION_TOOLS

POLICY_FILE = Path(__file__).with_name("policy.json")
ALLOW, APPROVAL, FORBID = "allow", "approval", "forbid"


@dataclass(frozen=True)
class Decision:
    decision: str       # allow | approval | forbid
    rule: str           # which policy key matched, or "default"
    reason: str
    policy_version: int


def load_policy(path: Path = POLICY_FILE) -> dict:
    policy = json.loads(path.read_text())
    for key, rule in policy["rules"].items():
        if rule["decision"] not in (ALLOW, APPROVAL, FORBID):
            raise ValueError(f"policy rule {key}: bad decision {rule['decision']!r}")
    return policy


def rule_key(tool: str, args: dict) -> str:
    target = args.get("service") or args.get("name")
    return f"{tool}:{target}" if target else tool


def decide(policy: dict, tool: str, args: dict) -> Decision:
    version = policy["version"]
    if tool not in ACTION_TOOLS:
        return Decision(FORBID, "default", f"{tool!r} is not an action tool", version)
    key = rule_key(tool, args)
    rule = policy["rules"].get(key) or policy["rules"].get(tool)
    if rule is None:
        return Decision(policy["default"], "default", f"no rule for {key}", version)
    matched = key if key in policy["rules"] else tool
    if "max_replicas" in rule and args.get("replicas", 0) > rule["max_replicas"]:
        return Decision(rule["above_max"], matched, f"replicas {args['replicas']} > {rule['max_replicas']}", version)
    return Decision(rule["decision"], matched, rule.get("why", ""), version)
