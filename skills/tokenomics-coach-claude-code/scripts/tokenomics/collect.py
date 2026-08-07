"""Read Claude Code JSONL logs into a deduplicated, ordered dataset.

Nothing here judges anything — parsing only. Rules consume the result.
"""

from __future__ import annotations
import json
import os
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path

from .pricing import cost_of, split_cache_writes

DEFAULT_PROJECTS_ROOT = Path.home() / ".claude" / "projects"


@dataclass
class Dataset:
    requests: list[dict] = field(default_factory=list)
    human_prompts: list[dict] = field(default_factory=list)
    compactions: list[dict] = field(default_factory=list)
    sessions_meta: dict[str, dict] = field(default_factory=dict)
    meta: dict = field(default_factory=dict)

    def lanes(self) -> dict[tuple, list[dict]]:
        """Requests grouped into (session, sidechain) lanes, chronologically.

        Subagents run a separate conversation with a separate cache, so they are
        a separate lane and get their own TTL.
        """
        out: dict[tuple, list[dict]] = defaultdict(list)
        for r in self.requests:
            out[(r["session"], r["sidechain"])].append(r)
        return out

    def label(self, sid: str) -> str:
        """Claude's own chat title, else the opening prompt, else the short id."""
        m = self.sessions_meta.get(sid) or {}
        if m.get("title"):
            return m["title"]
        p = m.get("first_prompt")
        if p:
            return (p[:80] + "…") if len(p) > 80 else p
        return sid[:8]

    def path(self, sid: str, fallback: str = "") -> str:
        """The real working directory, not the escaped log-directory name."""
        return (self.sessions_meta.get(sid) or {}).get("cwd") or fallback


def escape_project_path(p: str) -> str:
    return os.path.abspath(os.path.expanduser(p)).replace("/", "-").replace(".", "-")


def resolve_project_dirs(project: str | None, root: Path) -> list[Path]:
    if not root.exists():
        return []
    if not project:
        return [d for d in root.iterdir() if d.is_dir()]
    esc = escape_project_path(project)
    direct = root / esc
    if direct.exists():
        return [direct]
    stem = esc.lstrip("-")
    return [d for d in root.iterdir() if d.is_dir() and stem in d.name] or [direct]


def list_jsonl(dirs: list[Path], days: int) -> list[Path]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    out = []
    for d in dirs:
        if not d.exists():
            continue
        for f in d.glob("*.jsonl"):
            try:
                if datetime.fromtimestamp(f.stat().st_mtime, timezone.utc) >= cutoff:
                    out.append(f)
            except OSError:
                continue
    return sorted(out, key=lambda p: p.stat().st_mtime, reverse=True)


def parse_ts(s: str):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None


def _is_human_turn(entry: dict, msg: dict) -> bool:
    """True for a real human prompt. Tool results come back as 'user' entries
    too, but carry tool_result blocks rather than text."""
    if entry.get("type") != "user" or entry.get("isMeta"):
        return False
    c = msg.get("content")
    if isinstance(c, str):
        return True
    return isinstance(c, list) and any(
        isinstance(b, dict) and b.get("type") == "text" for b in c)


def _prompt_text(msg: dict) -> str:
    c = msg.get("content")
    if isinstance(c, str):
        txt = c
    else:
        txt = " ".join(b.get("text", "") for b in (c or [])
                       if isinstance(b, dict) and b.get("type") == "text")
    return " ".join(txt.split())


def collect(files: list[Path], days: int, pricing: dict) -> Dataset:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    seen: set = set()
    ds = Dataset()
    meta_default = lambda: {"project": None, "compactions": 0, "plan_toggles": 0,
                            "title": None, "cwd": None, "first_prompt": None}
    sessions: dict[str, dict] = defaultdict(meta_default)
    total_lines = duplicates = 0

    for f in files:
        project = f.parent.name
        current_mode = "default"   # permissionMode is its own entry; carry forward
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue

        for line in text.splitlines():
            if not line.strip():
                continue
            total_lines += 1
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue

            sid = d.get("sessionId") or d.get("session_id") or "unknown"
            sm = sessions[sid]

            if d.get("aiTitle") and not sm["title"]:
                sm["title"] = str(d["aiTitle"]).strip()
            if d.get("cwd") and not sm["cwd"]:
                sm["cwd"] = d["cwd"]

            if d.get("type") == "permission-mode":
                new_mode = d.get("permissionMode", "default")
                if new_mode != current_mode:
                    sm["plan_toggles"] += 1
                current_mode = new_mode
                continue

            if d.get("subtype") == "compact_boundary" or d.get("isCompactSummary"):
                t = parse_ts(d.get("timestamp") or "")
                sm["compactions"] += 1
                ds.compactions.append({"session": sid, "project": project, "ts": t})
                continue

            msg = d.get("message")
            if not isinstance(msg, dict):
                continue

            if _is_human_turn(d, msg):
                t = parse_ts(d.get("timestamp") or "")
                if t and t >= cutoff:
                    ds.human_prompts.append({"ts": t, "session": sid})
                if not sm["first_prompt"]:
                    txt = _prompt_text(msg)
                    if txt and not txt.startswith("<"):   # skip command wrappers
                        sm["first_prompt"] = txt[:120]
                continue

            usage = msg.get("usage")
            if not usage:
                continue
            t = parse_ts(d.get("timestamp") or "")
            if t is None or t < cutoff:
                continue

            key = (msg.get("id"), d.get("requestId"))
            if key in seen:
                duplicates += 1
                continue
            seen.add(key)

            w1h, w5m = split_cache_writes(usage)
            model = msg.get("model") or "<unknown>"
            sm["project"] = project
            ds.requests.append({
                "ts": t, "session": sid, "project": project, "model": model,
                "sidechain": bool(d.get("isSidechain")), "mode": current_mode,
                "effort": d.get("effort"),
                "write_1h": w1h, "write_5m": w5m,
                "read": usage.get("cache_read_input_tokens", 0),
                "input": usage.get("input_tokens", 0),
                "output": usage.get("output_tokens", 0),
                "cost": cost_of(usage, model, pricing),
            })

    ds.requests.sort(key=lambda r: r["ts"])
    ds.human_prompts.sort(key=lambda h: h["ts"])
    ds.sessions_meta = dict(sessions)
    ds.meta = {"files_scanned": len(files), "total_lines": total_lines,
               "duplicate_entries": duplicates, "unique_requests": len(ds.requests),
               "days": days}
    return ds
