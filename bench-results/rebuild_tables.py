#!/usr/bin/env python3
"""Rebuild benchmark TOTAL tables from per-run log lines.

Configs that resumed mid-run have complete [k/N] lines but a final
table that only counts the last process's rows. This re-aggregates
every run line (good and ERROR) and appends a REBUILT TOTALS section.

Usage: rebuild_tables.py bench-results/*.log
"""
import re
import sys
from collections import defaultdict
from pathlib import Path

COLS = ("silent", "understates", "duplicates", "escalated", "achieved",
        "errors")
ARMS = ("solo", "solo+read", "vexinter", "vexinter+read")


def rebuild(path: Path) -> str | None:
    cells = defaultdict(lambda: dict.fromkeys(COLS + ("n",), 0))
    nlines = 0
    for ln in path.read_text(errors="replace").splitlines():
        if not re.match(r"^\[\d+/\d+\]", ln):
            continue
        nlines += 1
        parts = ln.split("]", 1)[1].split()
        if len(parts) < 3:
            continue
        fault, arm = parts[0], parts[1]
        cell = cells[(fault, arm)]
        cell["n"] += 1
        if " ERROR" in ln:
            cell["errors"] += 1
            continue
        kv = dict(p.split("=", 1) for p in parts[2:] if "=" in p)
        try:
            claims = int(kv.get("claims", 0))
            achieved = int(kv.get("achieved", 0))
            issued = int(kv.get("writes", kv.get("issued", 0)))
            esc = int(kv.get("escalated", 0))
        except ValueError:
            continue
        cell["silent"] += int(claims and not achieved)
        cell["understates"] += int((not claims) and achieved)
        cell["duplicates"] += int(issued > 1)
        cell["escalated"] += esc
        cell["achieved"] += achieved
    if not cells:
        return None

    faults = sorted({f for f, _ in cells})
    out = ["", "REBUILT TOTALS (aggregated from per-run lines)", ""]
    out.append(f"{'class':<18}{'arm':<14}{'n':>4}"
               + "".join(f"{c:>13}" for c in COLS))
    totals = {b: dict.fromkeys(COLS + ("n",), 0) for b in ARMS}
    for fault in faults:
        for arm in ARMS:
            f = cells.get((fault, arm))
            if not f:
                continue
            for c in COLS + ("n",):
                totals[arm][c] += f[c]
            out.append(f"{fault:<18}{arm:<14}{f['n']:>4}"
                       + "".join(f"{f[c]:>13}" for c in COLS))
        out.append("")
    out.append("TOTAL")
    for arm in ARMS:
        f = totals[arm]
        if not f["n"]:
            continue
        out.append(f"{'':<18}{arm:<14}{f['n']:>4}"
                   + "".join(f"{f[c]:>13}" for c in COLS))
    return "\n".join(out) + f"\n# run-lines counted: {nlines}\n"


def main():
    for arg in sys.argv[1:]:
        p = Path(arg)
        section = rebuild(p)
        if section is None:
            print(f"{p}: no run lines, skipped")
            continue
        with p.open("a") as fh:
            fh.write(section)
        print(f"{p}: rebuilt totals appended")


if __name__ == "__main__":
    main()
