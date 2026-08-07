"""The report model: the JSON written next to the HTML.

This is the single source of truth for the rendered page. Rules produce it, an
LLM may annotate it by adding `notes`, and the renderer turns it into HTML. That
means a report can be re-rendered after annotation without re-reading any logs:

    analyze.py --render report.json --html report.html

Notes may be added in two places, both optional and both preserved on re-render:
  * top level  "notes": ["..."]              - overall narrative
  * per finding "notes": ["..."]             - commentary on one finding
"""

from __future__ import annotations
import json
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = "tokenomics-report/1"


def build(ds, ctx, findings, mode: str) -> dict:
    measured = sum(f.usd for f in findings if f.confidence == "measured")
    estimated = sum(f.usd for f in findings if f.confidence == "estimated")
    total = ctx.total_cost
    return {
        "schema": SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "mode": mode,
        "meta": ds.meta,
        "totals": ctx.totals,
        "summary": {
            "total_spend_usd": round(total, 2),
            "measured_waste_usd": round(measured, 2),
            "estimated_saving_usd": round(estimated, 2),
            "total_opportunity_usd": round(measured + estimated, 2),
            "pct_of_spend": round((measured + estimated) / total * 100, 1) if total else 0,
            "confidence_note": (
                "Measured waste is arithmetic: the difference between what a rebuild cost "
                "and what reusing the cache would have cost. Estimated savings reprice "
                "real token counts on a cheaper model and assume the work would take the "
                "same number of tokens. Do not merge them into one figure without saying "
                "which is which."),
        },
        "daily": ctx.daily,
        "findings": [f.to_dict() for f in findings],
        "notes": [],
    }


def load(path: Path) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("schema") != SCHEMA:
        raise ValueError(f"{path}: expected schema {SCHEMA!r}, got {data.get('schema')!r}")
    return data


def save(report: dict, path: Path) -> None:
    Path(path).write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
