#!/usr/bin/env python3
"""
nofile_review.py

Read-only helper. Sorts the "No file" Autoruns entries from autoruns.sqlite into
review groups and writes a plain-text report. Changes nothing.

  python3 nofile_review.py --db autoruns.sqlite --out reports/nofile_review.txt

Groups:
  1 USER-WRITABLE      missing file under Users, AppData, Temp or ProgramData
  2 THIRD-PARTY        missing file under Program Files or another non-Windows folder
  3 WINDOWS BUILT-IN   missing file under C:\\Windows (usually harmless leftovers)
  4 NAME ONLY          Image Path is just a file name, no folder
  5 NO PATH            nothing to point at (blank Image Path)
"""
import argparse
import collections
import json
import ntpath
import sqlite3
import sys
from pathlib import Path

import unsigned_report as u

PREFIX = "file not found:"


def group_of(img):
    raw = (img or "").strip()
    if not raw:
        return "5 NO PATH", ""
    low = raw.lower()
    if low.startswith(PREFIX):
        low = low[len(PREFIX):].strip()
    if not low:
        return "5 NO PATH", ""
    if "\\" not in low:
        return "4 NAME ONLY", low
    if low.startswith("c:\\windows") or low.startswith("%systemroot%"):
        return "3 WINDOWS BUILT-IN", low
    if any(x in low for x in ("\\users\\", "\\appdata\\", "\\temp\\", "\\programdata\\")):
        return "1 USER-WRITABLE", low
    return "2 THIRD-PARTY", low


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="autoruns.sqlite")
    ap.add_argument("--out", default="reports/nofile_review.txt")
    a = ap.parse_args()
    if not Path(a.db).exists():
        print("database not found: %s" % a.db, file=sys.stderr)
        return 1
    conn = sqlite3.connect("file:%s?mode=ro" % Path(a.db).resolve().as_posix(), uri=True)
    groups = collections.defaultdict(lambda: collections.defaultdict(list))
    total = 0
    for host, data in conn.execute("SELECT host, data FROM baseline ORDER BY host"):
        r = json.loads(data)
        if u.classify(r) != "nofile":
            continue
        total += 1
        g, low = group_of(r.get("Image Path"))
        folder = ntpath.dirname(low) if g in ("1 USER-WRITABLE", "2 THIRD-PARTY", "3 WINDOWS BUILT-IN") else (low or "(blank)")
        label = "%s  [%s]" % (r.get("Entry") or "(no name)", r.get("Category") or "?")
        groups[g][folder].append((host, label))
    lines = ["No-file review: %d entries" % total, ""]
    for g in sorted(groups):
        n = sum(len(v) for v in groups[g].values())
        lines.append("=== %s (%d) ===" % (g, n))
        for folder, items in sorted(groups[g].items(), key=lambda kv: (-len(kv[1]), kv[0])):
            hosts = sorted(set(h for h, _ in items))
            lines.append("%s   (%d entries, hosts: %s)" % (folder, len(items), ", ".join(hosts)))
            if g in ("1 USER-WRITABLE", "2 THIRD-PARTY", "4 NAME ONLY"):
                for host, label in sorted(set(items)):
                    lines.append("    %-9s %s" % (host, label))
        lines.append("")
    text = "\n".join(lines)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(text, encoding="utf-8")
    print(text)
    print("wrote %s" % a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
