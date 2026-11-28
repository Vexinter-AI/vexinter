"""Control-path cost of a Vexinter task, measured without the model.

Builds the same setup as examples.bench_domains.run() for the inventory
domain (control and false_success fault classes, vexinter arm with and
without the read tool) and times each segment of the task lifecycle:

  vex_init   Vexinter() construction
  task_enter task context entry (runtime, event log, tracker)
  call       one governed tool dispatch through task.tools
             (policy, idempotency, event append, adapter dispatch)
  raw        the same call through _Direct (no governance, baseline)
  finish     _finish(): verify, plus the recover path on false_success
  task_exit  task context exit

Per-task product cost = vex_init + task_enter + task_exit + finish +
K * call, with K the observed agent-initiated tool calls per run. No
model is involved: LLM inference time is excluded by construction, so
the numbers isolate what Vexinter itself costs per task.

Usage:
  python -m examples.bench_overhead --n 300
"""

from __future__ import annotations

import argparse
import json
import statistics
import time

from examples.bench_domains import DOMAINS, WithRead
from examples.bench_llm import _Direct, _finish
from vexinter import Vexinter


def _pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))]


def _summ(name, xs):
    return (f"{name:<10} n={len(xs):<5} "
            f"median={statistics.median(xs) * 1000:8.2f} ms  "
            f"p95={_pct(xs, 0.95) * 1000:8.2f} ms  "
            f"p99={_pct(xs, 0.99) * 1000:8.2f} ms  "
            f"mean={statistics.mean(xs) * 1000:8.2f} ms")


def _calls_for(d, fault):
    """Agent-initiated calls per task, matching the latency-run traces:
    one restock for control, one (the retry happens inside recover)
    for false_success."""
    tool = d["first_tool"]
    if "inventory" in tool:
        return [(tool, {"quantity": 50, "sku": "SKU-WIDGET"})]
    return [(tool, {"email": "h0v0@x.co", "subject": "Cannot log in",
                    "priority": "high"})]


def _one_task(d, fault, with_read):
    """One full vexinter-arm lifecycle; returns segment seconds."""
    inner, wrapped, base = d["factory"](fault, approval="auto",
                                      probe_ok=True, once=False)
    adapter = WithRead(wrapped, d["read"]) if with_read else wrapped
    inp = {**base, **d["input_fn"](0, fault, "vexinter")}
    seg = {"calls": []}
    t_all = time.monotonic()
    vex = Vexinter(embedded=True, adapters=[adapter])
    seg["vex_init"] = time.monotonic() - t_all
    ctx = vex.task(
        agent="domain_agent",
        objective=d["objective"].format(
            **{k: v for k, v in inp.items() if isinstance(v, str)}),
        condition=d["condition"](inp), input=inp)
    t0 = time.monotonic()
    t = ctx.__enter__()
    seg["task_enter"] = time.monotonic() - t0
    for tool_key, args in _calls_for(d, fault):
        system, _, name = tool_key.partition(".")
        c0 = time.monotonic()
        getattr(getattr(t.tools, system), name)(args)
        seg["calls"].append(time.monotonic() - c0)
    f0 = time.monotonic()
    _finish(t, wrapped, failed_tool=d["failed_tool"])
    seg["finish"] = time.monotonic() - f0
    b0 = time.monotonic()
    ctx.__exit__(None, None, None)
    seg["task_exit"] = time.monotonic() - b0
    seg["total"] = (seg["vex_init"] + seg["task_enter"] + seg["task_exit"]
                    + seg["finish"] + sum(seg["calls"]))
    return seg


def _one_raw(d, fault, with_read):
    """One governed-equivalent call with no Vexinter at all."""
    inner, wrapped, base = d["factory"](fault, approval="auto",
                                      probe_ok=True, once=False)
    adapter = WithRead(wrapped, d["read"]) if with_read else wrapped
    direct = _Direct([adapter])
    tool_key, args = _calls_for(d, fault)[0]
    t0 = time.monotonic()
    direct.call(tool_key, args)
    return time.monotonic() - t0


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--n", type=int, default=300,
                    help="task lifecycles per configuration")
    ap.add_argument("--raw-n", type=int, default=1000,
                    help="raw dispatches for the no-governance baseline")
    ap.add_argument("--domain", default="inventory",
                    choices=sorted(DOMAINS))
    ap.add_argument("--faults", default="control,false_success")
    ap.add_argument("--json", action="store_true",
                    help="emit raw samples as JSON")
    a = ap.parse_args()

    d = DOMAINS[a.domain]
    faults = [f.strip() for f in a.faults.split(",") if f.strip()]
    arms = [("vexinter", False), ("vexinter+read", True)]
    out = {"domain": a.domain, "n": a.n, "configs": {}}

    for fault in faults:
        for arm, with_read in arms:
            segs = {k: [] for k in
                    ("vex_init", "task_enter", "finish", "task_exit",
                     "total")}
            calls, raw = [], []
            for _ in range(a.n):
                s = _one_task(d, fault, with_read)
                for k in segs:
                    segs[k].append(s[k])
                calls.extend(s["calls"])
            for _ in range(a.raw_n):
                raw.append(_one_raw(d, fault, with_read))
            key = f"{fault}/{arm}"
            out["configs"][key] = {
                "segments": {k: {"median_ms": round(
                    statistics.median(v) * 1000, 3),
                    "p95_ms": round(_pct(v, 0.95) * 1000, 3),
                    "p99_ms": round(_pct(v, 0.99) * 1000, 3),
                    "mean_ms": round(statistics.mean(v) * 1000, 3)}
                    for k, v in segs.items()},
                "call": {"median_ms": round(
                    statistics.median(calls) * 1000, 3),
                    "p95_ms": round(_pct(calls, 0.95) * 1000, 3),
                    "p99_ms": round(_pct(calls, 0.99) * 1000, 3),
                    "mean_ms": round(statistics.mean(calls) * 1000, 3),
                    "n": len(calls)},
                "raw_call": {"median_ms": round(
                    statistics.median(raw) * 1000, 3),
                    "p95_ms": round(_pct(raw, 0.95) * 1000, 3),
                    "p99_ms": round(_pct(raw, 0.99) * 1000, 3),
                    "mean_ms": round(statistics.mean(raw) * 1000, 3),
                    "n": len(raw)},
            }
            c = out["configs"][key]
            print(f"[{key}] n={a.n}")
            for k in ("vex_init", "task_enter", "call", "finish",
                      "task_exit", "total"):
                if k == "call":
                    v = c["call"]
                    print(f"  call       median={v['median_ms']:8.2f} ms "
                          f"p95={v['p95_ms']:8.2f} ms "
                          f"(n={v['n']})")
                else:
                    v = c["segments"][k]
                    print(f"  {k:<10} median={v['median_ms']:8.2f} ms "
                          f"p95={v['p95_ms']:8.2f} ms")
            v = c["raw_call"]
            print(f"  raw_call   median={v['median_ms']:8.2f} ms "
                  f"p95={v['p95_ms']:8.2f} ms (n={v['n']})")
            print()

    if a.json:
        print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
