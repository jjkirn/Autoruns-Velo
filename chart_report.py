#!/usr/bin/env python3
"""
chart_report.py

Build chart.html, a self-contained page that shows the Autoruns change history
as one stacked-bar time series per host. Click a segment to list the matching
changes below the charts; click another segment and the list just updates.

It only READS the SQLite database written by autoruns_velociraptor.py. It does
not talk to Velociraptor and needs nothing beyond the Python standard library.

Usage:
  python3 chart_report.py --db autoruns.sqlite --out reports/chart.html

Optional: if the database has a `runs` table (see runs_table.patch), the charts
also mark days a host ran with no changes, and days it did not run at all
(offline, error). Without that table the charts still work, they just cannot
tell "quiet day" from "machine was off".
"""

import argparse
import datetime as dt
import json
import sqlite3
import sys
from pathlib import Path

# Categories get a fixed colour each, in this order (slots 1-7 of the palette),
# so "Services" is the same colour in every report. Anything else is grouped
# into a grey "Other" bar segment. Edit this list to taste: use the exact
# Category text from Autoruns, at most 7 entries.
CATEGORY_ORDER = [
    "Logon",
    "Scheduled Tasks",
    "Services",
    "Drivers",
    "Explorer",
    "Codecs",
    "WMI",
]

HISTORY_DAYS = 30


def table_exists(conn, name):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def load_data(db_path):
    conn = sqlite3.connect("file:%s?mode=ro" % Path(db_path).resolve().as_posix(), uri=True)
    hosts = set()
    last_success = {}
    if table_exists(conn, "host_runs"):
        for host, last in conn.execute("SELECT host, last_success FROM host_runs"):
            hosts.add(host)
            last_success[host] = last
    if table_exists(conn, "baseline"):
        hosts.update(h for (h,) in conn.execute("SELECT DISTINCT host FROM baseline"))
    change_rows = []
    if table_exists(conn, "changes"):
        change_rows = conn.execute(
            "SELECT host, detected, change_type, data, detail FROM changes "
            "ORDER BY detected, id"
        ).fetchall()
        hosts.update(r[0] for r in change_rows)
    run_rows = []
    has_runs = table_exists(conn, "runs")
    if has_runs:
        run_rows = conn.execute(
            "SELECT host, run_at, status, entries FROM runs ORDER BY run_at"
        ).fetchall()
        hosts.update(r[0] for r in run_rows)
    conn.close()

    host_list = sorted(hosts, key=str.lower)
    index = {h: i for i, h in enumerate(host_list)}

    changes = []
    for host, detected, ctype, data, detail in change_rows:
        rec = json.loads(data) if data else {}
        det = json.loads(detail) if detail else {}
        changes.append([
            index[host],
            detected,
            ctype,
            rec.get("Category", "") or "",
            rec.get("Entry Location", ""),
            rec.get("Entry", ""),
            rec.get("Image Path", ""),
            rec.get("Launch String", ""),
            rec.get("Signer", ""),
            [[f, o, n] for f, (o, n) in det.items()],
        ])
    runs = [[index[h], at, status, entries] for h, at, status, entries in run_rows]

    return {
        "generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "historyDays": HISTORY_DAYS,
        "hosts": host_list,
        "lastSuccess": [last_success.get(h, "") for h in host_list],
        "categoryOrder": CATEGORY_ORDER[:7],
        "hasRuns": has_runs,
        "changes": changes,
        "runs": runs,
    }


def render(data):
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    # Keep the JSON from ever closing the <script> block, whatever is in the data.
    payload = payload.replace("<", "\\u003c")
    return PAGE.replace("__DATA__", payload)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--db", default="autoruns.sqlite", help="SQLite database (default autoruns.sqlite)")
    ap.add_argument("--out", default="reports/chart.html", help="output file (default reports/chart.html)")
    args = ap.parse_args()

    if not Path(args.db).exists():
        print("database not found: %s" % args.db, file=sys.stderr)
        return 1
    data = load_data(args.db)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(data), encoding="utf-8")
    print("wrote %s (%d hosts, %d changes%s)" % (
        out, len(data["hosts"]), len(data["changes"]),
        ", run history present" if data["hasRuns"] else ", no run history yet"))
    return 0


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>Autoruns change chart</title>
<style>
:root{
  --page:#f9f9f7; --surface:#fcfcfb; --ink:#0b0b0b; --ink2:#52514e; --muted:#898781;
  --grid:#e1e0d9; --axis:#c3c2b7; --ring:rgba(11,11,11,.10); --wash:rgba(11,11,11,.05);
  --s1:#2a78d6; --s2:#eb6834; --s3:#1baf7a; --s4:#eda100; --s5:#e87ba4; --s6:#008300; --s7:#6250d6;
  --other:#898781;
}
@media (prefers-color-scheme: dark){
  :root{
    --page:#0d0d0d; --surface:#1a1a19; --ink:#f0efec; --ink2:#c3c2b7; --muted:#898781;
    --grid:#2c2c2a; --axis:#383835; --ring:rgba(255,255,255,.10); --wash:rgba(255,255,255,.07);
    --s1:#3987e5; --s2:#d95926; --s3:#199e70; --s4:#c98500; --s5:#d55181; --s6:#008300; --s7:#9085e9;
  }
}
*{box-sizing:border-box}
body{margin:0;background:var(--page);color:var(--ink);
  font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:20px 16px 48px}
header{display:flex;flex-wrap:wrap;align-items:baseline;gap:4px 16px;margin-bottom:12px}
h1{font-size:22px;margin:0;font-weight:650}
.sub{color:var(--ink2);font-size:13px}
nav{margin-left:auto;font-size:13px}
nav a{color:var(--ink2);margin-left:12px}
nav a:hover{color:var(--ink)}
.card{background:var(--surface);border:1px solid var(--ring);border-radius:8px}
.controls{display:flex;flex-wrap:wrap;gap:10px 18px;align-items:center;padding:10px 14px;margin-bottom:12px}
.ctl{display:flex;align-items:center;gap:8px;font-size:13px;color:var(--ink2)}
.seg-group{display:inline-flex;border:1px solid var(--axis);border-radius:6px;overflow:hidden}
.seg-group button{font:inherit;font-size:13px;background:transparent;color:var(--ink2);border:0;
  padding:5px 11px;cursor:pointer}
.seg-group button+button{border-left:1px solid var(--axis)}
.seg-group button:hover{background:var(--wash)}
.seg-group button[aria-pressed="true"]{background:var(--ink);color:var(--surface)}
.ctl label{display:flex;align-items:center;gap:6px;cursor:pointer}
.legend{display:flex;flex-wrap:wrap;gap:6px 16px;padding:0 14px 10px;margin:-4px 0 12px;font-size:13px;color:var(--ink2)}
.legend span{display:inline-flex;align-items:center;gap:6px}
.sw{width:10px;height:10px;border-radius:2px;display:inline-block}
.dash{width:10px;height:2px;background:var(--muted);display:inline-block}
.times{font-size:15px;line-height:1;color:var(--muted)}
.host{padding:12px 14px 4px;border-top:1px solid var(--ring)}
.host:first-child{border-top:0}
.host-head{display:flex;flex-wrap:wrap;align-items:baseline;gap:2px 12px}
.host-head h2{font-size:15px;margin:0}
.host-head .meta{font-size:12.5px;color:var(--ink2)}
.stale{font-size:11.5px;font-weight:600;border:1px solid var(--muted);color:var(--ink2);
  border-radius:10px;padding:0 7px}
svg{display:block;width:100%;height:auto;overflow:visible}
svg text{font:11.5px system-ui,-apple-system,"Segoe UI",sans-serif;fill:var(--muted);
  font-variant-numeric:tabular-nums}
svg .peak{fill:var(--ink2);font-weight:600}
svg .grid{stroke:var(--grid);stroke-width:1}
svg .axis{stroke:var(--axis);stroke-width:1}
svg .wash{fill:var(--wash)}
svg .hit{fill:transparent;cursor:pointer}
svg .seg{cursor:pointer;stroke:none}
svg .seg:hover{filter:brightness(1.12)}
svg .seg:focus-visible{outline:none;stroke:var(--ink);stroke-width:2}
svg .seg.sel{stroke:var(--ink);stroke-width:2}
svg .miss{font-size:14px;fill:var(--muted)}
svg .ok{fill:var(--muted)}
.panel{padding:14px;margin-top:12px}
.panel h2{font-size:16px;margin:0 0 2px}
.panel .hint{color:var(--ink2);margin:6px 0 0}
.pmeta{color:var(--ink2);font-size:13px;margin-bottom:8px}
.chips{display:flex;flex-wrap:wrap;align-items:center;gap:6px;margin:8px 0}
.chip{font:inherit;font-size:13px;display:inline-flex;align-items:center;gap:6px;background:transparent;
  color:var(--ink2);border:1px solid var(--axis);border-radius:14px;padding:3px 11px;cursor:pointer}
.chip:hover{background:var(--wash)}
.chip[aria-pressed="true"]{border-color:var(--ink);color:var(--ink);font-weight:600}
.chips .sp{flex:1}
.chips input[type=search]{font:inherit;font-size:13px;padding:4px 9px;min-width:190px;
  background:var(--surface);color:var(--ink);border:1px solid var(--axis);border-radius:6px}
.tbtn{font:inherit;font-size:13px;background:transparent;color:var(--ink2);border:0;
  text-decoration:underline;cursor:pointer;padding:3px 4px}
.tbtn:hover{color:var(--ink)}
.tablewrap{overflow-x:auto}
table{border-collapse:collapse;width:100%;margin:4px 0}
th,td{border:1px solid var(--grid);padding:6px 8px;text-align:left;vertical-align:top;
  font-size:13px;word-break:break-word}
th{background:var(--wash);font-weight:600;white-space:nowrap}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
.chg{white-space:nowrap;font-weight:600}
.detail div{margin:0 0 2px}
.zero{color:var(--muted)}
.cellbtn{font:inherit;font-size:13px;background:transparent;color:var(--ink);border:0;
  text-decoration:underline;cursor:pointer;padding:0 2px;font-variant-numeric:tabular-nums}
.note{color:var(--ink2);font-size:12.5px;margin:10px 2px 0}
.empty{padding:28px 14px;color:var(--ink2)}
.tip{position:fixed;z-index:10;pointer-events:none;background:var(--surface);color:var(--ink);
  border:1px solid var(--axis);border-radius:6px;padding:8px 10px;font-size:12.5px;
  box-shadow:0 4px 14px rgba(0,0,0,.18);max-width:260px;display:none}
.tip .t{font-weight:600;margin-bottom:4px}
.tip .r{display:flex;align-items:center;gap:7px;color:var(--ink2);padding:1px 0}
.tip .r .k{width:12px;height:3px;border-radius:2px;flex:none}
.tip .r b{color:var(--ink);font-weight:650;margin-left:auto;padding-left:12px;font-variant-numeric:tabular-nums}
.tip .r.hov{color:var(--ink)}
.tip .run{margin-top:4px;color:var(--ink2)}
.sr{position:absolute;left:-9999px}
@media (max-width:640px){ nav{margin-left:0;width:100%} .controls{gap:8px 12px} }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>Autoruns change chart</h1>
    <div class="sub" id="gen"></div>
    <nav><a href="unsigned.html">Unsigned</a><a href="history.html">History</a><a href="delta.html">Last run</a></nav>
  </header>
  <div class="card controls" id="controls"></div>
  <div class="card" id="chartcard">
    <div class="legend" id="legend" style="padding-top:12px;margin:0"></div>
    <div id="charts"></div>
  </div>
  <div class="note" id="notes"></div>
  <section class="card panel" id="panel" aria-live="polite"></section>
</div>
<div class="tip" id="tip" role="tooltip"></div>
<script type="application/json" id="data">__DATA__</script>
<script>
(function(){
'use strict';
var D = JSON.parse(document.getElementById('data').textContent);
var SVGNS = 'http://www.w3.org/2000/svg';
var MAX_ROWS = 300;

var TYPES = [
  {key:'ADDED',    label:'Added',    css:'--s1', glyph:'+'},
  {key:'REMOVED',  label:'Removed',  css:'--s2', glyph:'−'},
  {key:'MODIFIED', label:'Modified', css:'--s3', glyph:'~'}
];
var TYPE_BY = {}; TYPES.forEach(function(t){ TYPE_BY[t.key] = t; });
var CATS = D.categoryOrder.map(function(n,i){ return {key:n,label:n,css:'--s'+(i+1)}; });
CATS.push({key:'__other',label:'Other',css:'--other'});

var state = {view:'type', range:Math.min(30, D.historyDays), same:true, table:false, sel:null, filter:''};

/* ---------- helpers ---------- */
function $(id){ return document.getElementById(id); }
function h(tag, props, kids){
  var e = document.createElement(tag);
  if(props){ for(var k in props){
    if(k==='text') e.textContent = props[k];
    else if(k==='on'){ for(var ev in props.on) e.addEventListener(ev, props.on[ev]); }
    else if(k==='class') e.className = props[k];
    else e.setAttribute(k, props[k]);
  } }
  (kids||[]).forEach(function(c){ if(c) e.appendChild(c); });
  return e;
}
function sv(tag, attrs, parent, text){
  var e = document.createElementNS(SVGNS, tag);
  for(var k in attrs) e.setAttribute(k, attrs[k]);
  if(text!=null) e.textContent = text;
  if(parent) parent.appendChild(e);
  return e;
}
function pad2(n){ return (n<10?'0':'')+n; }
function dayKey(d){ return d.getFullYear()+'-'+pad2(d.getMonth()+1)+'-'+pad2(d.getDate()); }
function dayDate(k){ var p=k.split('-').map(Number); return new Date(p[0],p[1]-1,p[2],12); }
function fmtDay(k,long){
  return dayDate(k).toLocaleDateString(undefined, long?{weekday:'short',month:'short',day:'numeric'}:{month:'short',day:'numeric'});
}
function fmtTime(t){
  return t.toLocaleString(undefined,{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'});
}
function sw(css){ var s=h('span',{class:'sw'}); s.style.background='var('+css+')'; return s; }

/* ---------- data prep ---------- */
var changes = D.changes.map(function(r,i){
  var t = new Date(r[1]);
  var cat = r[3] || '(none)';
  return {id:i, host:D.hosts[r[0]], t:t, iso:r[1], day:dayKey(t), type:r[2], cat:cat,
          catKey:D.categoryOrder.indexOf(cat)>=0?cat:'__other',
          loc:r[4], entry:r[5], image:r[6], launch:r[7], signer:r[8], detail:r[9]};
});
var byHostDay = {};
changes.forEach(function(c){
  var m = byHostDay[c.host] || (byHostDay[c.host] = {});
  (m[c.day] || (m[c.day] = [])).push(c);
});
var runsBy = {}, firstRunDay = {};
D.runs.forEach(function(r){
  var host = D.hosts[r[0]], day = dayKey(new Date(r[1]));
  var m = runsBy[host] || (runsBy[host] = {});
  (m[day] || (m[day] = [])).push(r[2]);
  if(!firstRunDay[host] || day < firstRunDay[host]) firstRunDay[host] = day;
});
function runInfo(host, day){
  if(!D.hasRuns) return null;
  var first = firstRunDay[host];
  if(!first || day < first) return null;
  var st = (runsBy[host]||{})[day] || [];
  if(st.indexOf('ok')>=0) return {ran:true, text:'Ran, results compared'};
  if(st.indexOf('baseline')>=0) return {ran:true, text:'Baseline established'};
  if(st.length){
    var u = st.filter(function(s,i){ return st.indexOf(s)===i; });
    return {ran:false, text:'No successful run ('+u.join(', ')+')'};
  }
  return {ran:false, text:'No run recorded'};
}
function seriesList(){ return state.view==='type' ? TYPES : CATS; }
function keyOf(c){ return state.view==='type' ? c.type : c.catKey; }
function daysList(){
  var g = new Date(D.generated), out = [];
  for(var k=state.range-1;k>=0;k--) out.push(dayKey(new Date(g.getFullYear(),g.getMonth(),g.getDate()-k,12)));
  return out;
}
function cell(host, day){
  var items = (byHostDay[host]||{})[day] || [];
  var counts = {};
  items.forEach(function(c){ var k=keyOf(c); counts[k]=(counts[k]||0)+1; });
  return {items:items, counts:counts, total:items.length};
}
function activeSeries(days){
  var present = {};
  D.hosts.forEach(function(host){ days.forEach(function(d){
    var c = cell(host,d); for(var k in c.counts) present[k]=true;
  }); });
  var list = seriesList().filter(function(s){ return present[s.key]; });
  return list.length ? list : seriesList().slice(0,3);
}
function niceScale(max){
  if(max<=0) return {max:4, step:1};
  var raw = max/4, mag = Math.pow(10, Math.floor(Math.log10(raw))), step = 1;
  [1,2,5,10].some(function(m){ step = m*mag; return raw <= step; });
  if(step < 1) step = 1;
  return {max:Math.ceil(max/step)*step, step:step};
}
function compact(n){ return n>=1000 ? (Math.round(n/100)/10)+'K' : String(n); }

/* ---------- controls ---------- */
function segmented(label, options, current, onPick){
  var g = h('div',{class:'seg-group',role:'group','aria-label':label});
  options.forEach(function(o){
    g.appendChild(h('button',{type:'button','aria-pressed':String(o.value===current),text:o.label,
      on:{click:function(){ onPick(o.value); }}}));
  });
  return g;
}
function renderControls(){
  var c = $('controls'); c.replaceChildren();
  c.appendChild(h('div',{class:'ctl'},[h('span',{text:'Stack by'}),
    segmented('Stack by',[{value:'type',label:'Change type'},{value:'category',label:'Category'}],state.view,function(v){
      state.view = v; if(state.sel) state.sel.key = null; renderAll();
    })]));
  var ranges = [7,14,30].filter(function(n){ return n <= D.historyDays; });
  if(ranges.indexOf(D.historyDays) < 0) ranges.push(D.historyDays);
  c.appendChild(h('div',{class:'ctl'},[h('span',{text:'Range'}),
    segmented('Range',ranges.map(function(n){ return {value:n,label:n+' days'}; }),state.range,function(v){
      state.range = v; renderAll();
    })]));
  var same = h('input',{type:'checkbox',id:'same'}); same.checked = state.same;
  same.addEventListener('change', function(){ state.same = same.checked; renderCharts(); });
  var tbl = h('input',{type:'checkbox',id:'tbl'}); tbl.checked = state.table;
  tbl.addEventListener('change', function(){ state.table = tbl.checked; renderCharts(); });
  c.appendChild(h('div',{class:'ctl'},[h('label',{},[same,h('span',{text:'Same scale for all hosts'})])]));
  c.appendChild(h('div',{class:'ctl'},[h('label',{},[tbl,h('span',{text:'Table view'})])]));
}
function renderLegend(days){
  var l = $('legend'); l.replaceChildren();
  l.appendChild(h('span',{class:'sr',text:'Legend'}));
  activeSeries(days).forEach(function(s){
    l.appendChild(h('span',{},[sw(s.css),h('span',{text:s.label})]));
  });
  if(D.hasRuns){
    l.appendChild(h('span',{},[h('span',{class:'dash'}),h('span',{text:'Ran, no changes'})]));
    l.appendChild(h('span',{},[h('span',{class:'times',text:'×'}),h('span',{text:'Not run (offline or error)'})]));
  }
}

/* ---------- tooltip ---------- */
var tip = $('tip');
function showTip(ev, host, day, hoverKey, anchor){
  var c = cell(host, day), ri = runInfo(host, day);
  tip.replaceChildren();
  tip.appendChild(h('div',{class:'t',text:host+' · '+fmtDay(day,true)}));
  seriesList().forEach(function(s){
    var n = c.counts[s.key]; if(!n) return;
    var k = h('span',{class:'k'}); k.style.background = 'var('+s.css+')';
    tip.appendChild(h('div',{class:'r'+(s.key===hoverKey?' hov':'')},[k,h('span',{text:s.label}),h('b',{text:String(n)})]));
  });
  if(!c.total) tip.appendChild(h('div',{class:'r',text:'No changes'}));
  else if(Object.keys(c.counts).length>1)
    tip.appendChild(h('div',{class:'r'},[h('span',{text:'Total'}),h('b',{text:String(c.total)})]));
  if(ri) tip.appendChild(h('div',{class:'run',text:ri.text}));
  tip.style.display = 'block';
  var x, y;
  if(ev && ev.clientX!=null && !anchor){ x = ev.clientX+14; y = ev.clientY+14; }
  else { var r = anchor.getBoundingClientRect(); x = r.right+8; y = r.top; }
  var w = tip.offsetWidth, hh = tip.offsetHeight;
  if(x+w > innerWidth-8) x = Math.max(8, x-w-28);
  if(y+hh > innerHeight-8) y = Math.max(8, innerHeight-hh-8);
  tip.style.left = x+'px'; tip.style.top = y+'px';
}
function hideTip(){ tip.style.display = 'none'; }

/* ---------- charts ---------- */
var PLOT_H = 120, M = {l:40, r:8, t:20, b:24};

function barPath(x, y, w, hgt, round){
  if(!round) return 'M'+x+' '+y+'h'+w+'v'+hgt+'h'+(-w)+'Z';
  var r = Math.min(4, w/2, hgt);
  return 'M'+x+' '+(y+hgt)+'V'+(y+r)+'A'+r+' '+r+' 0 0 1 '+(x+r)+' '+y+'H'+(x+w-r)+
         'A'+r+' '+r+' 0 0 1 '+(x+w)+' '+(y+r)+'V'+(y+hgt)+'Z';
}
function isSel(host, day){ return state.sel && state.sel.host===host && state.sel.day===day; }

function drawHost(host, idx, days, scaleMax){
  var series = seriesList();
  var info = days.map(function(d){ return cell(host,d); });
  var hostMax = Math.max.apply(null, info.map(function(c){ return c.total; }).concat([0]));
  var sc = niceScale(state.same ? scaleMax : hostMax);
  var total = info.reduce(function(a,c){ return a+c.total; }, 0);
  var section = h('div',{class:'host'});
  var head = h('div',{class:'host-head'});
  head.appendChild(h('h2',{text:host}));
  var meta = total+' change'+(total===1?'':'s')+' in the last '+state.range+' days';
  var last = D.lastSuccess[idx];
  if(last) meta += ' · last successful run '+fmtTime(new Date(last));
  head.appendChild(h('span',{class:'meta',text:meta}));
  if(last && (new Date(D.generated) - new Date(last)) > 48*3600*1000) head.appendChild(h('span',{class:'stale',text:'stale'}));
  section.appendChild(head);

  var holder = h('div'); section.appendChild(holder);
  var W = Math.max(280, holder.clientWidth || $('charts').clientWidth - 28 || 600);
  var plotW = W - M.l - M.r, n = days.length, slot = plotW/n;
  var bw = Math.max(3, Math.min(24, slot-4));
  var Ht = M.t + PLOT_H + M.b, base = M.t + PLOT_H;
  var svg = sv('svg',{viewBox:'0 0 '+W+' '+Ht,width:W,height:Ht,role:'group',
    'aria-label':host+' autoruns changes per day'}, holder);
  function yOf(v){ return PLOT_H * v / sc.max; }

  for(var v=0; v<=sc.max; v+=sc.step){
    var gy = base - yOf(v);
    if(v>0) sv('line',{class:'grid',x1:M.l,x2:W-M.r,y1:gy,y2:gy},svg);
    sv('text',{x:M.l-8,y:gy+4,'text-anchor':'end'},svg,compact(v));
  }
  var peakIdx = -1, peakVal = 0;
  info.forEach(function(c,i){ if(c.total>peakVal){ peakVal=c.total; peakIdx=i; } });
  var every = Math.max(1, Math.ceil(64/slot));

  days.forEach(function(day,i){
    var c = info[i], cx = M.l + i*slot, bx = cx + (slot-bw)/2;
    var g = sv('g',{class:'col','data-host':host,'data-day':day},svg);
    var wash = sv('rect',{class:'wash',x:cx,y:M.t-4,width:slot,height:PLOT_H+4,rx:3},g);
    wash.style.display = isSel(host,day) ? '' : 'none';
    sv('rect',{class:'hit',x:cx,y:M.t-4,width:slot,height:PLOT_H+4},g);

    var present = series.filter(function(s){ return c.counts[s.key]; });
    var cursor = 0;
    /* Segment heights: proportional to the count, but the 2px gaps and the 4px
       minimum must not push the bar past its true total, so the larger
       segments give up the space the gaps and tiny segments take. */
    var gaps = Math.max(0, present.length-1)*2, avail = Math.max(0, yOf(c.total)-gaps);
    var raw = present.map(function(s){ return c.total ? avail*c.counts[s.key]/c.total : 0; });
    var tiny = raw.filter(function(v){ return v<4; }).length;
    var bigSum = raw.reduce(function(a,v){ return v>=4 ? a+v : a; }, 0);
    var bigBudget = Math.max(0, avail - 4*tiny);
    var hs = raw.map(function(v){
      if(v<4) return 4;
      return bigSum>bigBudget && bigSum>0 ? Math.max(4, v*bigBudget/bigSum) : v;
    });
    present.forEach(function(s,k){
      var hgt = hs[k];
      if(k>0) cursor += 2;
      var top = base - cursor - hgt;
      var seg = sv('path',{class:'seg','data-key':s.key,d:barPath(bx, top, bw, hgt, k===present.length-1),
        fill:'var('+s.css+')',tabindex:0,role:'button',
        'aria-label':host+', '+fmtDay(day)+', '+s.label+': '+c.counts[s.key]+' change'+(c.counts[s.key]===1?'':'s')},g);
      if(state.sel && isSel(host,day) && state.sel.key===s.key) seg.setAttribute('class','seg sel');
      cursor += hgt;
    });
    if(!c.total){
      var ri = runInfo(host, day);
      if(ri && ri.ran) sv('rect',{class:'ok',x:bx,y:base-4,width:bw,height:2,'pointer-events':'none'},g);
      else if(ri) sv('text',{class:'miss',x:cx+slot/2,y:base-4,'text-anchor':'middle','pointer-events':'none'},g,'×');
    }
    if(i===peakIdx && peakVal>0)
      sv('text',{class:'peak',x:cx+slot/2,y:Math.max(12, base-cursor-6),'text-anchor':'middle','pointer-events':'none'},g,String(peakVal));
    if((n-1-i)%every===0)
      sv('text',{x:cx+slot/2,y:base+16,'text-anchor':'middle'},svg,fmtDay(day));
  });
  sv('line',{class:'axis',x1:M.l,x2:W-M.r,y1:base,y2:base},svg);

  svg.addEventListener('pointermove', function(ev){
    var g = ev.target.closest && ev.target.closest('.col'); if(!g){ hideTip(); return; }
    showTip(ev, g.getAttribute('data-host'), g.getAttribute('data-day'), ev.target.getAttribute('data-key'));
  });
  svg.addEventListener('pointerleave', hideTip);
  svg.addEventListener('click', function(ev){
    var g = ev.target.closest && ev.target.closest('.col'); if(!g) return;
    select(g.getAttribute('data-host'), g.getAttribute('data-day'), ev.target.getAttribute('data-key'));
  });
  svg.addEventListener('keydown', function(ev){
    if(ev.key!=='Enter' && ev.key!==' ') return;
    var t = ev.target; if(!t.classList || !t.classList.contains('seg')) return;
    ev.preventDefault();
    var g = t.closest('.col');
    select(g.getAttribute('data-host'), g.getAttribute('data-day'), t.getAttribute('data-key'));
  });
  svg.addEventListener('focusin', function(ev){
    var t = ev.target; if(!t.classList || !t.classList.contains('seg')) return;
    var g = t.closest('.col');
    showTip(null, g.getAttribute('data-host'), g.getAttribute('data-day'), t.getAttribute('data-key'), t);
  });
  svg.addEventListener('focusout', hideTip);
  return section;
}

function drawTables(days){
  var frag = document.createDocumentFragment();
  var series = activeSeries(days);
  D.hosts.forEach(function(host, idx){
    var sec = h('div',{class:'host'});
    sec.appendChild(h('div',{class:'host-head'},[h('h2',{text:host})]));
    var t = h('table');
    t.appendChild(h('caption',{class:'sr',text:host+': changes per day'}));
    var hr = h('tr',{},[h('th',{text:'Date'})]);
    series.forEach(function(s){ hr.appendChild(h('th',{class:'num',text:s.label})); });
    hr.appendChild(h('th',{class:'num',text:'Total'}));
    if(D.hasRuns) hr.appendChild(h('th',{text:'Run'}));
    t.appendChild(h('thead',{},[hr]));
    var body = h('tbody');
    days.slice().reverse().forEach(function(day){
      var c = cell(host, day), ri = runInfo(host, day);
      var tr = h('tr',{},[h('td',{text:fmtDay(day,true)})]);
      series.forEach(function(s){
        var n = c.counts[s.key] || 0, td = h('td',{class:'num'});
        if(n){
          td.appendChild(h('button',{type:'button',class:'cellbtn',text:String(n),
            'aria-label':host+', '+fmtDay(day)+', '+s.label+': '+n,
            on:{click:function(){ select(host, day, s.key); }}}));
        } else { td.className = 'num zero'; td.textContent = '0'; }
        tr.appendChild(td);
      });
      var tt = h('td',{class:'num'});
      if(c.total) tt.appendChild(h('button',{type:'button',class:'cellbtn',text:String(c.total),
        'aria-label':host+', '+fmtDay(day)+', all changes: '+c.total,
        on:{click:function(){ select(host, day, null); }}}));
      else { tt.className = 'num zero'; tt.textContent = '0'; }
      tr.appendChild(tt);
      if(D.hasRuns) tr.appendChild(h('td',{text: ri ? ri.text : ''}));
      body.appendChild(tr);
    });
    t.appendChild(body);
    sec.appendChild(h('div',{class:'tablewrap'},[t]));
    frag.appendChild(sec);
  });
  return frag;
}

function renderCharts(){
  var box = $('charts'); hideTip(); box.replaceChildren();
  var days = daysList();
  renderLegend(days);
  if(!D.hosts.length){ box.appendChild(h('div',{class:'empty',text:'No hosts in the database yet. Run autoruns_velociraptor.py first.'})); return; }
  if(state.table){ box.appendChild(drawTables(days)); return; }
  var gmax = 0;
  D.hosts.forEach(function(host){ days.forEach(function(d){ gmax = Math.max(gmax, cell(host,d).total); }); });
  D.hosts.forEach(function(host, idx){ box.appendChild(drawHost(host, idx, days, gmax)); });
}

/* ---------- selection and detail panel ---------- */
function paintSelection(){
  Array.prototype.forEach.call(document.querySelectorAll('#charts .col'), function(g){
    var on = isSel(g.getAttribute('data-host'), g.getAttribute('data-day'));
    var w = g.querySelector('.wash'); if(w) w.style.display = on ? '' : 'none';
    Array.prototype.forEach.call(g.querySelectorAll('.seg'), function(s){
      s.setAttribute('class', 'seg' + (on && state.sel.key===s.getAttribute('data-key') ? ' sel' : ''));
    });
  });
}
function select(host, day, key){
  var first = !state.sel;
  state.sel = {host:host, day:day, key:key||null};
  state.filter = '';
  paintSelection();
  renderPanel();
  var r = $('panel').getBoundingClientRect();
  if(first || r.top > innerHeight-80){
    if(r.top > innerHeight-80){
      var reduce = window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches;
      window.scrollTo({top:scrollY + r.top - Math.round(innerHeight*0.35), behavior:reduce?'auto':'smooth'});
    }
  }
}
function seriesLabel(key){
  var s = seriesList().filter(function(x){ return x.key===key; })[0];
  return s ? s.label : key;
}
function renderPanel(){
  var p = $('panel'); p.replaceChildren();
  if(!state.sel){
    p.appendChild(h('h2',{text:'Changes'}));
    p.appendChild(h('p',{class:'hint',text:'Click a bar segment above (or a count in the table view) to list its changes here. Click a different segment to replace the list.'}));
    return;
  }
  var s = state.sel, c = cell(s.host, s.day);
  var items = c.items.filter(function(x){ return !s.key || keyOf(x)===s.key; });
  p.appendChild(h('h2',{text:s.host+' · '+fmtDay(s.day,true)+(s.key?' · '+seriesLabel(s.key):'')}));
  p.appendChild(h('div',{class:'pmeta',text:items.length+' change'+(items.length===1?'':'s')+
    (s.key?' of '+c.total+' that day':'')+' · times in your local time zone'}));

  var chips = h('div',{class:'chips'});
  chips.appendChild(h('button',{type:'button',class:'chip','aria-pressed':String(!s.key),on:{click:function(){ state.sel.key=null; state.filter=''; paintSelection(); renderPanel(); }}},
    [h('span',{text:'All ('+c.total+')'})]));
  seriesList().forEach(function(x){
    var n = c.counts[x.key]; if(!n) return;
    chips.appendChild(h('button',{type:'button',class:'chip','aria-pressed':String(s.key===x.key),on:{click:function(){ state.sel.key=x.key; state.filter=''; paintSelection(); renderPanel(); }}},
      [sw(x.css),h('span',{text:x.label+' ('+n+')'})]));
  });
  chips.appendChild(h('span',{class:'sp'}));
  var inp = h('input',{type:'search',placeholder:'Filter these rows','aria-label':'Filter these rows'});
  inp.value = state.filter;
  chips.appendChild(inp);
  chips.appendChild(h('button',{type:'button',class:'tbtn',text:'Clear selection',on:{click:function(){ state.sel=null; state.filter=''; paintSelection(); renderPanel(); }}}));
  p.appendChild(chips);

  var holder = h('div',{class:'tablewrap'}); p.appendChild(holder);
  var more = h('div',{class:'note'}); p.appendChild(more);
  var showAll = false;
  function draw(){
    var q = state.filter.trim().toLowerCase();
    var rows = items.filter(function(x){
      if(!q) return true;
      return [x.type,x.cat,x.loc,x.entry,x.image,x.launch,x.signer,JSON.stringify(x.detail)].join('\n').toLowerCase().indexOf(q) >= 0;
    });
    rows = rows.slice().sort(function(a,b){ return b.t - a.t || a.type.localeCompare(b.type); });
    var shown = showAll ? rows : rows.slice(0, MAX_ROWS);
    holder.replaceChildren();
    var t = h('table');
    t.appendChild(h('thead',{},[h('tr',{},['Detected','Change','Category','Entry Location','Entry','Image Path','Launch String','Signer','What changed']
      .map(function(x){ return h('th',{text:x}); }))]));
    var body = h('tbody');
    shown.forEach(function(x){
      var ty = TYPE_BY[x.type] || {glyph:'',label:x.type};
      var det = h('td',{class:'detail'});
      x.detail.forEach(function(d){
        det.appendChild(h('div',{text:d[0]+': '+(d[1]||'(blank)')+' → '+(d[2]||'(blank)')}));
      });
      var when = h('td',{text:fmtTime(x.t),title:x.iso+' (UTC)'});
      body.appendChild(h('tr',{},[when,h('td',{class:'chg',text:ty.glyph+' '+ty.label}),h('td',{text:x.cat==='(none)'?'':x.cat}),
        h('td',{text:x.loc}),h('td',{text:x.entry}),h('td',{text:x.image}),h('td',{text:x.launch}),h('td',{text:x.signer}),det]));
    });
    t.appendChild(body);
    holder.appendChild(t);
    more.replaceChildren();
    if(!rows.length) more.textContent = 'No rows match the filter.';
    else if(shown.length < rows.length){
      more.appendChild(document.createTextNode('Showing the newest '+shown.length+' of '+rows.length+' rows. '));
      more.appendChild(h('button',{type:'button',class:'tbtn',text:'Show all '+rows.length,on:{click:function(){ showAll=true; draw(); }}}));
    }
  }
  inp.addEventListener('input', function(){ state.filter = inp.value; showAll = false; draw(); });
  draw();
}

/* ---------- boot ---------- */
function renderNotes(){
  var n = $('notes'); n.replaceChildren();
  var t = 'Bars are counted by the day each change was collected, which is when the nightly run saw it, not when it happened on the machine. Segments under 4 px are drawn at 4 px so they stay clickable.';
  if(!D.hasRuns) t += ' Run history is not being recorded yet, so a quiet day cannot be told apart from a day the machine was off (see runs_table.patch).';
  else t += ' Days before a host’s first recorded run have no marker.';
  n.textContent = t;
}
function renderAll(){ renderControls(); renderCharts(); renderPanel(); renderNotes(); }
$('gen').textContent = 'Generated '+fmtTime(new Date(D.generated))+' · last '+D.historyDays+' days of changes · times in your time zone';
renderAll();
var rt = null, lastW = $('charts').clientWidth;
window.addEventListener('resize', function(){
  if(rt) cancelAnimationFrame(rt);
  rt = requestAnimationFrame(function(){
    var w = $('charts').clientWidth;
    if(w !== lastW){ lastW = w; renderCharts(); }
  });
});
})();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    sys.exit(main())
