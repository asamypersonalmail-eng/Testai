#!/usr/bin/env python3
"""Count log blocks by hour for a given block title (streams, safe for multi-GB files).

Expected block layout:

    ========================================
    MDP  getCreditCards Auth Request
    Time is: 2026-09-25 00:00:25.906066
    Message is ( ...

Usage:
    python3 count_requests_by_hour.py /path/to/big.log
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
# Captures "YYYY-MM-DD HH" from the "Time is:" line
TIME_RE = re.compile(rb"Time is:\s*(\d{4}-\d{2}-\d{2})[ T](\d{2})")
# How many lines after the title to look for the "Time is:" line
LOOKAHEAD = 3


def count_file(fh, title, counts, stats):
    """Stream one binary file handle line by line and update counts."""
    pending = 0  # >0 while waiting for a "Time is:" line after a title match
    for line in fh:
        if pending:
            pending -= 1
            m = TIME_RE.search(line)
            if m:
                counts[(m.group(1).decode(), m.group(2).decode())] += 1
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
    ap.add_argument("--csv", help="also write results to this CSV file")
    args = ap.parse_args()

    title = args.title.encode()
    counts = Counter()
    stats = Counter()

    for path in args.files:
        if path == "-":
            count_file(sys.stdin.buffer, title, counts, stats)
        else:
            with open(path, "rb", buffering=16 * 1024 * 1024) as fh:
                count_file(fh, title, counts, stats)

    rows = [(d, f"{h}:00", counts[(d, h)]) for d, h in sorted(counts)]

    print(f"Title: {args.title}")
    print(f"{'Date':<12}{'Hour':<7}{'Count':>10}")
    print("-" * 29)
    for d, h, c in rows:
        print(f"{d:<12}{h:<7}{c:>10}")
    print("-" * 29)
    print(f"{'Total':<19}{sum(counts.values()):>10}")
    if stats["no_time"]:
        print(f"WARNING: {stats['no_time']} title line(s) without a 'Time is:' line",
              file=sys.stderr)

    if args.csv:
        with open(args.csv, "w", newline="") as out:
            w = csv.writer(out)
            w.writerow(["date", "hour", "count"])
            w.writerows(rows)
        print(f"CSV written to {args.csv}")


if __name__ == "__main__":
    main()
