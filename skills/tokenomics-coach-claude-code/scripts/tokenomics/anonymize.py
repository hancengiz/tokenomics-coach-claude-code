"""Turn a report into a copy that is safe to hand to someone else.

The JSON report is the source of truth, so anonymising is a pure transform of
it — same input, same output, no log access. Two passes run over a deep copy:

  1. structured — every session row's title, working directory, project and ids
     become stable placeholders: "Session 3", "~/projects/project-2", "sess0003".
  2. free text — those same real values are then substituted everywhere else
     they appear, because notes are written by hand and may quote them. A
     catch-all pass follows for home directories and e-mail addresses.

Numbering is stable within a report: the same session is "Session 3" in every
table, so the anonymised page still reads as a coherent story.

Deliberately kept: dollars, token counts, timings, dates, model names. Strip
those and the report says nothing. Deliberately not attempted: proving that no
identifying string survives — free text written by hand can always name
something this cannot know about, which is why `anonymize` also returns a
legend the author can check the result against.
"""

from __future__ import annotations
import copy
import re
from pathlib import Path

# A dict is a session row if it identifies a session. Finding dicts also carry a
# "title", but no session key, so they keep their real (generated) titles.
SESSION_KEYS = ("session_id", "session")
PROJECT_KEYS = ("path", "project")
SCRUBBED_KEYS = ("title", "path", "project", "session_id", "session")

# Substituting a short string into free text does more damage than good:
# a three-letter project name matches inside ordinary words.
MIN_NEEDLE = 4

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
HOME_RE = re.compile(r"(/Users/|/home/)[^/\s\"']+")


def _distinctive(name: str) -> bool:
    """Whether a bare directory name is safe to substitute into prose.

    A full path is unmistakable, but its last segment may be an ordinary word —
    replacing every "work" in the write-up would mangle it for no privacy gain.
    """
    return len(name) >= 6 or bool(re.search(r"[-_.\d]", name))


def _dicts(obj):
    """Every dict in the report, depth first."""
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _dicts(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _dicts(v)


def session_rows(report: dict) -> list[dict]:
    return [d for d in _dicts(report) if any(k in d for k in SESSION_KEYS)]


class Aliases:
    """Assigns each real session and project a number, stable across the report.

    Sessions are keyed by both the full id and the short one, projects by both
    the working directory and the escaped log-directory name, so a row that
    carries either form resolves to the same number.
    """

    def __init__(self, rows: list[dict]):
        self.sessions: dict[str, int] = {}
        self.projects: dict[str, int] = {}
        self._n_sessions = 0
        self._n_projects = 0
        for row in rows:
            self._assign(row, SESSION_KEYS)
            self._assign(row, PROJECT_KEYS)

    def _table(self, keys) -> dict[str, int]:
        return self.sessions if keys is SESSION_KEYS else self.projects

    def _assign(self, row: dict, keys) -> int | None:
        table = self._table(keys)
        values = [str(row[k]) for k in keys if row.get(k)]
        if not values:
            return None
        n = next((table[v] for v in values if v in table), None)
        if n is None:
            attr = "_n_sessions" if keys is SESSION_KEYS else "_n_projects"
            n = getattr(self, attr) + 1
            setattr(self, attr, n)
        for v in values:
            table.setdefault(v, n)
        return n

    def lookup(self, row: dict, keys) -> int | None:
        table = self._table(keys)
        return next((table[str(row[k])] for k in keys
                     if row.get(k) and str(row[k]) in table), None)

    def replacements(self, rows: list[dict]) -> dict[str, str]:
        """real string -> placeholder, first mapping wins on collision."""
        out: dict[str, str] = {}
        for row in rows:
            sn = self.lookup(row, SESSION_KEYS)
            pn = self.lookup(row, PROJECT_KEYS)
            if row.get("title") and sn:
                out.setdefault(str(row["title"]), f"Session {sn}")
            if row.get("path") and pn:
                real = str(row["path"])
                out.setdefault(real, f"~/projects/project-{pn}")
                # Prose usually writes the same directory in tilde form.
                if real.startswith(str(Path.home()) + "/"):
                    out.setdefault("~" + real[len(str(Path.home())):],
                                   f"~/projects/project-{pn}")
                # The directory name alone shows up in prose as the project name.
                if _distinctive(Path(real).name):
                    out.setdefault(Path(real).name, f"project-{pn}")
            if row.get("project") and pn:
                out.setdefault(str(row["project"]), f"project-{pn}")
            if row.get("session_id") and sn:
                out.setdefault(str(row["session_id"]), f"session-{sn:04d}")
            if row.get("session") and sn:
                out.setdefault(str(row["session"]), f"sess{sn:04d}")
        return out


def legend(rows: list[dict], aliases: Aliases) -> dict:
    """What each placeholder stands for. Private — never written to the copy."""
    sessions: dict[int, dict] = {}
    projects: dict[int, dict] = {}
    for row in rows:
        sn = aliases.lookup(row, SESSION_KEYS)
        if sn and sn not in sessions:
            sessions[sn] = {"placeholder": f"Session {sn}",
                            "title": row.get("title"),
                            "session_id": row.get("session_id") or row.get("session")}
        pn = aliases.lookup(row, PROJECT_KEYS)
        if pn and pn not in projects:
            projects[pn] = {"placeholder": f"project-{pn}",
                            "path": row.get("path"),
                            "log_directory": row.get("project")}
    return {"sessions": [sessions[k] for k in sorted(sessions)],
            "projects": [projects[k] for k in sorted(projects)]}


def _needles(repl: dict[str, str]) -> list[tuple[str, str]]:
    """Free-text substitutions, longest first so a path wins over its own basename."""
    pairs = [(real, fake) for real, fake in repl.items() if len(real) >= MIN_NEEDLE]
    home = str(Path.home())
    if len(home) >= MIN_NEEDLE:
        pairs.append((home, "/Users/user"))
    if len(Path.home().name) >= MIN_NEEDLE:
        pairs.append((Path.home().name, "user"))
    return sorted(pairs, key=lambda kv: -len(kv[0]))


def _redact(text: str, needles: list[tuple[str, str]]) -> str:
    for real, fake in needles:
        if real in text:
            text = text.replace(real, fake)
    text = HOME_RE.sub(r"\1user", text)
    return EMAIL_RE.sub("user@example.com", text)


def _scrub_text(obj, needles):
    if isinstance(obj, dict):
        return {k: _scrub_text(v, needles) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_scrub_text(v, needles) for v in obj]
    if isinstance(obj, str):
        return _redact(obj, needles)
    return obj


def anonymize(report: dict) -> tuple[dict, dict]:
    """Return (shareable copy, legend). The original is not touched."""
    out = copy.deepcopy(report)
    # Internal bookkeeping holds absolute output paths.
    for key in [k for k in out if k.startswith("_")]:
        out.pop(key)

    rows = session_rows(out)
    aliases = Aliases(rows)
    repl = aliases.replacements(rows)
    key = legend(rows, aliases)

    # Structured first: these fields are replaced whole, whatever their length.
    for row in rows:
        for field in SCRUBBED_KEYS:
            value = row.get(field)
            if isinstance(value, str) and value in repl:
                row[field] = repl[value]

    out = _scrub_text(out, _needles(repl))
    out["anonymized"] = True
    return out, key
