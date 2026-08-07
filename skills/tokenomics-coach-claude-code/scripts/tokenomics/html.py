"""Render a report dict to a self-contained HTML page.

Pure function of the report JSON: same input, same output, no log access. Any
`notes` an LLM added to the JSON are rendered alongside the generated content
and clearly marked as commentary.
"""

from __future__ import annotations
from pathlib import Path

from .theme import CSS, JS


def esc(s) -> str:
    # Attributes below are single-quoted, so ' must be escaped or it truncates them.
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&#39;"))


def _fmt(value, col: dict) -> str:
    key = col["key"]
    if value is None:
        return "—"
    if key.endswith("_usd"):
        return f"${float(value):,.2f}"
    if col.get("numeric") and isinstance(value, (int, float)):
        return f"{value:,}" if isinstance(value, int) else f"{value:,.1f}"
    return esc(value)


def _session_cell(row: dict) -> str:
    bits = [f"<div class='nm'>{esc(row.get('title') or row.get('session') or '')}</div>"]
    if row.get("path"):
        bits.append(f"<div class='pth'>{esc(row['path'])}</div>")
    meta = " · ".join(x for x in [row.get("session"),
                                  (row.get("at") or row.get("started") or "")[:16]
                                  .replace("T", " "),
                                  row.get("model")] if x)
    if meta:
        bits.append(f"<div class='sid'>{esc(meta)}</div>")
    return "".join(bits)


def _table(columns: list[dict], rows: list[dict]) -> str:
    if not rows:
        return ""
    head = "".join(
        f"<th title='{esc(c.get('help', ''))}'><span class='ht'>{esc(c['label'])}</span></th>"
        if c.get("help") else f"<th>{esc(c['label'])}</th>"
        for c in columns)
    body = []
    for r in rows:
        cells = []
        for c in columns:
            key = c["key"]
            if key == "session":
                cells.append(f"<td class='wide'>{_session_cell(r)}</td>")
            elif c.get("wide"):
                cells.append(f"<td class='wide'>{esc(r.get(key, ''))}</td>")
            else:
                cls = " class='num'" if c.get("numeric") else ""
                cells.append(f"<td{cls}>{_fmt(r.get(key), c)}</td>")
        body.append("<tr>" + "".join(cells) + "</tr>")
    return (f"<div class='scroll'><table class='sortable'><thead><tr>{head}</tr>"
            f"</thead><tbody>{''.join(body)}</tbody></table></div>")


def _notes(notes: list, label: str = "Note") -> str:
    return "".join(f"<div class='note'><span class='lbl'>{esc(label)}</span>{esc(n)}</div>"
                   for n in (notes or []))


def _chart(daily: list[dict]) -> str:
    if not daily:
        return "<p class='muted'>No daily data.</p>"
    W, H, PAD_L, PAD_B, PAD_T = 900, 260, 54, 46, 12
    peak = max((d["cost_usd"] for d in daily), default=0) or 1
    n = len(daily)
    step = (W - PAD_L - 12) / n
    bw = max(4.0, step * 0.72)
    plot_h = H - PAD_T - PAD_B
    p = [f"<svg viewBox='0 0 {W} {H}' preserveAspectRatio='xMidYMid meet' role='img' "
         f"aria-label='Daily spend split by cost type'>"]
    for f in (0, 0.25, 0.5, 0.75, 1.0):
        y = PAD_T + plot_h * (1 - f)
        p.append(f"<line x1='{PAD_L}' y1='{y:.1f}' x2='{W-8}' y2='{y:.1f}' class='grid'/>")
        p.append(f"<text x='{PAD_L-8}' y='{y+4:.1f}' class='ylab'>${peak*f:.2f}</text>")
    for i, d in enumerate(daily):
        x = PAD_L + i * step + (step - bw) / 2
        y = PAD_T + plot_h
        for key, cls, name in (("read_usd", "b-read", "cache reads"),
                               ("write_usd", "b-write", "cache writes"),
                               ("output_usd", "b-out", "output")):
            h = (d.get(key, 0) or 0) / peak * plot_h
            if h <= 0:
                continue
            y -= h
            p.append(f"<rect x='{x:.1f}' y='{y:.1f}' width='{bw:.1f}' height='{h:.1f}' "
                     f"class='{cls}'><title>{esc(d['date'])} — {name}: "
                     f"${d[key]:.2f}</title></rect>")
        if n <= 20 or i % max(1, n // 12) == 0:
            cx = x + bw / 2
            p.append(f"<text x='{cx:.1f}' y='{H-PAD_B+16}' class='xlab' "
                     f"transform='rotate(-45 {cx:.1f} {H-PAD_B+16})'>{d['date'][5:]}</text>")
    p.append("</svg>")
    return "".join(p)


def render(report: dict) -> str:
    meta, s = report["meta"], report["summary"]
    findings = report.get("findings", [])
    totals = report.get("totals", {})

    cards = [("Window", f"{meta['days']} days", False),
             ("Total spend", f"${s['total_spend_usd']:,.2f}", False),
             ("Measured waste", f"${s['measured_waste_usd']:,.2f}", True),
             ("Estimated savings", f"${s['estimated_saving_usd']:,.2f}", True),
             ("Total opportunity",
              f"${s['total_opportunity_usd']:,.2f} · {s['pct_of_spend']}%", True),
             ("Reuse ratio", f"{totals.get('read_to_write_ratio', 0)}x", False),
             ("Requests", f"{meta['unique_requests']:,}", False)]
    cards_html = "".join(
        f"<div class='card{' hot' if hot else ''}'><div class='k'>{esc(k)}</div>"
        f"<div class='v'>{esc(v)}</div></div>" for k, v, hot in cards)

    acts = "".join(
        f"<li><div class='hd'><span class='n'>{i+1}.</span>"
        f"<span class='t'>{esc(f['title'])}</span>"
        f"<span class='amt'>{('$%.2f' % f['usd']) if f['usd'] else '—'}</span></div></li>"
        for i, f in enumerate(findings)) or "<li class='muted'>Nothing flagged.</li>"

    blocks = []
    for f in findings:
        blocks.append(
            f"<div class='finding' id='{esc(f['id'])}'>"
            f"<div class='top'><h3>{esc(f['title'])}</h3>"
            f"<span class='badge'>{esc(f['confidence'])}</span>"
            f"<span class='amt'>{('$%.2f' % f['usd']) if f['usd'] else ''}</span></div>"
            f"<div class='detail'>{esc(f['detail'])}</div>"
            f"{_notes(f.get('notes'), 'Note')}"
            f"{_table(f.get('columns', []), f.get('events', []))}"
            f"</div>")

    ttl = ", ".join(f"{v} {k}" for k, v in sorted(
        (totals.get("ttl_detection") or {}).items())) or "n/a"

    return f"""<title>Tokenomics Report — {meta['days']}d</title>
<style>{CSS}</style>
<div class="wrap">
<h1>Claude Code Tokenomics</h1>
<p class="sub">Last {meta['days']} days · {meta['files_scanned']} log files ·
{meta['unique_requests']:,} unique requests ({meta['duplicate_entries']:,} duplicates
removed) · generated {esc(report.get('generated_at', ''))}</p>

<div class="cards">{cards_html}</div>
{_notes(report.get('notes'), 'Analyst note')}

<h2>What to change</h2>
<p class="sub" style="margin:2px 0 0">{esc(s['confidence_note'])}</p>
<div class="panel"><ol class="acts">{acts}</ol></div>

<h2>Daily spend</h2>
<div class="panel">{_chart(report.get('daily', []))}
<div class="legend">
<span><i style="background:var(--read)"></i>cache reads — reusing context</span>
<span><i style="background:var(--write)"></i>cache writes — building it</span>
<span><i style="background:var(--out)"></i>output — what the model generated</span>
</div></div>

<h2>Findings</h2>
{''.join(blocks) or "<div class='panel muted'>Nothing flagged in this window.</div>"}

<footer>
Cache lifetime detected per session from the logs: {esc(ttl)}.
Writes: {totals.get('cache_write_1h_tokens', 0):,} tokens long-lived,
{totals.get('cache_write_5m_tokens', 0):,} short-lived; reads
{totals.get('cache_read_tokens', 0):,}.<br>
Rates come from <code>pricing.json</code>. Relative to normal input price: a
short-lived cache write costs 1.25x, a long-lived one 2x, and reading cached
content 0.1x.<br>
Generated from <code>{esc(report.get('schema', ''))}</code>. Re-render after editing
the JSON with <code>analyze.py --render &lt;file&gt;.json</code>.
</footer>
</div>
<script>{JS}</script>
"""


def write(report: dict, path: Path) -> None:
    Path(path).write_text(render(report), encoding="utf-8")
