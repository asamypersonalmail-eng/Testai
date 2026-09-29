#!/usr/bin/env python3
"""Count log blocks per hour / minute / second for a given block title
(streams, safe for multi-GB files). Only periods that have requests are listed.

Expected block layout:

    ========================================
    MDP  getCreditCards Auth Request
    Time is: 2026-09-25 00:00:25.906066
    Message is ( ...

Usage:
    python3 count_requests_by_hour.py /path/to/big.log
    python3 count_requests_by_hour.py big.log --by second --csv per_second.csv
    python3 count_requests_by_hour.py big.log --by minute
    python3 count_requests_by_hour.py big.log other.log --csv out.csv
    python3 count_requests_by_hour.py big.log --title "MDP  getCreditCards Auth Request"
    zcat big.log.gz | python3 count_requests_by_hour.py -
"""
import argparse
import csv
import re
import sys
from collections import Counter

DEFAULT_TITLE = "MDP  getCreditCards Auth Request"
# Captures "YYYY-MM-DD" and "HH:MM:SS" from the "Time is:" line
TIME_RE = re.compile(rb"Time is:\s*(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})")
# How many characters of "HH:MM:SS" to keep for each grouping
GRANULARITY = {"hour": 2, "minute": 5, "second": 8}
# How many lines after the title to look for the "Time is:" line
LOOKAHEAD = 3


def count_file(fh, title, counts, stats, width):
    """Stream one binary file handle line by line and update counts."""
    pending = 0  # >0 while waiting for a "Time is:" line after a title match
    for line in fh:
        if pending:
            pending -= 1
            m = TIME_RE.search(line)
            if m:
                counts[(m.group(1).decode(), m.group(2)[:width].decode())] += 1
                pending = 0
                continue
            if not pending:
                stats["no_time"] += 1
        # Cheap prefix check first; exact match on the stripped line
        if line.startswith(title) and line.rstrip() == title:
            if pending:
                stats["no_time"] += 1
            stats["titles"] += 1
            pending = LOOKAHEAD
    if pending:
        stats["no_time"] += 1


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="+", help="log file(s); '-' for stdin")
    ap.add_argument("--title", default=DEFAULT_TITLE,
                    help=f"block title line to count (default: {DEFAULT_TITLE!r})")
    ap.add_argument("--by", choices=GRANULARITY, default="hour",
                    help="group counts per hour (default), minute or second")
    ap.add_argument("--csv", help="also write results to this CSV file")
    args = ap.parse_args()

    title = args.title.encode()
    counts = Counter()
    stats = Counter()
    width = GRANULARITY[args.by]
    suffix = {"hour": ":00", "minute": "", "second": ""}[args.by]

    for path in args.files:
        if path == "-":
            count_file(sys.stdin.buffer, title, counts, stats, width)
        else:
            with open(path, "rb", buffering=16 * 1024 * 1024) as fh:
                count_file(fh, title, counts, stats, width)

    rows = [(d, t + suffix, counts[(d, t)]) for d, t in sorted(counts)]
    label = args.by.capitalize()
    total = sum(counts.values())

    print(f"Title: {args.title}   (per {args.by})")
    print(f"{'Date':<12}{label:<10}{'Count':>10}")
    print("-" * 32)
    for d, t, c in rows:
        print(f"{d:<12}{t:<10}{c:>10}")
    print("-" * 32)
    print(f"{'Total':<22}{total:>10}")
    if rows:
        peak = max(rows, key=lambda r: r[2])
        print(f"{'Active ' + args.by + 's':<22}{len(rows):>10}")
        print(f"{'Average per ' + args.by:<22}{total / len(rows):>10.2f}")
        print(f"Peak: {peak[2]} at {peak[0]} {peak[1]}")
    if stats["no_time"]:
        print(f"WARNING: {stats['no_time']} title line(s) without a 'Time is:' line",
              file=sys.stderr)

    if args.csv:
        with open(args.csv, "w", newline="") as out:
            w = csv.writer(out)
            w.writerow(["date", args.by, "count"])
            w.writerows(rows)
        print(f"CSV written to {args.csv}")


if __name__ == "__main__":
    main()
