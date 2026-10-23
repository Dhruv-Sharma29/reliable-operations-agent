"""Static HTML report of an evaluation run (PLAN.md M5 UI): evidence, tool trace, action result
and remaining uncertainty for every case, next to the independent verifier's verdict.

    python -m eval.report_ui results/raw/<run>.jsonl [more runs ...] --out results/ui/report.html

Self-contained (inline CSS, no scripts beyond <details>), so it opens from disk or a CI artifact.
"""

from __future__ import annotations

import argparse
import html
import json
from pathlib import Path

CSS = """
:root{--bg:#fbfbf9;--fg:#1d1d1b;--muted:#6b6b66;--line:#e2e1dc;--card:#fff;--ok:#1f7a3f;--bad:#b3261e;--warn:#8a5a00;
--code:#f3f2ee}
@media (prefers-color-scheme:dark){:root{--bg:#141413;--fg:#ecebe6;--muted:#9c9b95;--line:#2c2c29;--card:#1c1c1a;
--ok:#5cc58a;--bad:#f28b82;--warn:#e0b25a;--code:#232320}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,sans-serif}
main{max-width:1100px;margin:0 auto;padding:24px 16px 64px}h1{font-size:22px;margin:0 0 4px}
h2{font-size:17px;margin:32px 0 8px}.muted{color:var(--muted)}table{border-collapse:collapse;width:100%;font-size:14px}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}
.wrap{overflow-x:auto}.ok{color:var(--ok);font-weight:600}.bad{color:var(--bad);font-weight:600}.warn{color:var(--warn)}
details{background:var(--card);border:1px solid var(--line);border-radius:8px;margin:8px 0;padding:8px 12px}
summary{cursor:pointer;font-weight:600}pre{background:var(--code);padding:8px;border-radius:6px;overflow-x:auto;
white-space:pre-wrap;word-break:break-word;font-size:12.5px;margin:4px 0}.grid{display:grid;grid-template-columns:160px 1fr;
gap:4px 12px;margin:8px 0}.tag{display:inline-block;padding:0 6px;border-radius:4px;border:1px solid var(--line);font-size:12px}
@media (max-width:600px){.grid{grid-template-columns:1fr}}
"""


def esc(x) -> str:
    return html.escape(x if isinstance(x, str) else json.dumps(x, default=str))


def verdict(ok: bool, yes="yes", no="no") -> str:
    return f'<span class="{"ok" if ok else "bad"}">{yes if ok else no}</span>'


def case_html(r: dict) -> str:
    x = r["response"]
    step = (x.get("requested_actions") or [{}])[0]
    ev = {e["id"]: e for e in x.get("evidence_log", [])}
    cited = "".join(f'<pre><b>{esc(i)}</b> {esc(ev[i]["tool"])} {esc(ev[i]["args"])}\n{esc(ev[i]["result"])}</pre>'
                    if i in ev else f"<pre>{esc(i)} (result not recorded in this run)</pre>" for i in x.get("evidence", []))
    trace = "".join(f"<li><code>{esc(t['tool'])}</code> {esc(t['args'])}</li>" for t in x.get("tool_calls", []))
    words = "".join(f"<pre>{esc(t)}</pre>" for t in x.get("transcript", []) if not t.startswith("[tools]"))
    action = "none"
    if step.get("tool"):
        action = (f"<code>{esc(step['tool'])}</code> {esc(step.get('args', {}))} &rarr; policy "
                  f"<span class=tag>{esc(step.get('decision') or '-')}</span> state <span class=tag>{esc(step.get('state'))}</span>")
        res = next((a.get("result") for a in x.get("actions", [])), None)
        if res is not None:
            action += f"<pre>{esc(res)}</pre>"
    unknowns = "".join(f"<li>{esc(u)}</li>" for u in x.get("unknowns", [])) or "<li class=muted>none stated</li>"
    head = (f'{esc(r["case_id"])} &middot; {verdict(r["success"], "success", "failure")} &middot; '
            f'diagnosis {verdict(r["diagnosis_correct"], esc(x["diagnosis"]), esc(x["diagnosis"]))} '
            f'<span class=muted>(truth: {esc(r["incident"])})</span>')
    return f"""<details><summary>{head}</summary><div class=grid>
<div class=muted>Alerts</div><div>{esc(", ".join(a["alert"] for a in r.get("alerts", [])))}</div>
<div class=muted>Diagnosis</div><div>{esc(x["diagnosis"])} &middot; confidence {esc(x.get("confidence") or "-")}
 &middot; citations valid {verdict(x.get("citations_valid", False))} &middot; stop {esc(x.get("stop_reason") or "-")}</div>
<div class=muted>Remaining uncertainty</div><div><ul>{unknowns}</ul></div>
<div class=muted>Action</div><div>{action}</div>
<div class=muted>Verified health</div><div>{verdict(r["post_verify"]["healthy"], "healthy", "unhealthy")}
 <span class=muted>{esc(r["post_verify"].get("reason", ""))} &middot; independent probe, not the agent's claim</span></div>
<div class=muted>Unnecessary actions</div><div>{esc(r["unnecessary_actions"]) if r["unnecessary_actions"] else "none"}</div>
<div class=muted>Cost</div><div>{x.get("model_calls", 0)} model calls &middot; {r.get("respond_s", "-")} s to hand-off</div>
</div>
<p class=muted>Cited evidence (what the tools actually returned)</p>{cited or "<p class=muted>none</p>"}
<p class=muted>Tool trace</p><ol>{trace or "<li class=muted>none</li>"}</ol>
<p class=muted>The model's own words</p>{words or "<p class=muted>none</p>"}</details>"""


def run_html(path: Path) -> str:
    rows = [json.loads(line) for line in open(path)]
    meta = rows[0]
    cases = [r for r in rows if r.get("type") == "case" and "response" in r]
    n = len(cases)
    summary = (f"{sum(r['success'] for r in cases)}/{n} success &middot; {sum(r['diagnosis_correct'] for r in cases)}/{n} "
               f"correct diagnosis &middot; {sum(r['post_verify']['healthy'] for r in cases)}/{n} healthy after &middot; "
               f"{sum(bool(r['unnecessary_actions']) for r in cases)} with unnecessary actions")
    table = "".join(f"<tr><td>{esc(r['case_id'])}</td><td>{esc(r['response']['diagnosis'])}</td>"
                    f"<td>{esc(((r['response'].get('requested_actions') or [{}])[0]).get('tool') or '-')}</td>"
                    f"<td>{verdict(r['post_verify']['healthy'])}</td><td>{verdict(r['success'])}</td></tr>" for r in cases)
    return (f"<h2>{esc(meta['run_id'])}</h2><p class=muted>responder {esc(meta['responder'])} &middot; split "
            f"{esc(meta['split'])} &middot; {summary}</p><div class=wrap><table><tr><th>case</th><th>diagnosis</th>"
            f"<th>action</th><th>healthy after</th><th>success</th></tr>{table}</table></div>"
            + "".join(case_html(r) for r in cases))


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("runs", nargs="+")
    p.add_argument("--out", default="results/ui/report.html")
    args = p.parse_args()
    body = "".join(run_html(Path(r)) for r in args.runs)
    page = (f"<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content='width=device-width,"
            f"initial-scale=1'><title>Incident Agent Runs</title><style>{CSS}</style></head><body><main>"
            f"<h1>Incident agent runs</h1><p class=muted>Every verdict comes from the evaluation harness: success "
            f"needs the right diagnosis, a necessary allowed action and a healthy service by independent probe.</p>"
            f"{body}</main></body></html>")
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page)
    print(out)


if __name__ == "__main__":
    main()
