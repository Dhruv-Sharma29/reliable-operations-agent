"""Summarise incident runs: one table per run, one row per incident type.

    python -m eval.summarize results/raw/*.jsonl --out results/summary/m1
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections import defaultdict
from pathlib import Path


def summarise(path: Path) -> tuple[dict, list[dict]]:
    records = [json.loads(line) for line in path.read_text().splitlines()]
    run = next(r for r in records if r["type"] == "run")
    by_incident = defaultdict(list)
    for r in records:
        if r["type"] == "case":
            by_incident[r["incident"]].append(r)
    rows = []
    for incident, cases in by_incident.items():
        valid = [c for c in cases if "harness_error" not in c]
        ttv = [c["time_to_verified_s"] for c in valid if c.get("time_to_verified_s") is not None]
        rows.append({
            "incident": incident, "expected": cases[0]["expected"], "cases": len(cases),
            "harness_errors": len(cases) - len(valid),
            "alert_fired": sum(1 for c in valid if c["alerts"]),
            "diagnosis_correct": sum(1 for c in valid if c["diagnosis_correct"]),
            "healthy_after": sum(1 for c in valid if c["post_verify"]["healthy"]),
            "unnecessary_actions": sum(len(c["unnecessary_actions"]) for c in valid),
            "success": sum(1 for c in valid if c["success"]),
            "median_time_to_verified_s": round(statistics.median(ttv), 1) if ttv else None,
        })
    return run, rows


def to_markdown(run: dict, rows: list[dict]) -> str:
    cols = list(rows[0])
    lines = [f"### {run['run_id']} — responder `{run['responder']}`, split `{run['split']}`", "",
             "| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    lines += ["| " + " | ".join("" if r[c] is None else str(r[c]) for c in cols) + " |" for r in rows]
    total = {k: sum(r[k] for r in rows) for k in ("cases", "harness_errors", "alert_fired", "diagnosis_correct",
                                                  "healthy_after", "unnecessary_actions", "success")}
    lines.append("| **total** | | " + " | ".join(str(v) for v in total.values()) + " | |")
    return "\n".join(lines) + "\n"


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("files", nargs="+", type=Path)
    p.add_argument("--out", type=Path)
    args = p.parse_args()
    md = "\n".join(to_markdown(*summarise(f)) for f in sorted(args.files))
    print(md)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.with_suffix(".md").write_text(md)


if __name__ == "__main__":
    main()
