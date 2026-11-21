"""Failure benchmark against a REAL Invoice Ninja (local laboratory).

Reuses the agent and arms from bench_llm, but:
  - CRM and billing are the real adapters (vexinter/invoiceninja.py);
  - a wrapper injects each class's failure on the FIRST invoice creation
    (the effect, when there is one, actually happens on the instance);
  - the oracle reads the final state through the provider's API, bypassing
    the adapter.

Requirements: Invoice Ninja laboratory running and, in this session,
  $env:INVOICENINJA_URL, $env:INVOICENINJA_API_TOKEN and $env:OPENAI_API_KEY.

Usage:
  python -m examples.bench_real --n 1 --faults control,false_success --trace
  python -m examples.bench_real --n 10
"""
import argparse
import os
import sys
import uuid

import httpx

import examples.demo_llm as dl
from examples.bench_llm import (
    _TRACE,
    ARMS,
    COLS,
    _Agent,
    _finish,
    _WithRead,
    _Direct,
    _summary,
    print_table,
)
from examples.demo_coordinated import coordinated_condition
from vexinter import Vexinter
from vexinter.core.events import now
from vexinter.core.tools import IndeterminateOutcome, Observation, OperationResult
from vexinter.invoiceninja import (
    InvoiceNinjaBilling,
    InvoiceNinjaClient,
    InvoiceNinjaCrm,
)

URL = os.environ.get("INVOICENINJA_URL", "").rstrip("/")
TOKEN = os.environ.get("INVOICENINJA_API_TOKEN", "")
if not URL or not TOKEN:
    sys.exit("INVOICENINJA_URL and INVOICENINJA_API_TOKEN are missing from the environment.")
if not (URL.startswith(("http://localhost", "http://127.0.0.1"))):
    sys.exit("This benchmark only runs against a local laboratory instance.")

FAULTS = ("control", "false_success", "filtered_effect", "lost_response",
          "cut_before", "blind_honest", "blind_false_success")
TAG = uuid.uuid4().hex[:8]      # so two script runs do not collide


class InjectedFault:
    """Wraps the REAL invoice adapter and injects the class's failure on the
    first invoice creation (subsequent ones are real)."""
    system = "billing"

    def __init__(self, real, fault, confusing=False):
        self._real, self.fault, self._done_once = real, fault, False
        self._confusing = confusing

    def tools(self):
        t = dict(self._real.tools())
        if self._confusing:
            t["create_invoice"] = t["create_invoice"].model_copy(update={
                "description": "Creates and issues an invoice (two calls: "
                               "create and mark as sent). The instance "
                               "offers no idempotency."})
        return t

    def observe(self, query):
        if self.fault.startswith("blind"):
            return Observation(data=None, observed_at=now(),
                               source="billing/none", consistency="unknown")
        return self._real.observe(query)

    @staticmethod
    def _fake(args):
        return {"id": uuid.uuid4().hex[:10], "number": "9999",
                "customer_id": args.get("customer_id"),
                "amount": args.get("amount"), "status": "issued"}

    def invoke(self, tool, req):
        if tool != "create_invoice" or self.fault in ("control",
                                                      "blind_honest"):
            return self._real.invoke(tool, req)
        first = not self._done_once
        self._done_once = True
        if self.fault == "blind_false_success" or (first
                                                 and self.fault == "false_success"):
            return OperationResult(ok=True, data=self._fake(req.args))
        if not first:
            return self._real.invoke(tool, req)
        if self.fault == "cut_before":
            raise IndeterminateOutcome("cut before the effect (injected)")
        res = self._real.invoke(tool, req)          # the effect really happens
        if self.fault == "lost_response":
            raise IndeterminateOutcome("lost response (injected)")
        if self.fault == "filtered_effect":
            return OperationResult(ok=False, error="simulated timeout",
                                   data=res.data)
        return res


def _direct():
    """Own HTTP client, bypassing the adapter: the provider's route."""
    return httpx.Client(
        base_url=f"{URL}/api/v1", timeout=30.0,
        headers={"X-API-TOKEN": TOKEN, "X-Requested-With": "XMLHttpRequest",
                 "Accept": "application/json",
                 "Content-Type": "application/json"})


def real_oracle(email):
    """Truth read through the provider's API: active customer and EXACTLY one
    sent invoice (status_id 2), not archived or deleted."""
    with _direct() as d:
        r = d.get("/clients", params={"email": email, "per_page": 100})
        r.raise_for_status()
        customers = [c for c in r.json().get("data", [])
                    if any(k.get("email") == email
                           for k in (c.get("contacts") or []))
                    and not c.get("is_deleted") and not c.get("archived_at")]
        if not customers:
            return False, 0
        r = d.get("/invoices", params={"client_id": customers[0]["id"],
                                       "per_page": 100})
        r.raise_for_status()
        issued = [f for f in r.json().get("data", [])
                    if str(f.get("status_id")) == "2"
                    and not f.get("is_deleted") and not f.get("archived_at")]
    return len(issued) == 1, len(issued)


def run_real(llm, fault, arm, i, verify=False, approval="auto",
                  once=False, confusing=False):
    with_vexinter, with_read = ARMS[arm]
    email = f"real-{TAG}-{fault}-{arm}-{i}@example.com".replace("+", "_")
    cliente = InvoiceNinjaClient(URL, TOKEN)
    crm = InvoiceNinjaCrm(cliente)
    inner = InjectedFault(
        InvoiceNinjaBilling(cliente, approval=approval, once=once),
        fault, confusing)
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
    if with_vexinter:
        vex = Vexinter(embedded=True, adapters=[crm, billing])
        with vex.task(agent="llm_agent", objective=dl.SIGNUP_OBJECTIVE,
                      condition=coordinated_condition(),
                      input={"email": email}) as t:
            rep = llm.run(t, objective, context, allowed=allowed)
            agent = rep.done is True
            v = _finish(t, inner)
            claims = v.status == "met"
            escalated = len(t.pending_cases()) > 0
    else:
        rep = llm.run(_Direct([crm, billing]), objective, context,
                      allowed=allowed)
        agent = claims = rep.done is True
    achieved, issued = real_oracle(email)
    row = {"silenciosas": int(claims and not achieved),
            "subestima": int((not claims) and achieved),
            "duplicadas": int(issued > 1),
            "escaladas": int(escalated),
            "logradas": int(achieved),
            "errores": 0}
    line = (f"agent={int(agent)} claims={int(claims)} "
             f"achieved={int(achieved)} issued={issued} "
             f"escalated={int(escalated)}")
    trace = " | ".join(
        f"{t}({_summary(a)})->{'ok' if ok else 'ERR'}"
        for t, a, ok in _TRACE)
    return row, line, trace


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--n", type=int, default=10,
                    help="repetitions per class and arm")
    ap.add_argument("--faults", default=",".join(FAULTS))
    ap.add_argument("--model")
    ap.add_argument("--temperature", type=float, default=1.0)
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--trace", action="store_true")
    ap.add_argument("--approval", choices=("auto", "human"), default="auto")
    ap.add_argument("--once", action="store_true",
                    help="billing declares 'once per execution'")
    ap.add_argument("--confusing-description", action="store_true",
                    help="uses a create_invoice description that nudges the "
                         "agent into repeating the call")
    ap.add_argument("--max-input", type=int, default=1_500_000)
    ap.add_argument("--max-output", type=int, default=150_000)
    a = ap.parse_args()

    faults = [c.strip() for c in a.faults.split(",") if c.strip()]
    for c in faults:
        if c not in FAULTS:
            sys.exit(f"unknown class: {c}. Valid: {', '.join(FAULTS)}")
    if not os.environ.get("OPENAI_API_KEY"):
        sys.exit('Missing OPENAI_API_KEY: $env:OPENAI_API_KEY = "sk-..."')

    llm = _Agent(model=a.model, temperature=a.temperature)
    print(f"model={llm._model} temperature={a.temperature} "
          f"approval={a.approval} once={a.once} "
          f"confusing_description={a.confusing_description} "
          f"instance={URL} tag={TAG}\n")
    table = {(c, b): dict.fromkeys(COLS + ("n",), 0)
             for c in faults for b in ARMS}
    total = a.n * len(faults) * len(ARMS)
    k, cap_hit = 0, False
    for i in range(a.n):
        for fault in faults:
            for arm in ARMS:
                if (llm.usage["prompt"] >= a.max_input
                        or llm.usage["completion"] >= a.max_output):
                    cap_hit = True
                    break
                k += 1
                try:
                    row, line, trace = run_real(
                        llm, fault, arm, i, a.verify, a.approval,
                        a.once, a.confusing_description)
                except Exception as e:
                    row = dict.fromkeys(COLS, 0)
                    row["errores"] = 1
                    line = f"ERROR {type(e).__name__}: {str(e)[:160]}"
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
        print("TOKEN CAP REACHED: the run stopped before "
              "finishing.\n")
    print_table(table, faults)
    print(f"\ntokens: input={llm.usage['prompt']}, "
          f"output={llm.usage['completion']}")


if __name__ == "__main__":
    main()
