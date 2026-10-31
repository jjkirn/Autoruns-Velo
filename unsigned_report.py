#!/usr/bin/env python3
"""
unsigned_report.py

Build unsigned.html, a self-contained page that lists Autoruns entries with no
trustworthy code signature, so they are not lost on a 600-800 change day.

Two views on one page:
  * Changes           added / modified / removed entries that are unsigned
  * Current inventory every unsigned entry each host has right now

Signer classes (from the Signer text Velociraptor reports):
  No signature       Signer is empty and the entry has an Image Path
  Invalid signature  Signer starts with "(Not verified)": a signature is present
                     but does not validate (e.g. claims "Microsoft Corporation")
  No file            Signer is empty and there is no Image Path (nothing to sign), or
                     the Image Path is an Autoruns "File not found: ..." placeholder
                     (a stale autostart entry whose file is gone from disk)

It only READS the SQLite database written by autoruns_velociraptor.py and needs
nothing beyond the Python standard library. No data leaves your machine; the
VirusTotal column is a plain link you may click.

Usage:
  python3 unsigned_report.py --db autoruns.sqlite --out reports/unsigned.html

Optional ignore list (default: unsigned_ignore.txt next to the database): one
pattern per line, matched case-insensitively against Image Path or Launch String
with * and ? wildcards. Lines starting with # are comments, and so is anything
after whitespace followed by # at the end of a pattern line. Matching entries are
hidden by default and shown again with the "Show ignored" switch.
"""

import argparse
import datetime as dt
import fnmatch
import json
import re
import sqlite3
import sys
from pathlib import Path

HISTORY_DAYS = 30
HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")


def table_exists(conn, name):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def classify(rec):
    """Return 'invalid', 'none', 'nofile', or None when the entry is signed."""
    signer = (rec.get("Signer") or "").strip()
    image = (rec.get("Image Path") or "").strip()
    if signer.lower().startswith("(not verified)"):
        return "invalid"
    if not signer:
        if not image or image.lower().startswith("file not found"):
            return "nofile"
        return "none"
    return None


def load_patterns(path):
    pats = []
    p = Path(path)
    if p.exists():
        # utf-8-sig drops the BOM some Windows editors add to the first line.
        for line in p.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            # A "#" after whitespace starts a trailing comment.
            line = re.sub(r"\s+#.*$", "", line.strip()).strip()
            if line and not line.startswith("#"):
                pats.append(line.lower())
    return pats


def is_ignored(rec, pats):
    if not pats:
        return False
    targets = [(rec.get("Image Path") or "").lower(), (rec.get("Launch String") or "").lower()]
    return any(fnmatch.fnmatchcase(t, p) for p in pats for t in targets if t)


def sha_of(rec):
    s = (rec.get("SHA-256") or "").strip()
    return s if HEX64.match(s) else ""


def load_data(db_path, ignore_path):
    conn = sqlite3.connect("file:%s?mode=ro" % Path(db_path).resolve().as_posix(), uri=True)
    pats = load_patterns(ignore_path)

    base = []
    if table_exists(conn, "baseline"):
        base = conn.execute("SELECT host, key, data FROM baseline").fetchall()
    chg = []
    if table_exists(conn, "changes"):
        chg = conn.execute(
            "SELECT host, detected, change_type, key, data, detail FROM changes "
            "ORDER BY detected DESC, id DESC"
        ).fetchall()
    hosts = set(r[0] for r in base) | set(r[0] for r in chg)
    if table_exists(conn, "host_runs"):
        hosts |= set(h for (h,) in conn.execute("SELECT host FROM host_runs"))
    conn.close()

    host_list = sorted(hosts, key=str.lower)
    idx = {h: i for i, h in enumerate(host_list)}
    totals = [0] * len(host_list)

    # How many hosts carry each file hash, and when each entry first appeared.
    hash_hosts = {}
    parsed_base = []
    for host, key, data in base:
        rec = json.loads(data) if data else {}
        parsed_base.append((host, key, rec))
        totals[idx[host]] += 1
        sha = sha_of(rec)
        if sha:
            hash_hosts.setdefault(sha, set()).add(host)
    first_seen = {}
    for host, detected, ctype, key, data, detail in chg:
        if ctype == "ADDED":
            k = (host, key)
            if k not in first_seen or detected < first_seen[k]:
                first_seen[k] = detected

    changes = []
    for host, detected, ctype, key, data, detail in chg:
        rec = json.loads(data) if data else {}
        cls = classify(rec)
        if cls is None:
            continue
        det = json.loads(detail) if detail else {}
        sc = det.get("Signer")  # [old, new] when the signer itself changed
        changes.append([
            idx[host], detected, ctype, cls,
            rec.get("Category", "") or "",
            rec.get("Entry Location", ""), rec.get("Entry", ""),
            rec.get("Image Path", ""), rec.get("Launch String", ""),
            rec.get("Signer", "") or "", sha_of(rec),
            sc if sc else None,
            1 if is_ignored(rec, pats) else 0,
        ])

    inventory = []
    for host, key, rec in parsed_base:
        cls = classify(rec)
        if cls is None:
            continue
        sha = sha_of(rec)
        inventory.append([
            idx[host], cls,
            rec.get("Category", "") or "",
            rec.get("Entry Location", ""), rec.get("Entry", ""),
            rec.get("Image Path", ""), rec.get("Launch String", ""),
            rec.get("Signer", "") or "", sha,
            len(hash_hosts.get(sha, ())) if sha else 0,
            first_seen.get((host, key), ""),
            1 if is_ignored(rec, pats) else 0,
        ])
    inventory.sort(key=lambda r: (host_list[r[0]].lower(), r[1], r[5].lower(), r[4].lower()))

    return {
        "generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "historyDays": HISTORY_DAYS,
        "hosts": host_list,
        "totals": totals,
        "patterns": len(pats),
        "changes": changes,
        "inventory": inventory,
    }


def render(data):
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    payload = payload.replace("<", "\\u003c")  # never let data close the <script>
    return PAGE.replace("__DATA__", payload)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--db", default="autoruns.sqlite", help="SQLite database (default autoruns.sqlite)")
    ap.add_argument("--out", default="reports/unsigned.html", help="output file (default reports/unsigned.html)")
    ap.add_argument("--ignore", default=None,
                    help="ignore-pattern file (default: unsigned_ignore.txt next to the database)")
    args = ap.parse_args()

    if not Path(args.db).exists():
        print("database not found: %s" % args.db, file=sys.stderr)
        return 1
    ignore = args.ignore or str(Path(args.db).resolve().parent / "unsigned_ignore.txt")
    data = load_data(args.db, ignore)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(data), encoding="utf-8")
    print("wrote %s (%d unsigned changes, %d unsigned current entries, %d ignore patterns)" % (
        out, len(data["changes"]), len(data["inventory"]), data["patterns"]))
    return 0


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>Autoruns unsigned entries</title>
<style>
:root{
  --page:#f9f9f7; --surface:#fcfcfb; --ink:#0b0b0b; --ink2:#52514e; --muted:#898781;
  --grid:#e1e0d9; --axis:#c3c2b7; --ring:rgba(11,11,11,.10); --wash:rgba(11,11,11,.05);
  --warn:#b4540a; --bad:#b42318; --link:#1a5fb4;
}
@media (prefers-color-scheme: dark){
  :root{
    --page:#0d0d0d; --surface:#1a1a19; --ink:#f0efec; --ink2:#c3c2b7; --muted:#898781;
    --grid:#2c2c2a; --axis:#383835; --ring:rgba(255,255,255,.10); --wash:rgba(255,255,255,.07);
    --warn:#e8a05a; --bad:#f08a80; --link:#7db3f5;
  }
}
*{box-sizing:border-box}
body{margin:0;background:var(--page);color:var(--ink);
  font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
.wrap{max-width:1280px;margin:0 auto;padding:20px 16px 48px}
header{display:flex;flex-wrap:wrap;align-items:baseline;gap:4px 16px;margin-bottom:12px}
h1{font-size:22px;margin:0;font-weight:650}
h2{font-size:15px;margin:0 0 8px;font-weight:650}
.sub{color:var(--ink2);font-size:13px}
nav{margin-left:auto;font-size:13px}
nav a{color:var(--ink2);margin-left:12px}
nav a:hover{color:var(--ink)}
.card{background:var(--surface);border:1px solid var(--ring);border-radius:8px;padding:12px 14px;margin-bottom:12px}
.controls{display:flex;flex-wrap:wrap;gap:10px 18px;align-items:center}
.ctl{display:flex;align-items:center;gap:8px;font-size:13px;color:var(--ink2)}
.seg-group{display:inline-flex;border:1px solid var(--axis);border-radius:6px;overflow:hidden}
.seg-group button{font:inherit;font-size:13px;background:transparent;color:var(--ink2);border:0;
  padding:5px 12px;cursor:pointer}
.seg-group button+button{border-left:1px solid var(--axis)}
.seg-group button:hover{background:var(--wash)}
.seg-group button[aria-pressed="true"]{background:var(--ink);color:var(--surface)}
select,input[type=search]{font:inherit;font-size:13px;padding:5px 8px;background:var(--surface);
  color:var(--ink);border:1px solid var(--axis);border-radius:6px}
input[type=search]{min-width:200px}
.chip{font:inherit;font-size:12.5px;background:transparent;color:var(--ink2);
  border:1px solid var(--axis);border-radius:14px;padding:3px 11px;cursor:pointer}
.chip:hover{background:var(--wash)}
.chip[aria-pressed="true"]{border-color:var(--ink);color:var(--ink);font-weight:600}
.tablewrap{overflow-x:auto}
table{border-collapse:collapse;width:100%;margin:4px 0}
th,td{border:1px solid var(--grid);padding:6px 8px;text-align:left;vertical-align:top;
  font-size:13px;word-break:break-word}
th{background:var(--wash);font-weight:600;white-space:nowrap}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
tr.pick{cursor:pointer}
tr.pick:hover td{background:var(--wash)}
tr.pick.on td{background:var(--wash);font-weight:600}
.tag{display:inline-block;font-size:11.5px;font-weight:600;border:1px solid var(--muted);
  border-radius:4px;padding:0 6px;white-space:nowrap;color:var(--ink2)}
.tag.invalid{border-color:var(--bad);color:var(--bad)}
.tag.none{border-color:var(--warn);color:var(--warn)}
.tag.sc{border-color:var(--bad);color:var(--bad)}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12px}
.zero{color:var(--muted)}
.note{color:var(--ink2);font-size:12.5px;margin:8px 2px 0}
.empty{padding:22px 4px;color:var(--ink2)}
.tbtn{font:inherit;font-size:13px;background:transparent;color:var(--ink2);border:0;
  text-decoration:underline;cursor:pointer;padding:0}
.tbtn:hover{color:var(--ink)}
a{color:var(--link)}
.sr{position:absolute;left:-9999px}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>Unsigned entries</h1>
    <span class="sub" id="gen"></span>
    <nav><a href="chart.html">Chart</a><a href="history.html">History</a><a href="delta.html">Last run</a></nav>
  </header>

  <div class="card">
    <h2>Overview <span class="sub">(click a host to filter)</span></h2>
    <div class="tablewrap" id="overview"></div>
  </div>

  <div class="card controls" id="controls"></div>

  <div class="card" id="results" aria-live="polite"></div>
</div>

<script id="data" type="application/json">__DATA__</script>
<script>
(function(){
var D = JSON.parse(document.getElementById('data').textContent);
var CLASSES = [
  {key:'none',    label:'No signature',      hint:'Signer is empty and the entry points at a file'},
  {key:'invalid', label:'Invalid signature', hint:'Signature present but not verified'},
  {key:'nofile',  label:'No file',           hint:'No Image Path, or Autoruns reports the file as not found'}
];
var CLS = {}; CLASSES.forEach(function(c){ CLS[c.key] = c; });
var TYPES = ['ADDED','MODIFIED','REMOVED'];
var CAP = 300;

var S = {
  view: 'changes', host: -1, day: '', q: '', range: 30, showIgnored: false, all: false,
  cls: {none:true, invalid:true, nofile:false},
  types: {ADDED:true, MODIFIED:true, REMOVED:false}
};

function el(tag, attrs, kids){
  var e = document.createElement(tag);
  if (attrs) for (var k in attrs){
    if (k === 'text') e.textContent = attrs[k];
    else if (k === 'cls') e.className = attrs[k];
    else if (k.slice(0,2) === 'on') e.addEventListener(k.slice(2), attrs[k]);
    else e.setAttribute(k, attrs[k]);
  }
  (kids || []).forEach(function(c){ if (c != null) e.appendChild(typeof c === 'string' ? document.createTextNode(c) : c); });
  return e;
}
function pad(n){ return n < 10 ? '0' + n : '' + n; }
function ldate(s){ var d = new Date(s); return isNaN(d) ? s : d.getFullYear() + '-' + pad(d.getMonth()+1) + '-' + pad(d.getDate()); }
function ltime(s){ var d = new Date(s); return isNaN(d) ? s : ldate(s) + ' ' + pad(d.getHours()) + ':' + pad(d.getMinutes()); }
function clear(n){ while (n.firstChild) n.removeChild(n.firstChild); }

var cutoff = function(){ return new Date(D.generated).getTime() - S.range * 86400000; };

document.getElementById('gen').textContent = 'Built ' + ltime(D.generated) +
  (D.patterns ? ' · ' + D.patterns + ' ignore pattern' + (D.patterns === 1 ? '' : 's') : '');

function passBase(r, ci, ig){
  if (S.host >= 0 && r[0] !== S.host) return false;
  if (!S.cls[r[ci]]) return false;
  if (r[ig] && !S.showIgnored) return false;
  return true;
}
function textOf(r, from, to){ return r.slice(from, to).join(' ').toLowerCase(); }

function changeRows(skipDay){
  var cut = cutoff();
  return D.changes.filter(function(r){
    if (!passBase(r, 3, 12)) return false;
    if (!S.types[r[2]]) return false;
    if (new Date(r[1]).getTime() < cut) return false;
    if (!skipDay && S.day && ldate(r[1]) !== S.day) return false;
    if (S.q && textOf(r, 4, 11).indexOf(S.q) < 0) return false;
    return true;
  });
}
function invRows(){
  return D.inventory.filter(function(r){
    if (!passBase(r, 1, 11)) return false;
    if (S.q && textOf(r, 2, 8).indexOf(S.q) < 0) return false;
    return true;
  });
}

function renderOverview(){
  var n = D.hosts.length, inv = [], chg = [];
  for (var i = 0; i < n; i++){ inv.push({none:0,invalid:0,nofile:0}); chg.push(0); }
  var cut = cutoff();
  D.inventory.forEach(function(r){ if (!r[11] || S.showIgnored) inv[r[0]][r[1]]++; });
  D.changes.forEach(function(r){
    if ((!r[12] || S.showIgnored) && S.cls[r[3]] && S.types[r[2]] && new Date(r[1]).getTime() >= cut) chg[r[0]]++;
  });
  var t = el('table');
  t.appendChild(el('tr', null, [
    el('th', {text:'Host'}), el('th', {cls:'num', text:'Entries'}),
    el('th', {cls:'num', text:'Now: no signature'}), el('th', {cls:'num', text:'Now: invalid'}),
    el('th', {cls:'num', text:'Now: no file'}),
    el('th', {cls:'num', text:'Matching changes (last ' + S.range + 'd)', title:'Follows the Show and Type filters below'})
  ]));
  function pick(i){ S.host = (S.host === i) ? -1 : i; S.day = ''; S.all = false; draw(); }
  function numCell(v){ return el('td', {cls:'num' + (v ? '' : ' zero'), text: String(v)}); }
  var all = el('tr', {cls:'pick' + (S.host < 0 ? ' on' : ''), tabindex:'0', onclick:function(){ S.host = -1; S.day=''; draw(); }}, [
    el('td', {text:'All hosts'}),
    el('td', {cls:'num', text: String(D.totals.reduce(function(a,b){return a+b;}, 0))}),
    numCell(inv.reduce(function(a,x){return a+x.none;},0)), numCell(inv.reduce(function(a,x){return a+x.invalid;},0)),
    numCell(inv.reduce(function(a,x){return a+x.nofile;},0)), numCell(chg.reduce(function(a,b){return a+b;},0))
  ]);
  t.appendChild(all);
  D.hosts.forEach(function(h, i){
    var tr = el('tr', {cls:'pick' + (S.host === i ? ' on' : ''), tabindex:'0', onclick:function(){ pick(i); },
      onkeydown:function(e){ if (e.key === 'Enter' || e.key === ' '){ e.preventDefault(); pick(i); } }}, [
      el('td', {text:h}), el('td', {cls:'num', text:String(D.totals[i])}),
      numCell(inv[i].none), numCell(inv[i].invalid), numCell(inv[i].nofile), numCell(chg[i])
    ]);
    t.appendChild(tr);
  });
  var w = document.getElementById('overview'); clear(w); w.appendChild(t);
}

function group(label, buttons){
  var g = el('div', {cls:'seg-group', role:'group', 'aria-label':label});
  buttons.forEach(function(b){ g.appendChild(b); });
  return g;
}
function segBtn(text, on, fn){ return el('button', {type:'button', 'aria-pressed': on ? 'true' : 'false', text:text, onclick:fn}); }
function chip(text, on, fn, title){ return el('button', {type:'button', cls:'chip', 'aria-pressed': on ? 'true' : 'false', text:text, title:title || '', onclick:fn}); }

function renderControls(){
  var c = document.getElementById('controls'); clear(c);
  c.appendChild(group('View', [
    segBtn('Changes', S.view === 'changes', function(){ S.view = 'changes'; S.all = false; draw(); }),
    segBtn('Current inventory', S.view === 'inv', function(){ S.view = 'inv'; S.day = ''; S.all = false; draw(); })
  ]));
  var kinds = el('div', {cls:'ctl'}, [el('span', {text:'Show'})]);
  CLASSES.forEach(function(k){
    kinds.appendChild(chip(k.label, S.cls[k.key], function(){ S.cls[k.key] = !S.cls[k.key]; S.all = false; draw(); }, k.hint));
  });
  c.appendChild(kinds);
  if (S.view === 'changes'){
    var ty = el('div', {cls:'ctl'}, [el('span', {text:'Type'})]);
    TYPES.forEach(function(t){
      ty.appendChild(chip(t.charAt(0) + t.slice(1).toLowerCase(), S.types[t], function(){ S.types[t] = !S.types[t]; S.all = false; draw(); }));
    });
    c.appendChild(ty);
    c.appendChild(el('div', {cls:'ctl'}, [el('span', {text:'Range'}),
      group('Range', [7, 14, 30].map(function(n){
        return segBtn(n + ' days', S.range === n, function(){ S.range = n; S.day = ''; S.all = false; draw(); });
      }))
    ]));
    // Day picker with counts, built from the rows that pass every filter but the day.
    var counts = {}, order = [];
    changeRows(true).forEach(function(r){ var d = ldate(r[1]); if (!(d in counts)){ counts[d] = 0; order.push(d); } counts[d]++; });
    order.sort().reverse();
    var sel = el('select', {id:'daysel', 'aria-label':'Day', onchange:function(){ S.day = this.value; S.all = false; draw(); }});
    sel.appendChild(el('option', {value:'', text:'All days'}));
    order.forEach(function(d){ var o = el('option', {value:d, text:d + '  (' + counts[d] + ')'}); if (S.day === d) o.selected = true; sel.appendChild(o); });
    c.appendChild(el('label', {cls:'ctl'}, [el('span', {text:'Day'}), sel]));
  }
  var q = el('input', {type:'search', placeholder:'Filter rows…', 'aria-label':'Filter rows', value:S.q});
  q.addEventListener('input', function(){ S.q = this.value.toLowerCase(); S.all = false; var p = this.selectionStart; drawResults(); });
  c.appendChild(q);
  var ig = el('label', {cls:'ctl'}, [el('input', {type:'checkbox', id:'ig'}), el('span', {text:'Show ignored'})]);
  ig.firstChild.checked = S.showIgnored;
  ig.firstChild.addEventListener('change', function(){ S.showIgnored = this.checked; S.all = false; draw(); });
  c.appendChild(ig);
  if (S.host >= 0 || S.day || S.q){
    c.appendChild(el('button', {type:'button', cls:'tbtn', text:'Clear filters', onclick:function(){ S.host = -1; S.day = ''; S.q = ''; S.all = false; draw(); }}));
  }
}

function tag(r, cls){
  return el('span', {cls:'tag ' + cls, text: CLS[cls].label, title: CLS[cls].hint});
}
function signerCell(signer, cls, sc){
  var td = el('td');
  td.appendChild(tag(null, cls));
  if (signer) td.appendChild(el('div', {text:signer}));
  if (sc){
    td.appendChild(el('div', {cls:'tag sc', text:'Signer changed', title:'Was: ' + (sc[0] || '(empty)') + ' → now: ' + (sc[1] || '(empty)')}));
    td.appendChild(el('div', {cls:'zero', text:'was: ' + (sc[0] || '(empty)')}));
  }
  return td;
}
function vtCell(sha, hostsCount){
  var td = el('td');
  if (!sha){ td.appendChild(el('span', {cls:'zero', text:'no hash'})); return td; }
  td.appendChild(el('div', {cls:'mono', text:sha.slice(0, 16) + '…', title:sha}));
  td.appendChild(el('a', {href:'https://www.virustotal.com/gui/file/' + sha, target:'_blank', rel:'noopener noreferrer', text:'VirusTotal'}));
  if (hostsCount != null) td.appendChild(el('div', {cls:'zero', text: hostsCount + (hostsCount === 1 ? ' host' : ' hosts') + ' with this file'}));
  return td;
}

function drawResults(){
  var box = document.getElementById('results'); clear(box);
  var rows = S.view === 'changes' ? changeRows(false) : invRows();
  var title = S.view === 'changes' ? 'Unsigned changes' : 'Currently unsigned entries';
  var bits = [];
  if (S.host >= 0) bits.push(D.hosts[S.host]);
  if (S.day) bits.push(S.day);
  box.appendChild(el('h2', {text: title + (bits.length ? ' · ' + bits.join(' · ') : '')}));
  box.appendChild(el('div', {cls:'sub', text: rows.length + ' row' + (rows.length === 1 ? '' : 's')}));
  if (!rows.length){
    box.appendChild(el('div', {cls:'empty', text: 'Nothing matches the current filters.' +
      (S.view === 'changes' && !S.cls.nofile ? ' "No file" entries are hidden by default.' : '')}));
    return;
  }
  var shown = (S.all || rows.length <= CAP) ? rows : rows.slice(0, CAP);
  var t = el('table');
  var head = S.view === 'changes'
    ? ['When', 'Host', 'Change', 'Category', 'Entry', 'Image Path / Launch String', 'Signer', 'SHA-256']
    : ['Host', 'Category', 'Entry', 'Image Path / Launch String', 'Signer', 'SHA-256', 'First seen'];
  t.appendChild(el('tr', null, head.map(function(h){ return el('th', {text:h}); })));
  shown.forEach(function(r){
    var tr = el('tr');
    function pathCell(loc, entry, image, launch){
      return [
        el('td', null, [el('div', {text:entry}), el('div', {cls:'zero', text:loc})]),
        el('td', null, [el('div', {text:image || '(none)'}), launch && launch !== image ? el('div', {cls:'zero', text:launch}) : null])
      ];
    }
    if (S.view === 'changes'){
      tr.appendChild(el('td', {text:ltime(r[1]), cls:'mono'}));
      tr.appendChild(el('td', {text:D.hosts[r[0]]}));
      tr.appendChild(el('td', {text:r[2].charAt(0) + r[2].slice(1).toLowerCase()}));
      tr.appendChild(el('td', {text:r[4]}));
      pathCell(r[5], r[6], r[7], r[8]).forEach(function(x){ tr.appendChild(x); });
      tr.appendChild(signerCell(r[9], r[3], r[11]));
      tr.appendChild(vtCell(r[10], null));
    } else {
      tr.appendChild(el('td', {text:D.hosts[r[0]]}));
      tr.appendChild(el('td', {text:r[2]}));
      pathCell(r[3], r[4], r[5], r[6]).forEach(function(x){ tr.appendChild(x); });
      tr.appendChild(signerCell(r[7], r[1], null));
      tr.appendChild(vtCell(r[8], r[9]));
      tr.appendChild(el('td', {cls:'mono', text: r[10] ? ltime(r[10]) : 'before history'}));
    }
    t.appendChild(tr);
  });
  var w = el('div', {cls:'tablewrap'}, [t]); box.appendChild(w);
  if (shown.length < rows.length){
    box.appendChild(el('div', {cls:'note'}, [
      'Showing the first ' + shown.length + ' of ' + rows.length + '. ',
      el('button', {type:'button', cls:'tbtn', text:'Show all', onclick:function(){ S.all = true; drawResults(); }})
    ]));
  }
  if (S.view === 'inv'){
    box.appendChild(el('div', {cls:'note', text:'"First seen" is the first ADDED change in the stored history (' + D.historyDays +
      ' days). Entries older than that show "before history". A file hash seen on several hosts is usually a vendor tool; one seen on a single host deserves a closer look.'}));
  }
}

function draw(){ renderOverview(); renderControls(); drawResults(); }
draw();
})();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    sys.exit(main())
