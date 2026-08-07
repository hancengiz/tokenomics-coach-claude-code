"""Stylesheet and behaviour for the rendered report. Self-contained by design:
no external fonts, scripts or images, so the file works offline and anywhere."""

CSS = """
:root{--bg:#fbfbfa;--panel:#fff;--fg:#1a1a19;--muted:#6b6b68;--line:#e5e5e2;
--accent:#c8613a;--read:#4a7a63;--write:#c8613a;--out:#7a6ba8;--warn:#b4442a;
--note:#3f6ea8;--noteBg:#eef3fa}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){
--bg:#191917;--panel:#222220;--fg:#f0efec;--muted:#a3a29d;--line:#34342f;
--accent:#e08a5f;--read:#6fae8f;--write:#e08a5f;--out:#a294cc;--warn:#e5714f;
--note:#8fb4e0;--noteBg:#1e2733}}
:root[data-theme=dark]{--bg:#191917;--panel:#222220;--fg:#f0efec;--muted:#a3a29d;
--line:#34342f;--accent:#e08a5f;--read:#6fae8f;--write:#e08a5f;--out:#a294cc;
--warn:#e5714f;--note:#8fb4e0;--noteBg:#1e2733}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.6 ui-sans-serif,
-apple-system,'Segoe UI',Roboto,sans-serif;padding:32px 20px}
.wrap{max-width:1120px;margin:0 auto}
h1{font-size:27px;margin:0 0 4px;letter-spacing:-.02em}
h2{font-size:19px;margin:38px 0 6px;letter-spacing:-.01em}
h3{font-size:16px;margin:0 0 6px}
.muted{color:var(--muted)}
.sub{color:var(--muted);font-size:14px;margin:0 0 24px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.card .k{font-size:11.5px;color:var(--muted);text-transform:uppercase;letter-spacing:.06em}
.card .v{font-size:23px;font-weight:600;margin-top:4px;font-variant-numeric:tabular-nums}
.card.hot .v{color:var(--warn)}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;
padding:16px 18px;margin-top:10px}
.finding{background:var(--panel);border:1px solid var(--line);border-radius:10px;
padding:18px 20px;margin:14px 0}
.finding .top{display:flex;gap:12px;align-items:baseline;flex-wrap:wrap;margin-bottom:8px}
.finding .amt{margin-left:auto;font-size:20px;font-weight:600;color:var(--warn);
font-variant-numeric:tabular-nums}
.finding .detail{color:var(--fg);opacity:.9;font-size:14.5px;max-width:78ch}
.badge{display:inline-block;padding:2px 9px;border-radius:99px;font-size:11.5px;
border:1px solid var(--line);color:var(--muted);text-transform:uppercase;letter-spacing:.05em}
.note{background:var(--noteBg);border-left:3px solid var(--note);border-radius:0 8px 8px 0;
padding:10px 14px;margin:12px 0 0;font-size:14px}
.note .lbl{color:var(--note);font-weight:600;font-size:11.5px;text-transform:uppercase;
letter-spacing:.06em;display:block;margin-bottom:3px}
.anon{background:var(--noteBg);border:1px dashed var(--note);border-radius:10px;
padding:10px 14px;margin:0 0 18px;font-size:13.5px;max-width:78ch}
.anon .lbl{color:var(--note);font-weight:600;font-size:11.5px;text-transform:uppercase;
letter-spacing:.06em;display:block;margin-bottom:3px}
ol.acts{list-style:none;counter-reset:a;padding:0;margin:0}
ol.acts li{counter-increment:a;border-bottom:1px solid var(--line);padding:11px 0}
ol.acts li:last-child{border-bottom:0}
ol.acts .hd{display:flex;gap:10px;align-items:baseline;flex-wrap:wrap}
ol.acts .n{color:var(--muted);font-variant-numeric:tabular-nums}
ol.acts .t{font-weight:600}
ol.acts .amt{margin-left:auto;color:var(--warn);font-weight:600;font-variant-numeric:tabular-nums}
.scroll{overflow-x:auto;border:1px solid var(--line);border-radius:10px;
background:var(--panel);margin-top:12px}
table{border-collapse:collapse;width:100%;font-size:13.5px}
th,td{padding:9px 12px;text-align:left;border-bottom:1px solid var(--line);
white-space:nowrap;vertical-align:top}
th{font-size:11.5px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);
background:var(--panel);position:sticky;top:0}
th .ht{border-bottom:1px dotted var(--muted);cursor:help}
table.sortable th{cursor:pointer;user-select:none}
table.sortable th:hover{color:var(--fg)}
tbody tr:last-child td{border-bottom:0}
td{font-variant-numeric:tabular-nums}
td.num{text-align:right}
td.wide{white-space:normal;max-width:54ch;font-size:13px}
td .nm{font-weight:600;white-space:normal;max-width:32ch}
td .pth{color:var(--muted);font-size:12px;font-family:ui-monospace,SFMono-Regular,Menlo,
monospace;white-space:normal;word-break:break-all;max-width:36ch;margin-top:2px}
td .sid{color:var(--muted);font-size:11.5px;margin-top:2px;white-space:normal}
svg{width:100%;height:auto;display:block}
.grid{stroke:var(--line);stroke-width:1}
.ylab,.xlab{fill:var(--muted);font-size:10px;text-anchor:end}
.b-read{fill:var(--read)}.b-write{fill:var(--write)}.b-out{fill:var(--out)}
.legend{display:flex;gap:18px;flex-wrap:wrap;margin-top:10px;font-size:13px;color:var(--muted)}
.legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:6px}
footer{margin-top:40px;color:var(--muted);font-size:12.5px;border-top:1px solid var(--line);
padding-top:14px;line-height:1.7}
"""

JS = """
document.querySelectorAll('table.sortable').forEach(function(t){
  t.querySelectorAll('th').forEach(function(th,i){
    th.addEventListener('click',function(){
      var tb=t.tBodies[0], rows=Array.prototype.slice.call(tb.rows);
      var asc=th.dataset.asc!=='1'; th.dataset.asc=asc?'1':'0';
      rows.sort(function(a,b){
        var x=(a.cells[i]||{}).textContent||'', y=(b.cells[i]||{}).textContent||'';
        var nx=parseFloat(x.replace(/[$,\\s]/g,'')), ny=parseFloat(y.replace(/[$,\\s]/g,''));
        if(!isNaN(nx)&&!isNaN(ny)) return asc?nx-ny:ny-nx;
        return asc?x.trim().localeCompare(y.trim()):y.trim().localeCompare(x.trim());
      });
      rows.forEach(function(r){tb.appendChild(r)});
    });
  });
});
"""
