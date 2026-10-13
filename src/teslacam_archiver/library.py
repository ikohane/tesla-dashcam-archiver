"""The ledger (what has been built, on this computer) and the library index
(``library.json`` + ``index.html``, kept in the library itself so anyone who
can open the folder can browse it)."""
from __future__ import annotations

import html
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .events import Event, KIND_LABEL

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    event_key    TEXT PRIMARY KEY,      -- '<kind>/<name>'
    kind         TEXT NOT NULL,
    name         TEXT NOT NULL,
    origin       TEXT,                  -- incoming | adopt:<path> | import:<dir>
    status       TEXT NOT NULL,         -- pending | done | failed
    signature    TEXT,                  -- of the raw files the composite was built from
    attempts     INTEGER NOT NULL DEFAULT 0,
    error        TEXT,
    raw_files    INTEGER, raw_bytes INTEGER,
    composite    TEXT, poster TEXT,     -- library-relative paths
    size_bytes   INTEGER, sha256 TEXT, duration_sec REAL, width INTEGER, height INTEGER,
    taken_at     TEXT, caption TEXT, city TEXT, gps_lat REAL, gps_lon REAL,
    reason       TEXT, car TEXT,
    updated_at   TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT, finished_at TEXT, status TEXT, error TEXT,
    promoted_events INTEGER, promoted_files INTEGER, waiting INTEGER,
    built INTEGER, failed INTEGER, remaining INTEGER,
    encrypted INTEGER, conflicts INTEGER, note TEXT
);
"""

MAX_ATTEMPTS = 3   # failed builds of the same raw bytes before holding an event back


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class Ledger:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path, timeout=300)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def upsert(self, ev: Event, **cols: Any) -> None:
        cols["updated_at"] = now_iso()
        row = self.conn.execute("SELECT 1 FROM events WHERE event_key=?", (ev.key,)).fetchone()
        if row is None:
            base = {"event_key": ev.key, "kind": ev.kind, "name": ev.name, "status": "pending"}
            base.update(cols)
            self.conn.execute(f"INSERT INTO events ({','.join(base)}) "
                              f"VALUES ({','.join('?' * len(base))})", list(base.values()))
        else:
            self.conn.execute(f"UPDATE events SET {','.join(f'{k}=?' for k in cols)} "
                              "WHERE event_key=?", [*cols.values(), ev.key])

    def get(self, key: str) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM events WHERE event_key=?", (key,)).fetchone()

    def pending(self, events: List[Event]) -> List[Event]:
        """Events that need a (re)build, newest first."""
        rows = {r["event_key"]: r for r in self.conn.execute(
            "SELECT event_key, status, signature, attempts FROM events")}
        todo = []
        for ev in events:
            r = rows.get(ev.key)
            sig = ev.signature()
            if r is None:
                todo.append(ev)
            elif r["status"] == "done" and r["signature"] == sig:
                continue
            elif r["status"] == "failed" and r["signature"] == sig and r["attempts"] >= MAX_ATTEMPTS:
                continue                       # held back until the raw files change
            else:
                todo.append(ev)
        return sorted(todo, key=lambda e: e.name, reverse=True)

    def record_failure(self, ev: Event, error: str) -> int:
        prev = self.get(ev.key)
        sig = ev.signature()
        attempts = (prev["attempts"] + 1) if prev and prev["signature"] == sig else 1
        self.upsert(ev, status="failed", signature=sig, attempts=attempts, error=error[:500])
        self.conn.commit()
        return attempts

    def start_run(self) -> int:
        rid = self.conn.execute("INSERT INTO runs (started_at, status) VALUES (?, 'running')",
                                (now_iso(),)).lastrowid
        self.conn.commit()
        return int(rid)

    def finish_run(self, rid: int, rec: Dict[str, Any]) -> None:
        cols = ["finished_at", "status", "error", "promoted_events", "promoted_files", "waiting",
                "built", "failed", "remaining", "encrypted", "conflicts", "note"]
        rec = {**rec, "finished_at": now_iso()}
        self.conn.execute(f"UPDATE runs SET {','.join(c + '=?' for c in cols)} WHERE id=?",
                          [rec.get(c) for c in cols] + [rid])
        self.conn.commit()

    def done_events(self) -> List[Dict[str, Any]]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM events WHERE status='done' ORDER BY taken_at DESC, name DESC")]

    def summary(self, runs: int = 10) -> Dict[str, Any]:
        t = dict(self.conn.execute(
            "SELECT COUNT(*) total, SUM(status='done') done, SUM(status='pending') pending, "
            "SUM(status='failed') failed, COALESCE(SUM(CASE WHEN status='done' THEN duration_sec END),0) secs, "
            "COALESCE(SUM(CASE WHEN status='done' THEN size_bytes END),0) bytes FROM events").fetchone())
        fails = [dict(r) for r in self.conn.execute(
            "SELECT event_key, attempts, error FROM events WHERE status='failed' "
            "ORDER BY updated_at DESC LIMIT 10")]
        rr = [dict(r) for r in self.conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (runs,))]
        return {"events": {k: t[k] or 0 for k in ("total", "done", "pending", "failed")},
                "hours": round((t["secs"] or 0) / 3600, 1), "composite_bytes": t["bytes"] or 0,
                "failures": fails, "runs": rr}


# ---- index ------------------------------------------------------------------

def library_json(events: List[Dict[str, Any]]) -> str:
    keep = ("event_key", "kind", "name", "taken_at", "caption", "city", "gps_lat", "gps_lon",
            "reason", "car", "composite", "poster", "duration_sec", "size_bytes", "sha256",
            "width", "height")
    return json.dumps({"generated_at": now_iso(),
                       "events": [{k: e.get(k) for k in keep} for e in events]}, indent=1)


_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>Tesla dashcam library</title>
<style>
:root{--bg:#f6f6f4;--fg:#1d1d1f;--muted:#6b6b70;--card:#fff;--line:#e3e3e0;--accent:#c8102e}
@media (prefers-color-scheme:dark){:root{--bg:#121214;--fg:#ececee;--muted:#9a9aa1;--card:#1c1c20;--line:#2c2c31;--accent:#ff4d5e}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 -apple-system,system-ui,Segoe UI,Roboto,sans-serif}
header{padding:20px 16px 8px;max-width:1200px;margin:auto}h1{font-size:22px;margin:0 0 4px}
.sub{color:var(--muted);font-size:13px}.bar{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}
input,select{font:inherit;padding:7px 10px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--fg)}
input{flex:1;min-width:180px}main{max-width:1200px;margin:auto;padding:8px 16px 40px;
display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden;cursor:pointer}
.card img{width:100%;aspect-ratio:4/3;object-fit:cover;display:block;background:#000}
.meta{padding:9px 11px}.when{font-weight:600;font-size:14px}.cap{color:var(--muted);font-size:13px}
.tag{display:inline-block;font-size:11px;padding:1px 7px;border-radius:99px;border:1px solid var(--line);margin-right:4px}
.tag.sentry{color:var(--accent);border-color:var(--accent)}
dialog{border:0;border-radius:12px;padding:0;max-width:min(1100px,96vw);background:#000}
dialog video{width:100%;display:block;max-height:80vh}dialog .d{background:var(--card);color:var(--fg);padding:10px 14px;font-size:13px}
dialog a{color:var(--accent)}.empty{color:var(--muted);padding:40px 16px;text-align:center}
</style></head><body>
<header><h1>Tesla dashcam library</h1>
<div class="sub" id="sub"></div>
<div class="bar"><input id="q" placeholder="Search caption, city, date…">
<select id="kind"><option value="">All kinds</option><option value="sentry">Sentry</option>
<option value="saved">Saved</option><option value="recent">Drives</option><option value="track">Track</option></select>
<select id="year"><option value="">All years</option></select></div></header>
<main id="grid"></main>
<dialog id="dlg"><video id="vid" controls preload="metadata"></video><div class="d" id="info"></div></dialog>
<script id="data" type="application/json">__DATA__</script>
<script>
const D=JSON.parse(document.getElementById('data').textContent).events;
const g=document.getElementById('grid'),q=document.getElementById('q'),k=document.getElementById('kind'),y=document.getElementById('year');
[...new Set(D.map(e=>(e.taken_at||e.name).slice(0,4)))].sort().reverse().forEach(v=>{const o=document.createElement('option');o.value=o.textContent=v;y.appendChild(o)});
const hrs=D.reduce((a,e)=>a+(e.duration_sec||0),0)/3600;
document.getElementById('sub').textContent=D.length+(D.length===1?' event':' events')+' · '+(hrs<1?Math.round(hrs*60)+' minutes':hrs.toFixed(1)+' hours')+' of footage';
function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
function when(e){const t=(e.taken_at||'').replace('T',' ');return t.slice(0,16)}
function draw(){const s=q.value.toLowerCase();const L=D.filter(e=>(!k.value||e.kind===k.value)&&(!y.value||(e.taken_at||e.name).startsWith(y.value))&&(!s||(when(e)+' '+(e.caption||'')+' '+(e.city||'')).toLowerCase().includes(s)));
g.innerHTML=L.length?L.map((e,i)=>'<div class="card" data-i="'+D.indexOf(e)+'"><img loading="lazy" src="'+esc(e.poster||'')+'" alt=""><div class="meta"><div class="when">'+esc(when(e))+'</div><div class="cap"><span class="tag '+esc(e.kind)+'">'+esc(e.kind)+'</span>'+esc(e.caption||'')+'</div></div></div>').join(''):'<div class="empty">No events match.</div>';}
g.onclick=ev=>{const c=ev.target.closest('.card');if(!c)return;const e=D[+c.dataset.i];const v=document.getElementById('vid');v.src=e.composite;v.poster=e.poster||'';
document.getElementById('info').innerHTML=esc(when(e))+' · '+esc(e.caption||'')+(e.gps_lat!=null?' · <a target="_blank" rel="noopener" href="https://www.openstreetmap.org/?mlat='+e.gps_lat+'&mlon='+e.gps_lon+'#map=17/'+e.gps_lat+'/'+e.gps_lon+'">map</a>':'')+' · <a href="'+esc(e.composite)+'" download>download</a>';
document.getElementById('dlg').showModal();v.play().catch(()=>{});};
document.getElementById('dlg').addEventListener('close',()=>{const v=document.getElementById('vid');v.pause();v.removeAttribute('src');v.load();});
document.getElementById('dlg').onclick=ev=>{if(ev.target.id==='dlg')ev.target.close()};
[q,k,y].forEach(el=>el.addEventListener('input',draw));draw();
</script></body></html>
"""


def index_html(events: List[Dict[str, Any]]) -> str:
    """A self-contained gallery. The data is embedded, so it works when opened
    straight from a folder (file://) as well as from a web server. Paths are
    relative to the library root, where the page is written."""
    data = library_json(events).replace("</", "<\\/")
    return _PAGE.replace("__DATA__", data)
