"""M2 summary: diagnosis quality of the read-only LLM agent vs the scripted baseline, per incident.

    python -m eval.summarize_m2 results/raw/<llm run>.jsonl --baseline results/raw/<baseline run>.jsonl

Read-only means no recovery is attempted, so success is scored on the diagnosis alone: correct
incident type, citations that exist in the evidence ledger, and the cost to get there.
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections import defaultdict
from pathlib import Path


def cases(path: Path) -> list[dict]:
    return [r for r in map(json.loads, path.read_text().splitlines()) if r.get("type") == "case" and "harness_error" not in r]


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("llm", type=Path)
    p.add_argument("--baseline", type=Path)
    p.add_argument("--out", type=Path)
    args = p.parse_args()
    llm = cases(args.llm)
    base = {c["case_id"]: c for c in cases(args.baseline)} if args.baseline else {}
    by_inc = defaultdict(list)
    for c in llm:
        by_inc[c["incident"]].append(c)
    rows = []
    for inc, cs in sorted(by_inc.items()):
        r = [c["response"] for c in cs]
        rows.append({"incident": inc, "cases": len(cs),
                     "llm_correct": sum(c["diagnosis_correct"] for c in cs),
                     "baseline_correct": sum(base[c["case_id"]]["diagnosis_correct"] for c in cs if c["case_id"] in base),
                     "citations_valid": sum(x.get("citations_valid", False) for x in r),
                     "invalid_calls": sum(x.get("invalid_calls", 0) for x in r),
                     "median_model_calls": statistics.median(x.get("model_calls", 0) for x in r),
                     "median_seconds": statistics.median(x.get("seconds", 0) for x in r),
                     "llm_answers": ",".join(sorted({x["diagnosis"] for x in r}))})
    tot = {k: sum(r[k] for r in rows) for k in ("cases", "llm_correct", "baseline_correct", "citations_valid", "invalid_calls")}
    calls = [c["response"].get("model_calls", 0) for c in llm]
    toks = [c["response"].get("prompt_tokens", 0) + c["response"].get("completion_tokens", 0) for c in llm]
    cols = list(rows[0])
    md = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    md += ["| " + " | ".join(str(r[c]) for c in cols) + " |" for r in rows]
    md.append(f"\n**Total:** LLM correct {tot['llm_correct']}/{tot['cases']} · baseline {tot['baseline_correct']}/{tot['cases']} · "
              f"valid citations {tot['citations_valid']}/{tot['cases']} · invalid tool calls {tot['invalid_calls']} · "
              f"median model calls {statistics.median(calls)} · median tokens {statistics.median(toks)}")
    text = "\n".join(md) + "\n"
    print(text)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.with_suffix(".md").write_text(text)


if __name__ == "__main__":
    main()
