"""Arm with a real model from the failure benchmark.

A real agent (OpenAI, tool calling) executes the task against the same
simulated systems and the same failure classes as examples/bench_fallos.py.
Four arms, with and without Vexinter and with and without a read tool that
lets the agent self-verify:

  solo          agent -> systems, no registry, no keys, no verification
  solo+lee      same, with billing.list_invoices (read-only)
  vexinter      agent -> Vexinter; then verify / recover / reviews
  vexinter+lee  same, with billing.list_invoices

The TRUTH (oracle) comes from the simulators' internal state, not from the
verifier. Each arm is an independent run of the agent.

VALIDITY RULE: in the 'control' class (no failures) all arms must achieve
the objective in every run. If not, the agent is misconfigured and the rest
of the table cannot be interpreted.

Requirements:
  pip install -e ".[llm]"
  $env:OPENAI_API_KEY = "sk-..."        (never in the code)

Usage:
  python -m examples.bench_llm --faults control --n 2 --trace
  python -m examples.bench_llm --n 5
"""
import argparse
import json
import os
import re
import sys
import time
from datetime import timedelta
from pathlib import Path

import examples.demo_llm as dl
from examples.bench_fallos import FAULTS as _FAULTS_BASE
from examples.bench_fallos import oracle
from examples.demo_coordinated import coordinated_condition
from vexinter import Vexinter
from vexinter.core.events import now
from vexinter.core.store import EventQuery
from vexinter.core.tools import (
    IndeterminateOutcome,
    OperationClass,
    OperationRequest,
    OperationResult,
    ToolSpec,
)
from vexinter.demo import BillingSystem, CrmSystem


class _LostResponseNoIdem(BillingSystem):
    """No idempotency: the effect happens and the response is lost (once)."""

    def __init__(self):
        super().__init__(supports_idempotency=False, probeable=True)
        self._done_once = False

    def invoke(self, tool, req):
        res = super().invoke(tool, req)
        if tool == "create_invoice" and not self._done_once:
            self._done_once = True
            raise IndeterminateOutcome("lost response (simulated)")
        return res


class _CutBeforeNoIdem(BillingSystem):
    """No idempotency: cut BEFORE the effect (once); nothing is persisted."""

    def __init__(self):
        super().__init__(supports_idempotency=False, probeable=True)
        self._done_once = False

    def invoke(self, tool, req):
        if tool == "create_invoice" and not self._done_once:
            self._done_once = True
            raise IndeterminateOutcome("cut before the effect (simulated)")
        return super().invoke(tool, req)


class _AmbiguousResponse(BillingSystem):
    """The write DOES happen and returns ok=True, but the response does not say
    what was created and is left 'pending confirmation' (like an async API): a
    prudent agent may repeat the call to confirm."""

    def invoke(self, tool, req):
        res = super().invoke(tool, req)
        if tool == "create_invoice" and res.ok:
            return OperationResult(ok=True, data={
                "received": True, "status": "pending confirmation"})
        return res


FAULTS = {**_FAULTS_BASE,
          "lost_response_no_idem": _LostResponseNoIdem,
          "cut_before_no_idem": _CutBeforeNoIdem,
          "ambiguous_response": _AmbiguousResponse}

ARMS = {
    "solo": (False, False),
    "solo+read": (False, True),
    "vexinter": (True, False),
    "vexinter+read": (True, True),
}
COLS = ("silent", "understates", "duplicates", "escalated", "achieved",
        "errors")

_TRACE = []        # (tool, arguments, ok) of the current run


class _R:
    def __init__(self, ok, data, error):
        self.ok, self.data, self.error = ok, data, error


class _Direct:
    """Without Vexinter: the agent calls the systems as-is, with no
    registry, no idempotency keys and no verification."""

    def __init__(self, adapters):
        self._by = {a.system: a for a in adapters}
        self.available_tools = {f"{a.system}.{n}": s
                                for a in adapters
                                for n, s in a.tools().items()}

    def call(self, tool_key, args):
        system, _, name = tool_key.partition(".")
        return self._by[system].invoke(
            name, OperationRequest(tool=tool_key, args=args))


class _WithRead:
    """Also exposes a READ-ONLY tool so the agent can self-verify. Delegates
    everything else to the real system."""
    system = "billing"

    def __init__(self, inner):
        self._in = inner

    def tools(self):
        t = dict(self._in.tools())
        t["list_invoices"] = ToolSpec(
            system="billing", name="list_invoices",
            description="Lists a customer's invoices (read-only).",
            input_schema={"type": "object",
                          "properties": {"customer_id": {"type": "string"}},
                          "required": ["customer_id"]},
            operation_class=OperationClass.READ)
        return t

    def invoke(self, tool, req):
        if tool == "list_invoices":
            obs = self._in.observe({
                "type": "invoice", "many": True,
                "where": {"customer_id": req.args["customer_id"]}})
            return OperationResult(ok=True, data={
                "invoices": obs.data or [], "read": obs.consistency})
        return self._in.invoke(tool, req)

    def observe(self, query):
        return self._in.observe(query)


# -- demo_llm's agent calls _dispatch(); we wrap it so a network cut or an
#    error reaches the model as a tool result (same in all four arms), to
#    strip the simulator's '_simulated' marker (it would be a hint) and to
#    keep a trace.
_dispatch_original = dl._dispatch


def _clean(data):
    if isinstance(data, dict):
        return {k: v for k, v in data.items() if k != "_simulated"}
    return data


def _dispatch(task, tool_key, args):
    try:
        if isinstance(task, _Direct):
            res = task.call(tool_key, args)
        else:
            res = _dispatch_original(task, tool_key, args)
        out = _R(res.ok, _clean(res.data), res.error)
    except IndeterminateOutcome:
        out = _R(False, None, "there was no definitive response from the "
                              "tool: it is unknown whether the operation "
                              "was performed")
    except Exception as e:
        out = _R(False, None, f"{type(e).__name__}: {e}")
    seen = out.error if out.error else out.data
    base = dict(args) if isinstance(args, dict) else {}
    _TRACE.append((tool_key, {**base, "ret": str(seen)}, out.ok))
    return out


dl._dispatch = _dispatch


def _summary(args):
    if not isinstance(args, dict):
        return str(args)[:40]
    parts = []
    for k, v in args.items():
        txt = str(v)[:(110 if k == "ret" else 26)]
        parts.append(f"{k}={txt}".encode("ascii", "replace").decode())
    return ",".join(parts)


class _Agent(dl.OpenAIToolAgent):
    """Same as demo_llm's agent, with configurable temperature (at
    temperature 0 the repetitions would be nearly identical) and with
    parallel calls off by default: the prompt asks to create the customer
    BEFORE invoicing, and in parallel the model invents the identifier."""

    def __init__(self, model=None, temperature=1.0, parallel=False,
                 effort=None, base_url=None):
        super().__init__(model=model, base_url=base_url)
        original = self._client.chat.completions.create

        def create(**kw):
            kw["temperature"] = temperature
            if not parallel:
                kw["parallel_tool_calls"] = False
            if effort:
                kw["reasoning_effort"] = effort
            return original(**kw)

        self._client.chat.completions.create = create
        if self._client.chat.completions.create is not create:
            raise RuntimeError(
                "could not set the temperature (incompatible openai "
                "version): stopping before spending")


def _finish(t, inner, failed_tool="billing.create_invoice"):
    """What whoever integrates Vexinter does after the agent finishes."""
    v = t.verify()
    if v.status == "not_met":
        t.recover(failed_tool=failed_tool)
        v = t.verify()
    elif v.status == "inconclusive":
        if hasattr(inner, "tick"):
            inner.tick()               # simulator resource (see limits)
        for k in range(1, 4):
            r = t.run_followups(at=now() + timedelta(seconds=61 * k))
            if r:
                v = r[-1]
                if v.status != "inconclusive":
                    break
    return v


def _dump_trace(t, meta: dict, rep: int) -> None:
    """Serializes the run's full event chain to an immutable JSONL trace
    under bench-results/trazas/billing/ before the embedded store is
    discarded."""
    try:
        store = t._rt.store
        events = store.query(EventQuery(limit=100000))
        audit = store.verify_chain()
        cfg = meta.get("config") or "default"
        domain = meta.get("domain") or "billing"
        path = (Path("bench-results") / "trazas" / domain / cfg
                / f"{meta['fault']}-{meta['arm']}-{rep}.jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            f.write(json.dumps({"_meta": meta}) + "\n")
            for ev in sorted(events, key=lambda e: (e.stream, e.seq)):
                f.write(json.dumps(ev.model_dump(mode="json"),
                                   default=str) + "\n")
            f.write(json.dumps({"_verify": audit.model_dump()}) + "\n")
    except Exception:
        pass


def run(llm, fault, arm, i, verify=False, approval="auto",
             probe="correct", once=False, config=None):
    with_vexinter, with_read = ARMS[arm]
    email = f"llm-{fault}-{arm}-{i}@example.com".replace("+", "_")
    crm, inner = CrmSystem(), FAULTS[fault]()
    inner._approval = approval
    inner._probe_ok = (probe == "correct")
    inner._once = once
    billing = _WithRead(inner) if with_read else inner
    allowed = set(dl.AGENT_TOOLS)
    if with_read:
        allowed.add("billing.list_invoices")
    objective = dl.SIGNUP_OBJECTIVE
    if verify and with_read:
        objective += (" Before considering the task finished, use the invoice "
                     "read tool to check that the invoice actually exists.")
    context = {"email": email, "name": "Ana"}
    _TRACE.clear()
    escalated = False
    t0 = time.monotonic()
    if with_vexinter:
        vex = Vexinter(embedded=True, adapters=[crm, billing])
        with vex.task(agent="llm_agent", objective=dl.SIGNUP_OBJECTIVE,
                      condition=coordinated_condition(),
                      input={"email": email}) as t:
            rep = llm.run(t, objective, context, allowed=allowed)
            agent = rep.done is True
            v = _finish(t, inner)
            claims = v.status == "met"       # what is reported to the operator
            escalated = len(t.pending_cases()) > 0
        _dump_trace(t, {"domain": "billing", "fault": fault,
                        "arm": arm, "rep": i, "config": config,
                        "wall_s": round(time.monotonic() - t0, 2),
                        "model": getattr(llm, "_model", None)}, i)
    else:
        rep = llm.run(_Direct([crm, billing]), objective, context,
                      allowed=allowed)
        agent = claims = rep.done is True      # what the agent claims
    achieved, issued = oracle(crm, inner, email)
    row = {"silent": int(claims and not achieved),
            "understates": int((not claims) and achieved),
            "duplicates": int(issued > 1),
            "escalated": int(escalated),
            "achieved": int(achieved),
            "errors": 0}
    line = (f"agent={int(agent)} claims={int(claims)} "
             f"achieved={int(achieved)} issued={issued} "
             f"escalated={int(escalated)}")
    trace = " | ".join(
        f"{t}({_summary(a)})->{'ok' if ok else 'ERR'}"
        for t, a, ok in _TRACE)
    return row, line, trace


def print_table(table, faults):
    print(f"{'class':<18}{'arm':<14}{'n':>4}"
          + "".join(f"{c:>13}" for c in COLS))
    totals = {b: dict.fromkeys(COLS + ("n",), 0) for b in ARMS}
    for fault in faults:
        for arm in ARMS:
            f = table[(fault, arm)]
            for c in COLS + ("n",):
                totals[arm][c] += f[c]
            print(f"{fault:<18}{arm:<14}{f['n']:>4}"
                  + "".join(f"{f[c]:>13}" for c in COLS))
        print()
    print("TOTAL")
    for arm in ARMS:
        f = totals[arm]
        print(f"{'':<18}{arm:<14}{f['n']:>4}"
              + "".join(f"{f[c]:>13}" for c in COLS))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--n", type=int, default=5,
                    help="repetitions per class and arm")
    ap.add_argument("--faults", default=",".join(FAULTS),
                    help="comma-separated list")
    ap.add_argument("--model", help="default: gpt-4o-mini "
                    "(or VEXINTER_LLM_MODEL)")
    ap.add_argument("--temperature", type=float, default=1.0)
    ap.add_argument("--effort", choices=("minimal", "low", "medium", "high"),
                    help="reasoning_effort, only for reasoning models")
    ap.add_argument("--parallel", action="store_true",
                    help="allow parallel tool calls "
                         "(reproduces the setup that failed)")
    ap.add_argument("--trace", action="store_true",
                    help="print each call with its arguments")
    ap.add_argument("--config", help="config label for trace filenames "
                    "(default: model name)")
    ap.add_argument("--resume-log", default=None,
                    help="existing run log: skip completed runs "
                         "(ERROR lines are retried, not skipped)")
    ap.add_argument("--verify", action="store_true",
                    help="the +lee arms are asked to check with the "
                         "read tool before considering the task "
                         "finished")
    ap.add_argument("--approval", choices=("auto", "human"), default="auto",
                    help="retry policy for writes without "
                         "idempotency: 'human' requires human approval")
    ap.add_argument("--probe", choices=("correct", "defective"),
                    default="correct",
                    help="'defectuoso' simulates a badly defined effect probe "
                         "(counts something that never changes)")
    ap.add_argument("--once", action="store_true",
                    help="billing declares 'once per execution': an "
                         "identical retry after ok=True returns what was "
                         "already recorded")
    ap.add_argument("--max-input", type=int, default=1_500_000,
                    help="hard cap on input tokens")
    ap.add_argument("--max-output", type=int, default=150_000,
                    help="hard cap on output tokens")
    ap.add_argument("--base-url",
                    help="OpenAI-compatible endpoint (e.g. LM Studio "
                         "http://localhost:1234/v1); also OPENAI_BASE_URL")
    a = ap.parse_args()

    faults = [c.strip() for c in a.faults.split(",") if c.strip()]
    for c in faults:
        if c not in FAULTS:
            sys.exit(f"unknown class: {c}. Valid: {', '.join(FAULTS)}")
    base_url = a.base_url or os.environ.get("OPENAI_BASE_URL")
    if not os.environ.get("OPENAI_API_KEY") and not base_url:
        sys.exit('Missing OPENAI_API_KEY: $env:OPENAI_API_KEY = "sk-..." '
                 '(or set OPENAI_BASE_URL / --base-url for a local server)')

    llm = _Agent(model=a.model, temperature=a.temperature,
                  parallel=a.parallel, effort=a.effort,
                  base_url=base_url)
    print(f"model={llm._model} temperature={a.temperature} "
          f"parallel_calls={a.parallel} approval={a.approval} "
          f"probe={a.probe} once={a.once} effort={a.effort}\n")
    table = {(c, b): dict.fromkeys(COLS + ("n",), 0)
             for c in faults for b in ARMS}
    total = a.n * len(faults) * len(ARMS)
    skip = 0
    if a.resume_log and os.path.exists(a.resume_log):
        done = 0
        with open(a.resume_log, errors="replace") as fh:
            for ln in fh:
                if not re.match(r"^\[\d+/\d+\]", ln) or " ERROR" in ln:
                    continue
                done += 1
                # fault and arm are printed as fixed-width columns but a
                # fault name >=18 chars leaves no separating space; parse
                # from the right using the known arm names instead
                body = ln.split("]", 1)[1]
                head, _, metrics = body.partition("agent=")
                head = head.strip()
                fault_p = arm_p = None
                for arm_name in sorted(ARMS, key=len, reverse=True):
                    if head.endswith(arm_name):
                        arm_p = arm_name
                        fault_p = head[: -len(arm_name)].strip()
                        break
                if not fault_p or (fault_p, arm_p) not in table:
                    continue
                kv = dict(p.split("=", 1) for p in metrics.split()
                          if "=" in p)
                try:
                    claims = int(kv.get("claims", 0))
                    achieved = int(kv.get("achieved", 0))
                    issued = int(kv.get("issued", 0))
                    esc = int(kv.get("escalated", 0))
                except ValueError:
                    continue
                cell = table[(fault_p, arm_p)]
                cell["silent"] += int(claims and not achieved)
                cell["understates"] += int((not claims) and achieved)
                cell["duplicates"] += int(issued > 1)
                cell["escalated"] += esc
                cell["achieved"] += achieved
                cell["n"] += 1
        skip = min(done, total)
        if skip:
            print(f"resuming: skipping first {skip}/{total} runs "
                  f"from {a.resume_log}")
    k, cap_hit = 0, False
    pos = 0
    for i in range(a.n):
        for fault in faults:
            for arm in ARMS:
                pos += 1
                if pos <= skip:
                    continue
                if (llm.usage["prompt"] >= a.max_input
                        or llm.usage["completion"] >= a.max_output):
                    cap_hit = True
                    break
                k += 1
                try:
                    row, line, trace = run(llm, fault, arm, i,
                                                  a.verify, a.approval,
                                                  a.probe, a.once,
                                                  config=a.config or llm._model)
                except Exception as e:
                    row = dict.fromkeys(COLS, 0)
                    row["errors"] = 1
                    line = f"ERROR {type(e).__name__}: {str(e)[:120]}"
                    trace = ""
                for c in COLS:
                    table[(fault, arm)][c] += row[c]
                table[(fault, arm)]["n"] += 1
                print(f"[{k}/{total}] {fault:<18}{arm:<14}{line}"
                      f"  | tokens {llm.usage['prompt']}"
                      f"+{llm.usage['completion']}", flush=True)
                if a.trace and trace:
                    print(f"      {trace}", flush=True)
            if cap_hit:
                break
        if cap_hit:
            break

    print()
    if cap_hit:
        print("TOKEN CAP REACHED: the run stopped before finishing "
              "(the n column shows how many repetitions were "
              "completed per cell).\n")
    print_table(table, faults)
    print(f"\ntokens: input={llm.usage['prompt']}, "
          f"output={llm.usage['completion']}")
    rates = {"gpt-4o-mini": (0.15, 0.60), "gpt-4.1-mini": (0.40, 1.60),
               "gpt-5-mini": (0.25, 2.00)}
    rate = next((v for k, v in rates.items()
                   if llm._model.startswith(k)), None)
    if rate:
        cost = (llm.usage["prompt"] * rate[0]
                 + llm.usage["completion"] * rate[1]) / 1_000_000
        print(f"estimated cost: ~US$ {cost:.2f} (list price; "
              "confirm it on your usage dashboard)")


if __name__ == "__main__":
    main()
