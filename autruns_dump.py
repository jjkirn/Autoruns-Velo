#!/usr/bin/env python3
"""
autoruns_dump.py

Save the full Sysinternals Autoruns data (every column) for one Windows host
to a CSV or JSON file, using the Velociraptor API.

By default it starts a fresh Windows.Sysinternals.Autoruns collection on the
host and waits for it to finish. Use --flow-id to export a collection that
already exists (for example one you ran from the GUI) without running a new one.

Examples:
  python3 autoruns_dump.py --api-config api.config.yaml --host hp-envy
  python3 autoruns_dump.py --api-config api.config.yaml --host hp-envy --format json
  python3 autoruns_dump.py --api-config api.config.yaml --host hp-envy \
      --flow-id F.DB246QN0SP0PC --out hp-envy-autoruns.csv

Needs autoruns_velociraptor.py in the same folder (it reuses its API helpers).
"""

import argparse
import csv
import datetime as dt
import json
import sys
import time
from pathlib import Path

import autoruns_velociraptor as av


def find_client(velo, host):
    """Return (hostname, client_id) for a host name (case-insensitive)."""
    matches = [
        c for c in av.list_clients(velo)
        if str(c.get("os", "")).lower() == "windows"
        and (c.get("hostname") or "").lower() == host.lower()
    ]
    if not matches:
        sys.exit("No Windows client named %r found on the server." % host)
    if len(matches) > 1:
        # Several clients share the name: use the one seen most recently.
        matches.sort(key=lambda c: float(c.get("last_seen_at") or 0), reverse=True)
        print("Note: %d clients named %s; using the most recently seen." % (len(matches), host))
    c = matches[0]
    return c["hostname"], c["client_id"], float(c.get("last_seen_at") or 0)


def wait_for_flow(velo, client_id, flow_id, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = av.flow_state(velo, client_id, flow_id)
        if state == "FINISHED":
            return
        if state == "ERROR":
            sys.exit("The collection ended in ERROR. Check the flow's Logs tab in the GUI.")
        time.sleep(5)
    sys.exit("The collection did not finish within %d seconds." % timeout)


def write_rows(rows, path, fmt):
    """Write all rows with every column. Nested values are stored as JSON text."""
    columns = []
    for row in rows:
        for key in row:
            if key not in columns:
                columns.append(key)
    if fmt == "json":
        Path(path).write_text(json.dumps(rows, indent=2), encoding="utf-8")
        return columns
    with open(path, "w", newline="", encoding="utf-8-sig") as f:  # utf-8-sig opens cleanly in Excel
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({
                k: (json.dumps(v) if isinstance(v, (dict, list)) else ("" if v is None else v))
                for k, v in row.items()
            })
    return columns


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--api-config", required=True, help="Velociraptor api.config.yaml")
    ap.add_argument("--host", required=True, help="host name, for example hp-envy")
    ap.add_argument("--flow-id", help="export an existing collection instead of running a new one")
    ap.add_argument("--format", choices=["csv", "json"], default="csv")
    ap.add_argument("--out", help="output file (default: <host>-autoruns-<date>.<format>)")
    ap.add_argument("--timeout", type=int, default=900, help="seconds to wait (default 900)")
    ap.add_argument("--online-window", type=int, default=600,
                    help="host counts as online if seen within N seconds (default 600)")
    args = ap.parse_args()

    velo = av.Velo(args.api_config)
    try:
        host, client_id, last_seen = find_client(velo, args.host)

        if args.flow_id:
            flow_id = args.flow_id
            print("Using existing collection %s on %s" % (flow_id, host))
            wait_for_flow(velo, client_id, flow_id, args.timeout)
        else:
            if time.time() * 1e6 - last_seen > args.online_window * 1e6:
                sys.exit("%s is offline (not seen in the last %d seconds). "
                         "Run again when it is on, or use --flow-id for an earlier collection."
                         % (host, args.online_window))
            flow_id = av.start_collection(velo, client_id)
            print("Started collection %s on %s, waiting..." % (flow_id, host))
            wait_for_flow(velo, client_id, flow_id, args.timeout)

        rows = av.fetch_results(velo, client_id, flow_id)
    finally:
        velo.close()

    if not rows:
        sys.exit("The collection returned 0 rows; nothing to save.")

    out = args.out or "%s-autoruns-%s.%s" % (
        host, dt.datetime.now().strftime("%Y%m%d-%H%M"), args.format)
    columns = write_rows(rows, out, args.format)
    print("Saved %d rows, %d columns to %s" % (len(rows), len(columns), out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
