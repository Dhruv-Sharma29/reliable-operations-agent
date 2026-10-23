"""Operational state the agent can read and some actions can change: config versions, deploy
history, and secrets. Append-only JSONL histories make every change auditable and reversible."""

from __future__ import annotations

import copy
import json
import secrets
import time
from typing import Any

from . import paths

CONFIG_HISTORY = paths.STATE / "config_history.jsonl"
DEPLOYS = paths.STATE / "deploys.jsonl"
ESCALATIONS = paths.STATE / "escalations.jsonl"

KNOWN_GOOD_CONFIG: dict[str, Any] = {
    "api": {"db_port": paths.PG_PORT, "spool_quota_mb": 100},
    "worker": {"payments_url": paths.PAYMENTS_URL, "payments_timeout_s": 10},
}
STABLE_RELEASE = "2.3.0"
KNOWN_RELEASES = {"2.3.0", "2.3.1"}


def _read(path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _append(path, record: dict) -> None:
    with open(path, "a") as f:
        f.write(json.dumps(record) + "\n")


def config_history() -> list[dict]:
    return _read(CONFIG_HISTORY)


def current_config() -> dict:
    return config_history()[-1]["config"]


def write_config(config: dict, author: str, note: str, action_id: str | None = None) -> int:
    """`action_id` is stored in the same record as the change, so the change and the proof that
    this action made it are written atomically (one appended line)."""
    version = len(config_history()) + 1
    _append(CONFIG_HISTORY, {"version": version, "ts": time.time(), "author": author, "note": note,
                             "config": config, **({"action_id": action_id} if action_id else {})})
    return version


def deploy_history() -> list[dict]:
    return _read(DEPLOYS)


def current_release() -> str:
    return deploy_history()[-1]["release"]


def record_deploy(release: str, author: str, note: str, action_id: str | None = None) -> None:
    _append(DEPLOYS, {"ts": time.time(), "service": "api", "release": release, "author": author, "note": note,
                      **({"action_id": action_id} if action_id else {})})


def record_with_action(history: list[dict], action_id: str) -> dict | None:
    """The record an action wrote, if it ran: how idempotent actions detect a repeat."""
    return next((r for r in history if r.get("action_id") == action_id), None)


def escalations() -> list[dict]:
    return _read(ESCALATIONS)


def record_escalation(reason: str, action_id: str | None = None) -> None:
    _append(ESCALATIONS, {"ts": time.time(), "reason": reason, **({"action_id": action_id} if action_id else {})})


def read_secret(name: str) -> str:
    return (paths.SECRETS / name).read_text().strip()


def write_secret(name: str, value: str, action_id: str | None = None) -> None:
    (paths.SECRETS / name).write_text(value)
    meta = paths.SECRETS / f"{name}.action"
    if action_id:
        meta.write_text(action_id)
    else:
        meta.unlink(missing_ok=True)


def secret_written_by(name: str) -> str | None:
    meta = paths.SECRETS / f"{name}.action"
    return meta.read_text().strip() if meta.exists() else None


def reset() -> None:
    paths.ensure_dirs()
    CONFIG_HISTORY.unlink(missing_ok=True)
    DEPLOYS.unlink(missing_ok=True)
    ESCALATIONS.unlink(missing_ok=True)
    write_config(copy.deepcopy(KNOWN_GOOD_CONFIG), author="platform", note="baseline configuration")
    record_deploy(STABLE_RELEASE, author="ci", note="baseline release")
    write_secret("payments_token", "tok_" + secrets.token_hex(12))
