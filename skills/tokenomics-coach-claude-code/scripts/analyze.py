#!/usr/bin/env python3
"""
Tokenomics Coach — what Claude Code usage actually costs, and what was avoidable.

Reads ~/.claude/projects/*.jsonl and reports on prompt-cache economics rather
than prompt wording. Every finding is produced by an independent rule in
tokenomics/rules/ and written to a JSON report; the HTML page is rendered from
that JSON, so a report can be annotated and re-rendered without touching logs.

  analyze.py --days 30                      # analyse, write JSON + HTML
  analyze.py --mode cache --project ~/app   # one area, one project
  analyze.py --render report.json           # re-render HTML after adding notes

Architecture:
  pricing.py   rates and per-request cost
  collect.py   JSONL -> Dataset (parsing only, no judgement)
  signals.py   shared derived facts: cache drops, sessions, daily, totals
  rules/       one file per check; add a file to add a check
  report.py    the JSON report model
  html.py      report JSON -> self-contained page
"""

from __future__ import annotations
import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tokenomics import collect as collect_mod          # noqa: E402
from tokenomics import html as html_mod                # noqa: E402
from tokenomics import report as report_mod            # noqa: E402
from tokenomics.pricing import load_pricing            # noqa: E402
from tokenomics.rules import evaluate_all              # noqa: E402
from tokenomics.signals import Context                 # noqa: E402

DEFAULT_DAYS = 30


def default_paths(explicit_html: str | None, explicit_json: str | None):
    stem = Path.home() / f"tokenomics-report-{datetime.now().date().isoformat()}"
    html = Path(os.path.expanduser(explicit_html)) if explicit_html \
        else stem.with_suffix(".html")
    if explicit_json:
        js = Path(os.path.expanduser(explicit_json))
    else:
        js = html.with_suffix(".json")   # always sits beside the page
    return html, js


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Audit Claude Code logs for avoidable token spend.")
    ap.add_argument("--mode", default="all",
                    choices=["all", "cache", "sessions", "models"],
                    help="Which family of rules to run (default: all)")
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS,
                    help=f"Days of history to scan (default: {DEFAULT_DAYS})")
    ap.add_argument("--project", type=str, default=None,
                    help="Limit to one project path, e.g. ~/code/my-app")
    ap.add_argument("--projects-root", type=str,
                    default=str(collect_mod.DEFAULT_PROJECTS_ROOT))
    ap.add_argument("--html", type=str, default=None,
                    help="HTML output path (default: ~/tokenomics-report-<date>.html)")
    ap.add_argument("--json", type=str, default=None,
                    help="JSON report path (default: alongside the HTML)")
    ap.add_argument("--no-html", action="store_true", help="Skip the HTML page")
    ap.add_argument("--render", type=str, default=None, metavar="REPORT.json",
                    help="Re-render HTML from an existing report JSON and exit. "
                         "Use after adding notes to it.")
    ap.add_argument("--pretty", action="store_true",
                    help="Pretty-print the report to stdout")
    args = ap.parse_args()

    # Re-render path: no log access at all.
    if args.render:
        src = Path(os.path.expanduser(args.render))
        try:
            report = report_mod.load(src)
        except (OSError, ValueError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        out = Path(os.path.expanduser(args.html)) if args.html \
            else src.with_suffix(".html")
        html_mod.write(report, out)
        print(f"rendered {out}")
        return 0

    pricing = load_pricing()
    root = Path(os.path.expanduser(args.projects_root))
    dirs = collect_mod.resolve_project_dirs(args.project, root)
    if not dirs:
        print(f"error: no project logs found under {root}", file=sys.stderr)
        return 1
    files = collect_mod.list_jsonl(dirs, args.days)
    if not files:
        print(f"error: no .jsonl logs modified in the last {args.days} days under "
              f"{root}. Try a longer --days, or check --project.", file=sys.stderr)
        return 1

    ds = collect_mod.collect(files, args.days, pricing)
    if not ds.requests:
        print(f"error: scanned {len(files)} file(s) but found no billable requests in "
              f"the last {args.days} days.", file=sys.stderr)
        return 1

    ctx = Context(ds, pricing)
    findings = evaluate_all(ctx, args.mode)
    report = report_mod.build(ds, ctx, findings, args.mode)

    html_path, json_path = default_paths(args.html, args.json)
    try:
        json_path.parent.mkdir(parents=True, exist_ok=True)
        report_mod.save(report, json_path)
        report["_json_path"] = str(json_path)
    except OSError as e:
        print(f"warning: could not write JSON report ({e})", file=sys.stderr)
        json_path = None

    if not args.no_html:
        try:
            html_mod.write(report, html_path)
            report["_html_path"] = str(html_path)
        except OSError as e:
            print(f"warning: could not write HTML report ({e})", file=sys.stderr)
            html_path = None
    else:
        html_path = None

    s = report["summary"]
    print(f"{report['meta']['days']} days · ${s['total_spend_usd']:,.2f} spent · "
          f"${s['measured_waste_usd']:,.2f} measured waste + "
          f"${s['estimated_saving_usd']:,.2f} estimated savings "
          f"({s['pct_of_spend']}% of spend)")
    for i, f in enumerate(report["findings"], 1):
        print(f"  {i}. ${f['usd']:>8,.2f}  {f['title']}  [{f['confidence']}]")
    if json_path:
        print(f"json: {json_path}")
    if html_path:
        print(f"html: {html_path}")

    if args.pretty:
        import json as _json
        print(_json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
