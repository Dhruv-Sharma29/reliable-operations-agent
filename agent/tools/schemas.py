"""Strict JSON Schemas for every tool, in the OpenAI function-calling shape vLLM serves.

Arguments are validated against these before any policy check or execution (M3). Enums encode
scope, e.g. which services exist and which may be restarted. act.py refuses the same out-of-scope
targets independently (defence in depth: a schema bug must not widen what an action can touch).
"""

from __future__ import annotations

from jsonschema import Draft202012Validator

INSTANCE = {"type": "string", "pattern": "^(api|payments|report-exporter|loadgen|worker-[1-4])$"}
INCIDENT_TYPES = ["worker_crashed", "worker_hung", "bad_config", "db_connection_exhaustion", "queue_backlog",
                  "slow_dependency", "disk_fill", "bad_deploy", "expired_credential",
                  "false_alarm_client_errors", "unknown"]


def _obj(properties: dict | None = None, required: tuple[str, ...] = ()) -> dict:
    return {"type": "object", "properties": properties or {}, "required": list(required), "additionalProperties": False}


READ_TOOLS = {
    "service_status": ("Process state and /health answer of every component instance.", _obj()),
    "api_metrics": ("API request counts by status and route, and p95 latency, over the last 60 s.", _obj()),
    "worker_metrics": ("Per-worker payments-call latency, status codes, job totals, last error (60 s).", _obj()),
    "queue_stats": ("Pending jobs, oldest pending age, done/failed in the last 60 s, p95 job duration.", _obj()),
    "worker_heartbeats": ("Seconds since each worker's main loop last wrote a heartbeat.", _obj()),
    "db_connections": ("Open database connections grouped by application and state.", _obj()),
    "search_logs": ("Most recent JSON log records of one instance, optionally filtered.", _obj({
        "instance": INSTANCE,
        "contains": {"type": "string", "maxLength": 100},
        "level": {"enum": ["info", "warning", "error"]},
        "last": {"type": "integer", "minimum": 1, "maximum": 100},
    }, required=("instance",))),
    "config_history": ("Recent config versions with the keys each one changed.", _obj({
        "last": {"type": "integer", "minimum": 1, "maximum": 20}})),
    "deploy_history": ("Recent releases deployed, newest last.", _obj({
        "last": {"type": "integer", "minimum": 1, "maximum": 20}})),
    "runbook": ("Runbook text, optionally only sections mentioning a topic.", _obj({
        "topic": {"type": "string", "maxLength": 60}})),
}

ACTION_TOOLS = {
    "restart_service": ("Restart a service, or one instance of it.", _obj({
        "service": {"enum": ["api", "worker", "report-exporter"]},
        "instance": {"type": "string", "pattern": "^worker-[1-4]$"},
    }, required=("service",))),
    "stop_service": ("Stop the report exporter.", _obj({"service": {"enum": ["report-exporter"]}}, required=("service",))),
    "scale_service": ("Set the number of worker replicas.", _obj({
        "service": {"enum": ["worker"]}, "replicas": {"type": "integer", "minimum": 1, "maximum": 4},
    }, required=("service", "replicas"))),
    "rollback_config": ("Restore the previous config version and restart api and workers.", _obj()),
    "rollback_release": ("Redeploy the previous api release.", _obj()),
    "cleanup_spool": ("Delete temporary files from the spool directory.", _obj(
        {"pattern": {"enum": ["*.tmp"]}}, required=("pattern",))),
    "rotate_credential": ("Issue a new credential from the provider and update the secret store.", _obj(
        {"name": {"enum": ["payments"]}}, required=("name",))),
    "escalate": ("Hand the incident to a human with a reason. Changes nothing.", _obj(
        {"reason": {"type": "string", "minLength": 1, "maxLength": 500}}, required=("reason",))),
}

DIAGNOSIS_TOOL = {
    "submit_diagnosis": ("Final answer: the incident type, the evidence IDs that support it, confidence, and "
                         "what remains unknown.", _obj({
        "incident_type": {"enum": INCIDENT_TYPES},
        "evidence": {"type": "array", "items": {"type": "string", "pattern": "^ev-[0-9]+$"}, "minItems": 1, "maxItems": 20},
        "confidence": {"enum": ["low", "medium", "high"]},
        # Bounded so the whole call fits the 400-token output budget (v1 allowed 10 x 300 chars and
        # twice ran out of tokens mid-submission; results/raw/superseded/README.md).
        "unknowns": {"type": "array", "items": {"type": "string", "maxLength": 160}, "maxItems": 3},
    }, required=("incident_type", "evidence", "confidence", "unknowns"))),
}

ALL_TOOLS = {**READ_TOOLS, **ACTION_TOOLS, **DIAGNOSIS_TOOL}
_VALIDATORS = {name: Draft202012Validator(schema) for name, (_, schema) in ALL_TOOLS.items()}


class ToolArgumentError(ValueError):
    pass


def validate_call(name: str, args: object) -> None:
    """Raise ToolArgumentError (with every problem listed) unless `args` is valid for tool `name`."""
    if name not in _VALIDATORS:
        raise ToolArgumentError(f"unknown tool {name!r}")
    errors = sorted(_VALIDATORS[name].iter_errors(args), key=lambda e: list(e.path))
    if errors:
        raise ToolArgumentError("; ".join(f"{'/'.join(map(str, e.path)) or '(args)'}: {e.message}" for e in errors))


def openai_tools(*groups: dict) -> list[dict]:
    return [{"type": "function", "function": {"name": name, "description": desc, "parameters": schema}}
            for group in groups for name, (desc, schema) in group.items()]
