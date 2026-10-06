#!/usr/bin/env python3
"""
autoruns_velociraptor.py

Collect Sysinternals Autoruns from Windows hosts through the Velociraptor API,
diff against a per-host SQLite baseline, keep a 30-day rolling change history,
and write two reports: delta.html (this run) and history.html (last 30 days).

Setup (once):
  1. On the Velociraptor server, create an API client config:
       velociraptor --config server.config.yaml config api_client \
           --name autoruns_api --role api,investigator api.config.yaml
  2. pip install pyvelociraptor pyyaml
  3. Make sure the server's API port (api_connection_string in api.config.yaml,
     usually 127.0.0.1:8001) is reachable from where this script runs.

Usage:
  python3 autoruns_velociraptor.py --api-config api.config.yaml
  python3 autoruns_velociraptor.py --api-config api.config.yaml \
      --db autoruns.sqlite --out-dir ./reports --hosts hp-envy,KirnPC

The first run for a host only establishes its baseline (no changes reported).
Later runs report entries as ADDED, REMOVED or MODIFIED.
"""

import argparse
import datetime as dt
import html
import json
import sqlite3
import sys
import time
from pathlib import Path

ARTIFACT = "Windows.Sysinternals.Autoruns"

# What makes an autorun entry "the same entry" between runs.
# NOTE: these names are the column headers seen in the GUI. If the API returns
# different field names, change them here (run with --dump-columns to check).
KEY_FIELDS = ["Entry Location", "Entry", "Image Path", "Launch String"]

# If the same entry (same key) differs in any of these, it is reported MODIFIED.
WATCH_FIELDS = ["SHA-256", "MD5", "Signer", "Company", "Version", "Enabled", "Category"]

# Fields stored in the baseline (key fields + watched fields + context).
STORE_FIELDS = KEY_FIELDS + WATCH_FIELDS + ["Profile", "Description"]

HISTORY_DAYS = 30


# --------------------------------------------------------------------------
# Velociraptor API
# --------------------------------------------------------------------------
class Velo:
    def __init__(self, config_path):
        import grpc
        import yaml
        from pyvelociraptor import api_pb2, api_pb2_grpc

        self.api_pb2 = api_pb2
        with open(config_path) as f:
            config = yaml.safe_load(f)
        creds = grpc.ssl_channel_credentials(
            root_certificates=config["ca_certificate"].encode("utf8"),
            private_key=config["client_private_key"].encode("utf8"),
            certificate_chain=config["client_cert"].encode("utf8"),
        )
        options = (("grpc.ssl_target_name_override", "VelociraptorServer"),)
        self.channel = grpc.secure_channel(
            config["api_connection_string"], creds, options
        )
        self.stub = api_pb2_grpc.APIStub(self.channel)

    def query(self, vql):
        """Run a VQL query, return a list of row dicts."""
        request = self.api_pb2.VQLCollectorArgs(
            max_wait=10,
            max_row=100000,
            Query=[self.api_pb2.VQLRequest(Name="autoruns", VQL=vql)],
        )
        rows = []
        for response in self.stub.Query(request):
            if response.Response:
                rows.extend(json.loads(response.Response))
        return rows

    def close(self):
        self.channel.close()


def vql_str(value):
    """Quote a value as a VQL string literal."""
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def list_clients(velo):
    return velo.query(
        "SELECT client_id, os_info.hostname AS hostname, os_info.system AS os, "
        "last_seen_at FROM clients()"
    )


def start_collection(velo, client_id):
    rows = velo.query(
        "SELECT collect_client(client_id=%s, artifacts=%s) AS flow FROM scope()"
        % (vql_str(client_id), vql_str(ARTIFACT))
    )
    if not rows:
        raise RuntimeError("collect_client returned nothing")
    flow = rows[0].get("flow") or {}
    flow_id = flow.get("session_id") or flow.get("flow_id")
    if not flow_id:
        raise RuntimeError("could not find flow id in %r" % (flow,))
    return flow_id


def flow_state(velo, client_id, flow_id):
    rows = velo.query(
        "SELECT session_id, state FROM flows(client_id=%s, flow_id=%s)"
        % (vql_str(client_id), vql_str(flow_id))
    )
    return str(rows[0].get("state", "")).upper() if rows else ""


def fetch_results(velo, client_id, flow_id):
    return velo.query(
        "SELECT * FROM source(client_id=%s, flow_id=%s, artifact=%s)"
        % (vql_str(client_id), vql_str(flow_id), vql_str(ARTIFACT))
    )


# --------------------------------------------------------------------------
# Diff logic (pure functions, no network)
# --------------------------------------------------------------------------
def norm(value):
    return "" if value is None else str(value).strip()


def make_key(row):
    return "|".join(norm(row.get(f)).lower() for f in KEY_FIELDS)


def snapshot(rows):
    """Turn raw result rows into {key: record}. Duplicate keys: first one wins."""
    snap = {}
    for row in rows:
        key = make_key(row)
        if key.strip("|") == "":
            continue
        if key not in snap:
            snap[key] = {f: norm(row.get(f)) for f in STORE_FIELDS}
    return snap


def diff(old, new):
    """Compare two snapshots. Returns a list of change dicts."""
    changes = []
    for key, rec in new.items():
        if key not in old:
            changes.append({"type": "ADDED", "key": key, "record": rec, "detail": {}})
        else:
            detail = {}
            for f in WATCH_FIELDS:
                if old[key].get(f, "") != rec.get(f, ""):
                    detail[f] = [old[key].get(f, ""), rec.get(f, "")]
            if detail:
                changes.append(
                    {"type": "MODIFIED", "key": key, "record": rec, "detail": detail}
                )
    for key, rec in old.items():
        if key not in new:
            changes.append({"type": "REMOVED", "key": key, "record": rec, "detail": {}})
    return changes


# --------------------------------------------------------------------------
# SQLite storage
# --------------------------------------------------------------------------
def init_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS baseline (
            host TEXT NOT NULL,
            key  TEXT NOT NULL,
            data TEXT NOT NULL,
            PRIMARY KEY (host, key)
        );
        CREATE TABLE IF NOT EXISTS changes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            host TEXT NOT NULL,
            detected TEXT NOT NULL,
            change_type TEXT NOT NULL,
            key TEXT NOT NULL,
            data TEXT NOT NULL,
            detail TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS host_runs (
            host TEXT PRIMARY KEY,
            last_success TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_changes_detected ON changes(detected);
        """
    )
    return conn


def load_baseline(conn, host):
    cur = conn.execute("SELECT key, data FROM baseline WHERE host = ?", (host,))
    return {k: json.loads(d) for k, d in cur.fetchall()}


def apply_run(conn, host, new_snap, changes, stamp, first_run):
    with conn:
        if not first_run:
            for c in changes:
                conn.execute(
                    "INSERT INTO changes (host, detected, change_type, key, data, detail) "
                    "VALUES (?,?,?,?,?,?)",
                    (host, stamp, c["type"], c["key"], json.dumps(c["record"]),
                     json.dumps(c["detail"])),
                )
        conn.execute("DELETE FROM baseline WHERE host = ?", (host,))
        conn.executemany(
            "INSERT INTO baseline (host, key, data) VALUES (?,?,?)",
            [(host, k, json.dumps(v)) for k, v in new_snap.items()],
        )
        conn.execute(
            "INSERT INTO host_runs (host, last_success) VALUES (?, ?) "
            "ON CONFLICT(host) DO UPDATE SET last_success = excluded.last_success",
            (host, stamp),
        )


def prune_history(conn):
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=HISTORY_DAYS)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    with conn:
        conn.execute("DELETE FROM changes WHERE detected < ?", (cutoff,))


# --------------------------------------------------------------------------
# Reports
# --------------------------------------------------------------------------
CSS = """
body{font-family:system-ui,sans-serif;margin:24px;color:#1b1b1b}
h1{margin-bottom:4px} .sub{color:#666;margin-bottom:20px}
table{border-collapse:collapse;width:100%;margin:8px 0 24px}
th,td{border:1px solid #ccc;padding:6px 8px;text-align:left;vertical-align:top;
      font-size:13px;word-break:break-word}
th{background:#f0f0f0}
.ADDED{background:#e6f6e6} .REMOVED{background:#fbe7e7} .MODIFIED{background:#fff4d6}
.note{background:#eef3fb;padding:8px 12px;border-radius:4px;margin:6px 0}
"""


def esc(v):
    return html.escape(str(v))


def change_rows(changes):
    out = []
    for c in changes:
        rec = c["record"]
        detail = "; ".join(
            "%s: %s &rarr; %s" % (esc(f), esc(o) or "(blank)", esc(n) or "(blank)")
            for f, (o, n) in c["detail"].items()
        )
        out.append(
            '<tr class="%s"><td>%s</td><td>%s</td><td>%s</td><td>%s</td>'
            "<td>%s</td><td>%s</td><td>%s</td></tr>"
            % (
                c["type"], c["type"], esc(rec.get("Entry Location", "")),
                esc(rec.get("Entry", "")), esc(rec.get("Image Path", "")),
                esc(rec.get("Launch String", "")), esc(rec.get("Signer", "")), detail,
            )
        )
    return "\n".join(out)


TABLE_HEAD = (
    "<tr><th>Change</th><th>Entry Location</th><th>Entry</th><th>Image Path</th>"
    "<th>Launch String</th><th>Signer</th><th>What changed</th></tr>"
)


def write_delta(path, run_stamp, per_host, notes):
    parts = [
        "<!doctype html><meta charset='utf-8'><title>Autoruns delta</title>",
        "<style>%s</style>" % CSS,
        "<h1>Autoruns changes</h1>",
        "<div class='sub'>Run at %s</div>" % esc(run_stamp),
    ]
    for n in notes:
        parts.append("<div class='note'>%s</div>" % esc(n))
    for host, (changes, first_run) in sorted(per_host.items()):
        parts.append("<h2>%s</h2>" % esc(host))
        if first_run:
            parts.append(
                "<div class='note'>Baseline established, no comparison on the first run.</div>"
            )
        elif not changes:
            parts.append("<p>No changes.</p>")
        else:
            parts.append("<table>%s%s</table>" % (TABLE_HEAD, change_rows(changes)))
    Path(path).write_text("\n".join(parts), encoding="utf-8")


def write_history(path, conn):
    rows = conn.execute(
        "SELECT host, detected, change_type, key, data, detail FROM changes "
        "ORDER BY detected DESC, id DESC"
    ).fetchall()
    parts = [
        "<!doctype html><meta charset='utf-8'><title>Autoruns history</title>",
        "<style>%s</style>" % CSS,
        "<h1>Autoruns history</h1>",
        "<div class='sub'>Changes from the last %d days, newest first</div>" % HISTORY_DAYS,
    ]
    if not rows:
        parts.append("<p>No changes recorded.</p>")
    else:
        parts.append(
            "<table><tr><th>Detected (UTC)</th><th>Host</th>"
            + TABLE_HEAD[len("<tr>"):]
        )
        for host, detected, ctype, key, data, detail in rows:
            c = {"type": ctype, "key": key, "record": json.loads(data),
                 "detail": json.loads(detail)}
            body = change_rows([c])
            body = body.replace(
                "<tr class=\"%s\">" % ctype,
                "<tr class=\"%s\"><td>%s</td><td>%s</td>" % (ctype, esc(detected), esc(host)),
                1,
            )
            parts.append(body)
        parts.append("</table>")
    Path(path).write_text("\n".join(parts), encoding="utf-8")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--api-config", required=True, help="Velociraptor api.config.yaml")
    ap.add_argument("--db", default="autoruns.sqlite")
    ap.add_argument("--out-dir", default="reports")
    ap.add_argument("--timeout", type=int, default=900,
                    help="seconds to wait for collections (default 900)")
    ap.add_argument("--online-window", type=int, default=600,
                    help="client counts as online if seen within N seconds (default 600)")
    ap.add_argument("--hosts", help="comma-separated hostnames to include (default: all)")
    ap.add_argument("--dump-columns", action="store_true",
                    help="print the field names of the first result row, then continue")
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    conn = init_db(args.db)
    velo = Velo(args.api_config)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    notes = []

    wanted = {h.strip().lower() for h in args.hosts.split(",")} if args.hosts else None
    now_us = time.time() * 1e6
    targets = {}  # hostname -> client_id
    for c in list_clients(velo):
        host = c.get("hostname") or c.get("client_id")
        if str(c.get("os", "")).lower() != "windows":
            continue
        if wanted and host.lower() not in wanted:
            continue
        online = (now_us - float(c.get("last_seen_at") or 0)) < args.online_window * 1e6
        if not online:
            notes.append("%s was offline and skipped this run." % host)
            continue
        targets[host] = c["client_id"]

    # Start every collection first so the hosts work in parallel.
    flows = {}
    for host, cid in targets.items():
        try:
            flows[host] = start_collection(velo, cid)
            print("started %s on %s (%s)" % (ARTIFACT, host, flows[host]))
        except Exception as e:
            notes.append("%s: could not start collection (%s)" % (host, e))

    # Poll until all finish or the timeout hits.
    pending = dict(flows)
    deadline = time.time() + args.timeout
    done = {}
    while pending and time.time() < deadline:
        for host in list(pending):
            state = flow_state(velo, targets[host], pending[host])
            if state == "FINISHED":
                done[host] = pending.pop(host)
            elif state == "ERROR":
                notes.append("%s: collection ended in ERROR." % host)
                pending.pop(host)
        if pending:
            time.sleep(5)
    for host in pending:
        notes.append("%s: collection did not finish within %ds." % (host, args.timeout))

    per_host = {}
    for host, flow_id in done.items():
        rows = fetch_results(velo, targets[host], flow_id)
        if args.dump_columns and rows:
            print("columns:", list(rows[0].keys()))
        if not rows:
            notes.append("%s: collection returned 0 rows; baseline left unchanged." % host)
            continue
        new_snap = snapshot(rows)
        old_snap = load_baseline(conn, host)
        first_run = not old_snap
        changes = [] if first_run else diff(old_snap, new_snap)
        apply_run(conn, host, new_snap, changes, stamp, first_run)
        per_host[host] = (changes, first_run)
        print("%s: %d entries, %s" % (
            host, len(new_snap),
            "baseline created" if first_run else "%d changes" % len(changes)))

    prune_history(conn)
    write_delta(out_dir / "delta.html", stamp, per_host, notes)
    write_history(out_dir / "history.html", conn)
    velo.close()
    print("wrote %s and %s" % (out_dir / "delta.html", out_dir / "history.html"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
    