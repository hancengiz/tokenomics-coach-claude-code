"""Model rates and per-request costing.

Multipliers against base input, from platform.claude.com (verified 2026-08-07):
  5-minute cache write 1.25x · 1-hour cache write 2x · cache read 0.1x
"""

from __future__ import annotations
import json
import sys
from pathlib import Path

PRICING_PATH = Path(__file__).resolve().parent.parent / "pricing.json"

FALLBACK = {"input": 3.0, "output": 15.0,
            "cache_write_5m": 3.75, "cache_write_1h": 6.0, "cache_read": 0.3}


def load_pricing(path: Path | None = None) -> dict:
    try:
        return json.loads((path or PRICING_PATH).read_text())
    except Exception as e:
        print(f"warning: could not load pricing.json ({e}); using fallback rates",
              file=sys.stderr)
        return {"models": {}, "families": {}, "fallback": dict(FALLBACK)}


def rates_for(model: str, pricing: dict) -> dict:
    """Resolve a model id to its rate table: exact match, then family, then default."""
    fallback = pricing.get("fallback", FALLBACK)
    if not model or model == "<synthetic>":
        return fallback
    mn = model.lower()
    for entry in pricing.get("models", {}).values():
        for pat in entry.get("match", []):
            if pat.lower() in mn:
                return entry
    for family, key in pricing.get("families", {}).items():
        if family in mn:
            entry = pricing.get("models", {}).get(key)
            if entry:
                return entry
    return fallback


def split_cache_writes(usage: dict) -> tuple[int, int]:
    """Return (1h tokens, 5m tokens). Entries predating the per-TTL breakdown
    report only a lump sum; attribute those to the 5m rate."""
    cc = usage.get("cache_creation") or {}
    w1h = cc.get("ephemeral_1h_input_tokens", 0)
    w5m = cc.get("ephemeral_5m_input_tokens", 0)
    if not cc:
        w5m = usage.get("cache_creation_input_tokens", 0)
    return w1h, w5m


def cost_of(usage: dict, model: str, pricing: dict) -> float:
    r = rates_for(model, pricing)
    w1h, w5m = split_cache_writes(usage)
    return (
        usage.get("input_tokens", 0) / 1e6 * r["input"]
        + usage.get("output_tokens", 0) / 1e6 * r["output"]
        + w1h / 1e6 * r["cache_write_1h"]
        + w5m / 1e6 * r["cache_write_5m"]
        + usage.get("cache_read_input_tokens", 0) / 1e6 * r["cache_read"]
    )


def write_rate(request: dict, pricing: dict) -> float:
    """The rate this request's cache write was billed at."""
    r = rates_for(request["model"], pricing)
    return r["cache_write_1h"] if request["write_1h"] else r["cache_write_5m"]


def read_rate(request: dict, pricing: dict) -> float:
    return rates_for(request["model"], pricing)["cache_read"]
