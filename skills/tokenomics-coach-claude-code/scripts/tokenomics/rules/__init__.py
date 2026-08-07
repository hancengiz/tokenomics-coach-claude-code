"""Rule registry.

Every module in this package that exposes a module-level `rule` is discovered
automatically, so adding a check means adding one file and nothing else. Rules
are independent: one failing or being removed cannot affect the others.
"""

from __future__ import annotations
import importlib
import pkgutil
import sys
from pathlib import Path

from .base import Rule, Finding, Column   # re-exported for rule authors

__all__ = ["Rule", "Finding", "Column", "discover", "evaluate_all"]


def discover() -> list[Rule]:
    """Import every sibling module and collect its `rule`."""
    found: list[Rule] = []
    for mod in pkgutil.iter_modules([str(Path(__file__).parent)]):
        if mod.name.startswith("_") or mod.name == "base":
            continue
        module = importlib.import_module(f"{__name__}.{mod.name}")
        r = getattr(module, "rule", None)
        if r is not None:
            found.append(r)
    found.sort(key=lambda r: r.id)
    return found


def evaluate_all(ctx, mode: str = "all") -> list[Finding]:
    """Run every rule in scope. A rule that raises is skipped with a warning
    rather than taking the whole report down with it."""
    findings: list[Finding] = []
    for r in discover():
        if mode != "all" and mode not in r.modes:
            continue
        try:
            f = r.evaluate(ctx)
        except Exception as e:                      # noqa: BLE001 - isolation is the point
            print(f"warning: rule '{r.id}' failed: {type(e).__name__}: {e}",
                  file=sys.stderr)
            continue
        if f is not None:
            findings.append(f)
    findings.sort(key=lambda f: -f.usd)
    return findings
