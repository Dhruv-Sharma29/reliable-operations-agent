"""Run the sandbox and the agent as their own unprivileged OS user, with the audit store's superuser
kept by the setup user (docs/03-permissions-and-audit.md § A separate agent user).

    python -m sandbox.deploy prepare --agent-uid 1000      # as root: audit store, hand over the sandbox
    python -m sandbox.deploy exec -- .venv/bin/python -m eval.run --responder baseline-exec --out-dir .run/results

`prepare` stops whatever runs from this run directory, starts the audit store and applies its
schema as the setup user, gives the agent's password to the agent's uid, and makes the agent's uid
the owner of the sandbox's directories, including its PostgreSQL cluster. The audit store's cluster
and the superuser's password stay with `nobody` and the setup user. `exec` drops to that uid, the
way a service manager starts a service as its user, and runs the command. The uid must be a real
account (initdb looks it up), not root and not `nobody`, which runs the audit store. Run
`prepare --agent-uid 0` to hand the sandbox back to root.
"""

from __future__ import annotations

import argparse
import json
import os
import pwd
import shutil
import sys
from pathlib import Path

from . import paths

DEPLOY_FILE = paths.RUN / "deploy.json"


def _chown_tree(path: Path, uid: int, gid: int) -> None:
    for p in [path, *path.rglob("*")]:
        os.chown(p, uid, gid, follow_symlinks=False)


def prepare(agent_uid: int) -> None:
    from agent import store, workflow
    from sandbox.runtime import Runtime
    if os.geteuid() != 0:
        sys.exit("prepare must run as root: it changes file ownership")
    if agent_uid == 65534:
        sys.exit("uid 65534 (nobody) runs the audit store; the agent needs a different user")
    try:
        gid = pwd.getpwuid(agent_uid).pw_gid
    except KeyError:
        sys.exit(f"uid {agent_uid} has no passwd entry; initdb needs one (create the account first)")
    rt = Runtime()
    rt.down()
    rt.postgres_stop()
    os.environ["AGENT_OS_UID"] = str(agent_uid)   # read by agent_db_start: who gets agent.pw
    store._ready = False
    workflow.ensure()                             # audit store up, both schemas applied, as the setup user
    paths.ensure_dirs()
    (paths.RUN / "results").mkdir(exist_ok=True)
    for d in (paths.LOGS, paths.PIDS, paths.STATE, paths.FAULTS, paths.SPOOL, paths.RUN / "results"):
        _chown_tree(d, agent_uid, gid)
    # The sandbox's cluster belongs to the user that runs it; root's clusters run as `nobody`.
    paths.PG_DIR.mkdir(exist_ok=True)
    _chown_tree(paths.PG_DIR, *((65534, 65534) if agent_uid == 0 else (agent_uid, gid)))
    DEPLOY_FILE.write_text(json.dumps({"agent_uid": agent_uid}) + "\n")
    print(json.dumps({"agent_uid": agent_uid, "run_dir": str(paths.RUN), "audit_store_port": paths.AGENT_PG_PORT,
                      "agent_password_owner": (paths.AGENT_SECRETS / "agent.pw").stat().st_uid,
                      "superuser_password_owner": (paths.AGENT_SECRETS / "postgres.pw").stat().st_uid}))


def exec_as_agent(cmd: list[str]) -> None:
    agent_uid = json.loads(DEPLOY_FILE.read_text())["agent_uid"]
    gid = pwd.getpwuid(agent_uid).pw_gid
    groups = set(paths.traverse_groups(paths.ROOT)) | set(paths.traverse_groups(paths.RUN)) | \
        set(paths.traverse_groups(paths.PG_BIN))
    group_arg = [f"--groups={','.join(str(g) for g in sorted(groups))}"] if groups else ["--clear-groups"]
    os.environ.update(HOME=str(paths.RUN / "results"), PYTHONDONTWRITEBYTECODE="1")
    os.execvp("setpriv", ["setpriv", f"--reuid={agent_uid}", f"--regid={gid}", *group_arg, "--", *cmd])


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("prepare").add_argument("--agent-uid", type=int, required=True)
    sub.add_parser("exec").add_argument("cmd", nargs=argparse.REMAINDER)
    args = p.parse_args()
    if args.command == "prepare":
        prepare(args.agent_uid)
    else:
        if not shutil.which("setpriv"):
            sys.exit("exec needs setpriv (util-linux)")
        exec_as_agent(args.cmd[1:] if args.cmd[:1] == ["--"] else args.cmd)


if __name__ == "__main__":
    main()
