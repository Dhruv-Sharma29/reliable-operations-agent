"""Recovery actions on the sandbox. M1: plain functions used by the scripted baseline.
M3 adds argument schemas and an authorization policy in front of these. M4 adds idempotency at the
effect boundary: each action takes the executor's `action_id` (never a model argument) and records
it in the target system together with the effect. A repeat under the same id is a no-op that
returns `repeat: True`. Each returns {"ok": bool, "detail": ...}. Guarantees: docs/guarantees.md.
"""

from __future__ import annotations

import os

import httpx

from sandbox import paths, state
from sandbox.runtime import SPECS, Runtime

RESTARTABLE = {"api", "worker", "report-exporter"}  # payments is an external dependency


def restart_service(service: str, instance: str | None = None, action_id: str | None = None) -> dict:
    if service not in RESTARTABLE:
        return {"ok": False, "detail": f"{service} cannot be restarted from the sandbox"}
    done = Runtime().restart(service, instance, action_id=action_id)
    if not done:
        return {"ok": True, "detail": f"{instance or service} already restarted by this action", "repeat": True}
    return {"ok": True, "detail": f"restarted {', '.join(done)}"}


def stop_service(service: str, action_id: str | None = None) -> dict:
    """Naturally idempotent: stopping a stopped service changes nothing."""
    if service != "report-exporter":
        return {"ok": False, "detail": f"stopping {service} is not permitted"}
    Runtime().stop(service)
    return {"ok": True, "detail": f"stopped {service}"}


def scale_service(service: str, replicas: int, action_id: str | None = None) -> dict:
    """Naturally idempotent: sets an absolute replica count, not a delta."""
    if service not in SPECS or not SPECS[service].scalable or not 1 <= replicas <= 4:
        return {"ok": False, "detail": "only worker can be scaled, to 1-4 replicas"}
    Runtime().scale(service, replicas)
    return {"ok": True, "detail": f"{service} scaled to {replicas}"}


def rollback_config(action_id: str | None = None) -> dict:
    """NOT naturally idempotent: a second run would roll back to history[-2], which is the bad
    version, and re-introduce the fault. With `action_id`, a repeat finds its own record and only
    completes any restart that didn't happen."""
    history = state.config_history()
    done = state.record_with_action(history, action_id) if action_id else None
    if done is None:
        if len(history) < 2:
            return {"ok": False, "detail": "no previous config version"}
        previous = history[-2]
        version = state.write_config(previous["config"], author="ops-agent", note=f"rollback to v{previous['version']}",
                                     action_id=action_id)
        detail = f"config v{version} = v{previous['version']}"
    else:
        detail = f"config v{done['version']} already written by this action"
    rt = Runtime()
    restarted = rt.restart("api", action_id=action_id) + rt.restart("worker", action_id=action_id)
    return {"ok": True, "detail": f"{detail}; restarted: {', '.join(restarted) or 'none needed'}",
            **({"repeat": True} if done else {})}


def rollback_release(action_id: str | None = None) -> dict:
    """NOT naturally idempotent: a second run would 'roll back' to the bad release. Same fix."""
    deploys = state.deploy_history()
    done = state.record_with_action(deploys, action_id) if action_id else None
    if done is None:
        if len(deploys) < 2:
            return {"ok": False, "detail": "no previous release"}
        previous = deploys[-2]["release"]
        state.record_deploy(previous, author="ops-agent", note=f"rollback to {previous}", action_id=action_id)
        detail = f"api rolled back to {previous}"
    else:
        detail = f"rollback to {done['release']} already recorded by this action"
    restarted = Runtime().restart("api", action_id=action_id)
    return {"ok": True, "detail": f"{detail}; restarted: {', '.join(restarted) or 'none needed'}",
            **({"repeat": True} if done else {})}


def cleanup_spool(pattern: str, action_id: str | None = None) -> dict:
    """Naturally idempotent: deleting already-deleted files does nothing."""
    if pattern not in {"*.tmp"}:
        return {"ok": False, "detail": "only *.tmp files may be removed from the spool"}
    removed = 0
    for p in paths.SPOOL.glob(pattern):
        p.unlink(missing_ok=True)
        removed += 1
    return {"ok": True, "detail": f"removed {removed} files"}


def rotate_credential(name: str, action_id: str | None = None) -> dict:
    """The action id is the provider's idempotency key, so a retry gets back the same token
    instead of issuing (and invalidating the previous) one."""
    if name != "payments":
        return {"ok": False, "detail": f"unknown credential {name}"}
    headers = {"Idempotency-Key": action_id} if action_id else {}
    resp = httpx.post(f"{paths.PAYMENTS_URL}/admin/issue", headers=headers, timeout=5).json()
    state.write_secret("payments_token", resp["token"], action_id=action_id)
    return {"ok": True, "detail": "issued new payments token and updated the secret store",
            **({"repeat": True} if resp.get("replayed") else {})}


def escalate(reason: str, action_id: str | None = None) -> dict:
    """No system effect beyond one notification record, deduplicated by action id."""
    if not (action_id and state.record_with_action(state.escalations(), action_id)):
        state.record_escalation(reason, action_id=action_id)
    return {"ok": True, "detail": f"escalated: {reason}"}


def observe(tool: str, args: dict, action_id: str) -> bool | None:
    """Reconciliation: is this action's effect visible in the system? True / False, or None when
    the action leaves no trace of who did it. Those are only the naturally idempotent ones, so
    re-running them is safe either way."""
    rt = Runtime()
    if tool == "rollback_config":
        return state.record_with_action(state.config_history(), action_id) is not None
    if tool == "rollback_release":
        return state.record_with_action(state.deploy_history(), action_id) is not None
    if tool == "rotate_credential":
        return state.secret_written_by("payments_token") == action_id
    if tool == "escalate":
        return state.record_with_action(state.escalations(), action_id) is not None
    if tool == "restart_service":
        targets = [args["instance"]] if args.get("instance") else rt.instances(args["service"])
        return all(rt.started_by(i) == action_id and rt.is_running(i) for i in targets)
    return None


ACTIONS = {f.__name__: f for f in (restart_service, stop_service, scale_service, rollback_config,
                                   rollback_release, cleanup_spool, rotate_credential, escalate)}
