#!/usr/bin/env python3
"""Build the benchmark trace manifest and its commitment hash.

Reads every bench-results/trazas/**/*.jsonl, extracts the head hash of each
event stream (last event's this_hash per stream), writes MANIFEST.txt with
one "<relpath> <stream> <seq> <head_hash>" line per stream, then prints the
SHA-256 of the manifest — the value to publish alongside BENCHMARK.md.

Run after the benchmark completes:
    .venv/bin/python bench-results/build_manifest.py
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TRAZAS = ROOT / "trazas"
MANIFEST = ROOT / "MANIFEST.txt"


def main() -> int:
    if not TRAZAS.is_dir():
        sys.exit("no bench-results/trazas/ directory")
    lines = []
    files = 0
    for path in sorted(TRAZAS.rglob("*.jsonl")):
        heads: dict[str, tuple[int, str]] = {}
        ok_chain = None
        for raw in path.read_text().splitlines():
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if rec.get("_verify") is not None:
                ok_chain = rec["_verify"].get("ok")
                continue
            if rec.get("_meta"):
                continue
            stream = rec.get("stream")
            if stream:
                heads[stream] = (rec.get("seq", 0), rec.get("this_hash", ""))
        rel = path.relative_to(ROOT)
        files += 1
        for stream, (seq, h) in sorted(heads.items()):
            flag = "" if ok_chain else " CHAIN_FAIL"
            lines.append(f"{rel}\t{stream}\t{seq}\t{h}{flag}")
    MANIFEST.write_text("\n".join(lines) + "\n")
    digest = hashlib.sha256(MANIFEST.read_bytes()).hexdigest()
    print(f"files={files} streams={len(lines)}")
    print(f"manifest={MANIFEST}")
    print(f"sha256={digest}")
    fails = [l for l in lines if l.endswith("CHAIN_FAIL")]
    if fails:
        print(f"WARNING: {len(fails)} streams failed chain verification")
    return 0


if __name__ == "__main__":
    sys.exit(main())
