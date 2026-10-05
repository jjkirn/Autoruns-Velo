#!/usr/bin/env python3
"""Nightly Autoruns delta reports using the Velociraptor gRPC API.

PROTOTYPE: not yet tested end to end. See README.md before relying on it.

Usage:
  autoruns_delta.py launch
  autoruns_delta.py collect [--rebaseline]
  autoruns_delta.py history [--host H] [--days N] [--change added|removed|changed]
  autoruns_delta.py history-html [--days N]

Settings come from environment variables (all optional):
  AR_API_CONFIG   path to the Velociraptor api_client.yaml   (default: api_client.yaml)
  AR_DATA_DIR     where the database and reports are kept     (default: data)
  AR_KEEP_DAYS    days of change history to keep              (default: 30)
  AR_LABEL        only hunt clients that carry this label     (default: all Windows clients)
  AR_HUNT_HOURS   how long a hunt stays open for late hosts   (default: 24)
"""
import argparse
import datetime
import html
import json
import os
import sqlite3
import sys

import grpc
import pyvelociraptor
from pyvelociraptor import api_pb2, api_pb2_grpc

API_CONFIG = os.environ.get("AR_API_CONFIG", "api_client.yaml")
DATA_DIR = os.environ.get("AR_DATA_DIR", "data")
KEEP_DAYS = int(os.environ.get("AR_KEEP_DAYS", "30"))
LABEL = os.environ.get("AR_LABEL", "")
HUNT_HOURS = int(os.environ.get("AR_HUNT_HOURS", "24"))

STATE = os.path.join(DATA_DIR, "state.json")
DB = os.path.join(DATA_DIR, "autoruns.db")
REPORT = os.path.join(DATA_DIR, "delta.html")
HISTORY = os.path.join(DATA_DIR, "history.html")

# --------------------------------------------------------------------------
# Velociraptor queries. Verify function and column names against your server
# version (see README, "Things to verify").
# --------------------------------------------------------------------------
PULL = """
SELECT * FROM foreach(
  row={ SELECT ClientId, FlowId FROM hunt_flows(hunt_id=hid) },
  query={ SELECT client_info(client_id=ClientId).os_info.fqdn AS Host, *
          FROM source(client_id=ClientId, flow_id=FlowId,
                      artifact="Windows.Sysinternals.Autoruns") })"""


def launch_query():
    label = ", include_labels=lbl" if LABEL else ""
    return (
        'SELECT hunt(description="Nightly autoruns", '
        'artifacts="Windows.Sysinternals.Autoruns", os="Windows"'
        + label
        + f", expires=now() + {HUNT_HOURS * 3600}) AS Hunt FROM scope()"
    )


def vql(config, query, env=None):
    """Run a VQL query over the gRPC API and yield result rows (dicts)."""
    creds = grpc.ssl_channel_credentials(
        root_certificates=config["ca_certificate"].encode("utf8"),
        private_key=config["client_private_key"].encode("utf8"),
        certificate_chain=config["client_cert"].encode("utf8"),
    )
    opts = (("grpc.ssl_target_name_override", "VelociraptorServer"),)
    req = api_pb2.VQLCollectorArgs(
        max_wait=10,
        max_row=10000,
        Query=[api_pb2.VQLRequest(Name="Query", VQL=query)],
        env=[dict(key=k, value=v) for k, v in (env or {}).items()],
    )
    with grpc.secure_channel(config["api_connection_string"], creds, opts) as ch:
        for resp in api_pb2_grpc.APIStub(ch).Query(req):
            if resp.Response:
                yield from json.loads(resp.Response)


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------
SCHEMA = """CREATE TABLE IF NOT EXISTS {}(host TEXT, category TEXT, profile TEXT,
    entry TEXT, image TEXT, launch TEXT, signer TEXT, sha256 TEXT,
    PRIMARY KEY(host, category, profile, entry, image, launch))"""
LOG_SCHEMA = """CREATE TABLE IF NOT EXISTS delta_log(run_date TEXT, host TEXT,
    change TEXT, category TEXT, entry TEXT, image TEXT, launch TEXT,
    old_signer TEXT, new_signer TEXT, old_sha256 TEXT, new_sha256 TEXT)"""


def load(db, table, host):
    return {
        tuple(r[:6]): tuple(r[6:])
        for r in db.execute(f"SELECT * FROM {table} WHERE host=?", (host,))
    }


def save(db, table, host, rows):
    db.execute(f"DELETE FROM {table} WHERE host=?", (host,))
    db.executemany(
        f"INSERT INTO {table} VALUES (?,?,?,?,?,?,?,?)",
        [k + v for k, v in rows.items()],
    )


def diff(old, new):
    ev = [("ADDED", k, ("", ""), new[k]) for k in new if k not in old]
    ev += [("REMOVED", k, old[k], ("", "")) for k in old if k not in new]
    ev += [
        ("CHANGED", k, old[k], new[k]) for k in new if k in old and new[k] != old[k]
    ]
    return ev


def fmt(e):
    change, k, old, new = e
    s = f"{change} {k[1]} | {k[3]} | {k[4]}"
    if change == "ADDED":
        s += f" | signer={new[0] or 'none'}"
    if change == "CHANGED":
        s += f" | {old} -> {new}"
    return s


def compare(rows, rebaseline):
    """Compare today's rows with each host's baseline and with its last run.

    The report shows changes against the baseline (which stays fixed until you
    re-baseline). The history logs day-to-day changes against the previous run,
    so the same change is not recorded every night.
    """
    db = sqlite3.connect(DB)
    for t in ("baseline", "last_run"):
        db.execute(SCHEMA.format(t))
    db.execute(LOG_SCHEMA)
    db.execute("CREATE INDEX IF NOT EXISTS idx_log_date ON delta_log(run_date)")
    run = datetime.date.today().isoformat()

    today = {}
    for r in rows:
        def g(k, r=r):
            return str(r.get(k) or "")
        key = (
            g("Host"), g("Category"), g("Profile"),
            g("Entry"), g("Image Path"), g("Launch String"),
        )
        today[key] = (g("Signer"), g("SHA-256"))

    responded = {k[0] for k in today}
    known = {h for (h,) in db.execute("SELECT DISTINCT host FROM baseline")}
    report = []
    for h in sorted(responded):
        t = {k: v for k, v in today.items() if k[0] == h}
        base, prev = load(db, "baseline", h), load(db, "last_run", h)
        events = diff(prev, t) if prev else []
        db.executemany(
            "INSERT INTO delta_log VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [
                (run, h, c, k[1], k[3], k[4], k[5], o[0], n[0], o[1], n[1])
                for c, k, o, n in events
            ],
        )
        save(db, "last_run", h, t)
        if rebaseline or not base:
            save(db, "baseline", h, t)
            report.append((h, "baseline created", [f"{len(t)} entries"]))
        else:
            lines = [fmt(e) for e in diff(base, t)]
            report.append((h, f"{len(lines)} changes vs baseline", lines))

    cutoff = (
        datetime.date.today() - datetime.timedelta(days=KEEP_DAYS)
    ).isoformat()
    db.execute("DELETE FROM delta_log WHERE run_date < ?", (cutoff,))
    db.commit()
    return report, sorted(known - responded)


# --------------------------------------------------------------------------
# Reports
# --------------------------------------------------------------------------
def write_report(report, silent):
    with open(REPORT, "w") as f:
        f.write(f"<h1>Autoruns delta {datetime.datetime.now():%Y-%m-%d}</h1>")
        if silent:
            f.write(
                "<p><b>No data from:</b> " + html.escape(", ".join(silent)) + "</p>"
            )
        for h, s, lines in report:
            f.write(f"<h2>{html.escape(h)}: {s}</h2><ul>")
            f.writelines(f"<li>{html.escape(x)}</li>" for x in lines)
            f.write("</ul>")


PAGE = """<!doctype html><html><head><meta charset="utf-8">
<title>Autoruns history</title>
<style>
body{font:14px system-ui,sans-serif;margin:1.5rem;background:#fff;color:#222}
@media(prefers-color-scheme:dark){body{background:#1b1b1b;color:#ddd}}
.bar{display:flex;gap:.5rem;flex-wrap:wrap;margin:1rem 0}
input,select{padding:.3rem .5rem;font:inherit}
table{border-collapse:collapse;width:100%}
th,td{text-align:left;padding:.3rem .5rem;border-bottom:1px solid #8884;vertical-align:top;word-break:break-all}
.b{padding:.1rem .4rem;border-radius:4px;font-size:12px;color:#fff}
.b.added{background:#2e7d32}.b.removed{background:#b26a00}.b.changed{background:#c62828}
</style></head><body>
<h1>Autoruns history</h1>
<p>Last __DAYS__ days, generated __GENERATED__. <span id="n"></span> rows shown.</p>
<div class="bar">
<input id="q" placeholder="Search entry, path, launch string...">
<select id="h"><option value="">All hosts</option>__HOSTS__</select>
<select id="c"><option value="">All changes</option><option>ADDED</option><option>REMOVED</option><option>CHANGED</option></select>
</div>
<table><thead><tr><th>Date</th><th>Host</th><th>Change</th><th>Category</th><th>Entry</th><th>Image path</th><th>Launch string</th><th>Detail</th></tr></thead>
<tbody id="t">__ROWS__</tbody></table>
<script>
const q=document.getElementById('q'),h=document.getElementById('h'),c=document.getElementById('c'),
rows=[...document.querySelectorAll('#t tr')];
function f(){const s=q.value.toLowerCase();let n=0;
rows.forEach(r=>{const x=r.children,ok=(!h.value||x[1].textContent===h.value)&&(!c.value||x[2].textContent===c.value)&&(!s||r.textContent.toLowerCase().includes(s));r.hidden=!ok;if(ok)n++;});
document.getElementById('n').textContent=n;}
[q,h,c].forEach(e=>e.addEventListener('input',f));f();
</script></body></html>"""


def detail(change, old_signer, new_signer, old_hash, new_hash):
    if change == "ADDED":
        return f"signer: {new_signer or 'none'}"
    if change == "REMOVED":
        return f"signer: {old_signer or 'none'}"
    return (
        f"signer: {old_signer or 'none'} -> {new_signer or 'none'}; "
        f"hash: {(old_hash or '')[:8]} -> {(new_hash or '')[:8]}"
    )


def write_history_html(days=KEEP_DAYS):
    db = sqlite3.connect(DB)
    db.execute(LOG_SCHEMA)
    cutoff = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    rows = db.execute(
        """SELECT run_date, host, change, category, entry, image, launch,
                  old_signer, new_signer, old_sha256, new_sha256
           FROM delta_log WHERE run_date >= ?
           ORDER BY run_date DESC, host, change""",
        (cutoff,),
    ).fetchall()
    e = html.escape
    body = "".join(
        f"<tr><td>{e(r[0])}</td><td>{e(r[1])}</td>"
        f"<td><span class='b {r[2].lower()}'>{r[2]}</span></td>"
        f"<td>{e(r[3])}</td><td>{e(r[4])}</td><td>{e(r[5])}</td><td>{e(r[6])}</td>"
        f"<td>{e(detail(r[2], r[7], r[8], r[9], r[10]))}</td></tr>"
        for r in rows
    )
    hosts = "".join(f"<option>{e(x)}</option>" for x in sorted({r[1] for r in rows}))
    page = (
        PAGE.replace("__ROWS__", body)
        .replace("__HOSTS__", hosts)
        .replace("__DAYS__", str(days))
        .replace("__GENERATED__", f"{datetime.datetime.now():%Y-%m-%d %H:%M}")
    )
    with open(HISTORY, "w") as f:
        f.write(page)


def history(host, days, change):
    db = sqlite3.connect(DB)
    db.execute(LOG_SCHEMA)
    cutoff = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    q = (
        "SELECT run_date, host, change, category, entry, image, new_signer "
        "FROM delta_log WHERE run_date >= ?"
    )
    args = [cutoff]
    if host:
        q += " AND host LIKE ?"
        args.append(f"%{host}%")
    if change:
        q += " AND change = ?"
        args.append(change.upper())
    for r in db.execute(q + " ORDER BY run_date DESC, host", args):
        print(" | ".join(str(x) for x in r))


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------
def launch(config):
    env = {"lbl": LABEL} if LABEL else None
    h = list(vql(config, launch_query(), env=env))[0]["Hunt"]
    hunt_id = h.get("hunt_id") or h.get("HuntId")
    with open(STATE, "w") as f:
        json.dump({"hunt_id": hunt_id}, f)
    print("launched", hunt_id)


def collect(config, rebaseline):
    try:
        with open(STATE) as f:
            hunt_id = json.load(f)["hunt_id"]
    except FileNotFoundError:
        sys.exit("No hunt recorded yet. Run 'launch' first.")
    rows = list(vql(config, PULL, env={"hid": hunt_id}))
    report, silent = compare(rows, rebaseline)
    write_report(report, silent)
    write_history_html()
    print(f"{len({r.get('Host') for r in rows})} hosts reported, "
          f"{len(silent)} known hosts silent")


def main():
    ap = argparse.ArgumentParser(description="Autoruns delta via Velociraptor")
    ap.add_argument("cmd", choices=["launch", "collect", "history", "history-html"])
    ap.add_argument("--rebaseline", action="store_true",
                    help="reset each reporting host's baseline to today's data")
    ap.add_argument("--host", help="history: filter by host name (substring)")
    ap.add_argument("--days", type=int, default=KEEP_DAYS)
    ap.add_argument("--change", choices=["added", "removed", "changed"])
    a = ap.parse_args()

    os.makedirs(DATA_DIR, exist_ok=True)
    if a.cmd == "history":
        history(a.host, a.days, a.change)
    elif a.cmd == "history-html":
        write_history_html(a.days)
    else:
        cfg = pyvelociraptor.LoadConfigFile(API_CONFIG)
        if a.cmd == "launch":
            launch(cfg)
        else:
            collect(cfg, a.rebaseline)


if __name__ == "__main__":
    main()
