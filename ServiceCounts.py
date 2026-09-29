#!/usr/bin/env python3
"""Count service occurrences per hour, plus totals (streams, safe for multi-GB logs).

A "service" is the text line right before a "Time is:" line, e.g.

    ========================================
    Validate Customer Start.......
    Time is: 2026-09-25 00:00:25.906066

    Start getAccounts.........................
    Time is: 2026-09-25 00:00:26.101010

gives services "Validate Customer Start" and "Start getAccounts"
(trailing dots and spaces are removed).

Only the services in SERVICES below are counted (exact name match).
Edit that list to add/remove services, or use --service / --all.

Usage:
    python ServiceCounts.py HDB_LOG09-25-2026.txt
    python ServiceCounts.py HDB_LOG09-25-2026.txt --csv service_counts.csv
    python ServiceCounts.py HDB_LOG09-25-2026.txt --service "Start ICT Transfer"
    python ServiceCounts.py HDB_LOG09-25-2026.txt --all      # every service found in the log
    python ServiceCounts.py log1.txt log2.txt
"""
import argparse
import csv
import re
import sys
from collections import Counter, defaultdict

TIME_PREFIX = b"Time is:"
SEPARATOR = b"====="
HOUR_RE = re.compile(rb"(\d{4}-\d{2}-\d{2})[ T](\d{2})")
UNKNOWN = ("unknown", "--")

# Services to count (exact names, without the trailing dots)
SERVICES = [
    "Validate Customer Start",
    "Start getAccounts",
    "Start getAccount",
    "Start ICT Transfer",
    "Customer Position",
]


def clean_name(raw):
    return raw.decode("utf-8", "replace").strip().rstrip(".").strip()


def count_file(fh, counts):
    """counts[service][(date, hour)] += 1 for every 'Time is:' line."""
    prev = None  # last non-blank, non-separator line
    for line in fh:
        s = line.strip()
        if not s:
            continue
        if s.startswith(TIME_PREFIX):
            if prev is not None:
                m = HOUR_RE.search(s)
                key = (m.group(1).decode(), m.group(2).decode()) if m else UNKNOWN
                counts[clean_name(prev)][key] += 1
                prev = None
        elif not s.startswith(SEPARATOR):
            prev = s


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="+", help="log file(s); '-' for stdin")
    ap.add_argument("--service", action="append", default=[],
                    help="exact service name to count instead of the built-in list (repeatable)")
    ap.add_argument("--all", action="store_true",
                    help="count every service found in the log, not just the list")
    ap.add_argument("--csv", help="write an Excel-friendly table: date, hour, one column per service, total")
    args = ap.parse_args()

    counts = defaultdict(Counter)
    for path in args.files:
        if path == "-":
            count_file(sys.stdin.buffer, counts)
        else:
            with open(path, "rb", buffering=16 * 1024 * 1024) as fh:
                count_file(fh, counts)

    if args.all:
        services = sorted(counts, key=lambda k: -sum(counts[k].values()))
        if not services:
            print("No services found.")
            return
    else:
        services = [clean_name(x.encode()) for x in (args.service or SERVICES)]
        counts = {k: counts.get(k, Counter()) for k in services}
    hours = sorted({h for c in counts.values() for h in c})
    grand_total = sum(sum(c.values()) for c in counts.values())
    width = max(len(s) for s in services + ["Service"]) + 2

    # Summary: total per service
    print("=== Totals per service ===")
    print(f"{'Service':<{width}}{'Count':>10}")
    print("-" * (width + 10))
    for s in services:
        print(f"{s:<{width}}{sum(counts[s].values()):>10}")
    print("-" * (width + 10))
    print(f"{'TOTAL':<{width}}{grand_total:>10}")

    # Per service, per hour
    for s in services:
        print(f"\n=== {s} ===")
        print(f"{'Date':<12}{'Hour':<7}{'Count':>10}")
        print("-" * 29)
        for d, h in sorted(counts[s]):
            print(f"{d:<12}{h + ':00' if h != '--' else h:<7}{counts[s][(d, h)]:>10}")
        print("-" * 29)
        print(f"{'Total':<19}{sum(counts[s].values()):>10}")

    if any(UNKNOWN in c for c in counts.values()):
        print("\nNOTE: some 'Time is:' lines had no parsable timestamp; counted under 'unknown'.",
              file=sys.stderr)

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as out:
            w = csv.writer(out)
            w.writerow(["date", "hour"] + services + ["total"])
            for d, h in hours:
                row = [counts[s][(d, h)] for s in services]
                w.writerow([d, h + ":00" if h != "--" else h] + row + [sum(row)])
            totals = [sum(counts[s].values()) for s in services]
            w.writerow(["TOTAL", ""] + totals + [grand_total])
        print(f"\nCSV written to {args.csv}")


if __name__ == "__main__":
    main()
