"""Runs the BENCHMARK.md §8 re-verification snippet against a committed
sample trace, so the documented procedure cannot silently rot."""
import json
from pathlib import Path

from vexinter.core.crypto import GENESIS
from vexinter.core.events import Event

ROOT = Path(__file__).resolve().parent.parent
TRACE = (ROOT / "bench-results" / "traces" / "inventory"
         / "inventory-nemlx-main" / "control-vexinter-0.jsonl")


def test_committed_trace_reverifies():
    prevs = {}                       # chain links are per-stream
    count = 0
    for line in TRACE.open():
        d = json.loads(line)
        if "type" not in d:
            continue                 # _meta header / _verify trailer
        e = Event(**d).seal()         # recompute this_hash
        assert e.this_hash == d["this_hash"]
        assert e.prev_hash == prevs.get(e.stream, GENESIS)
        prevs[e.stream] = e.this_hash
        count += 1
    assert count > 0
