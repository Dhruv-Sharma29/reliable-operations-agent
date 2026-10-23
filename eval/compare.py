"""PLAN.md M5 comparison: scripted runbook vs simple prompted agent vs durable agent.

    python -m eval.compare results/summary/m5_manifest.json --out results/summary/m5

The manifest maps responder -> split -> [run files]. When several files list the same case, the
LATER file wins, e.g. the baseline's original runs followed by the backlog cases rerun with the
sustained injection. Every number comes from the runner's records: the verifier's probe, not
the agent's claims.

Metrics per responder and split:
  success            right diagnosis + necessary, allowed action + verified healthy (or right
                     escalation / no action), the runner's `score`
  correct diagnosis
  verified recovery  healthy after, by the independent probe, on incidents that need a fix
  unnecessary        cases with any state change the incident didn't need
  claimed != verified  cases where the agent's action reported success but the probe found the
                     service unhealthy (escalations and no-action excluded)
  model calls        median per case
  time to recovery   median seconds from hand-off to verified healthy, recovered cases only
"""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path

from incidents.catalog import INCIDENTS

ROOT = Path(__file__).resolve().parents[1]


def load_cases(files: list[str]) -> dict[str, dict]:
    cases: dict[str, dict] = {}
    for f in files:
        for line in open(ROOT / f):
            r = json.loads(line)
            if r.get("type") == "case" and "harness_error" not in r:
                cases[r["case_id"]] = r
    return cases


def _claimed_fixed(r: dict) -> bool:
    """The agent ran at least one state-changing action and every one reported ok."""
    acts = [a for a in r["response"].get("actions", []) if a["action"] != "escalate"]
    return bool(acts) and all(isinstance(a.get("result"), dict) and a["result"].get("ok") for a in acts)


def metrics(cases: dict[str, dict]) -> dict:
    rs = list(cases.values())
    fix = [r for r in rs if INCIDENTS[r["incident"]].expected == "fix"]
    acted = [r for r in rs if _claimed_fixed(r)]
    recov = [r["time_to_verified_s"] for r in fix if r.get("time_to_verified_s") is not None]
    calls = [r["response"].get("model_calls", 0) for r in rs]
    return {"cases": len(rs), "success": sum(r["success"] for r in rs),
            "correct_diagnosis": sum(r["diagnosis_correct"] for r in rs),
            "verified_recovery": f"{sum(r['post_verify']['healthy'] for r in fix)}/{len(fix)}",
            "unnecessary": sum(bool(r["unnecessary_actions"]) for r in rs),
            "claimed_not_verified": sum(not r["post_verify"]["healthy"] for r in acted),
            "median_model_calls": statistics.median(calls) if calls else 0,
            "median_time_to_recovery_s": round(statistics.median(recov), 1) if recov else None}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("manifest")
    p.add_argument("--out", default="results/summary/m5")
    args = p.parse_args()
    manifest = json.loads(Path(args.manifest).read_text())
    rows, per_case = [], {}
    for responder, splits in manifest["responders"].items():
        for split, files in splits.items():
            cases = load_cases(files)
            rows.append({"responder": responder, "split": split, **metrics(cases)})
            per_case[f"{responder}/{split}"] = {k: {"success": v["success"], "diagnosis": v["response"]["diagnosis"],
                                                    "healthy": v["post_verify"]["healthy"]} for k, v in cases.items()}
    cols = ["responder", "split", "cases", "success", "correct_diagnosis", "verified_recovery", "unnecessary",
            "claimed_not_verified", "median_model_calls", "median_time_to_recovery_s"]
    md = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    md += ["| " + " | ".join(str(r[c]) for c in cols) + " |" for r in rows]
    out = Path(args.out)
    out.with_suffix(".md").write_text("\n".join(md) + "\n")
    out.with_suffix(".json").write_text(json.dumps({"rows": rows, "cases": per_case, "manifest": manifest}, indent=1))
    print("\n".join(md))


if __name__ == "__main__":
    main()
