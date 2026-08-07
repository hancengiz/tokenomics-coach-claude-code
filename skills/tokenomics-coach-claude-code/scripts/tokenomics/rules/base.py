"""The contract every rule implements.

A rule owns exactly one thing worth telling the user about. It reads the shared
Context, and returns a Finding or None. Rules never call each other, never
mutate the Context, and never render HTML — so adding, changing or deleting one
cannot affect any other.

To add a rule: drop a module in this package exposing `rule = MyRule()`. It is
discovered automatically and appears in the JSON report and the HTML.
"""

from __future__ import annotations
from dataclasses import dataclass, field


@dataclass
class Column:
    """One column of a finding's evidence table."""
    key: str
    label: str
    help: str = ""          # tooltip; say what the number means in plain words
    numeric: bool = False   # right-aligned and sorted numerically
    wide: bool = False      # allowed to wrap (explanations, paths)


@dataclass
class Finding:
    id: str
    title: str
    usd: float                       # what this is worth; 0.0 if not costed
    detail: str                      # the explanation shown under the title
    confidence: str = "measured"     # 'measured' | 'estimated'
    severity: str = "info"           # 'waste' | 'opportunity' | 'info'
    count: int = 0
    events: list[dict] = field(default_factory=list)
    columns: list[Column] = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)   # filled in by an LLM later

    def to_dict(self) -> dict:
        return {
            "id": self.id, "title": self.title, "usd": round(self.usd, 2),
            "confidence": self.confidence, "severity": self.severity,
            "count": self.count, "detail": self.detail,
            "stats": self.stats, "notes": list(self.notes),
            "columns": [c.__dict__ for c in self.columns],
            "events": self.events,
        }


class Rule:
    """Subclass and implement `evaluate`."""

    id: str = "unnamed"
    title: str = "Unnamed rule"
    # Which --mode values this rule belongs to. 'all' always includes it.
    modes: tuple[str, ...] = ("cache",)

    def evaluate(self, ctx) -> Finding | None:
        raise NotImplementedError

    # Convenience for subclasses
    @staticmethod
    def usd(x: float) -> str:
        return f"${x:,.2f}"
