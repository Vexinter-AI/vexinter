"""Failure benchmark: what happens with each class of failure
depending on the agent's policy. All simulated, zero cost.

The TRUTH (oracle) is read from the simulated systems' internal state, not
from the verifier: so Vexinter's verdict is checked against something
independent of it.

Columns:
  silenciosas : the agent claims success but the objective was NOT achieved
  subestima   : the agent does not claim success although the objective WAS achieved
  duplicadas  : there is more than one issued invoice (repeated effect)
  escaladas   : the case was left waiting for a person
  logradas    : the objective was truly achieved (per the oracle)

Usage:  python -m examples.bench_failures [--n 1]
"""
import argparse
from datetime import timedelta

from examples.demo_coordinated import coordinated_condition
from vexinter import Vexinter
from vexinter.core.events import now
from vexinter.core.tools import IndeterminateOutcome, Observation, OperationRequest
from vexinter.demo import BillingSystem, CrmSystem


class _LostResponse(BillingSystem):
    """The effect happens and the response is lost (only the first time)."""

    def __init__(self):
        super().__init__()
        self._done_once = False

    def invoke(self, tool, req):
        res = super().invoke(tool, req)
        if tool == "create_invoice" and not self._done_once:
            self._done_once = True
            raise IndeterminateOutcome("lost response (simulated)")
        return res


class _CutBefore(BillingSystem):
    """Cut BEFORE the effect: nothing is persisted (only the first time)."""

    def __init__(self):
        super().__init__()
        self._done_once = False

    def invoke(self, tool, req):
        if tool == "create_invoice" and not self._done_once:
            self._done_once = True
            raise IndeterminateOutcome("cut before the effect (simulated)")
        return super().invoke(tool, req)


class _Blind(BillingSystem):
    """Cannot be observed: its read declares 'unknown', now and
    later. Writes follow BillingSystem's behavior."""

    def observe(self, query):
        return Observation(data=None, observed_at=now(),
                           source="billing/none", consistency="unknown")


FAULTS = {
    "control": lambda: BillingSystem(),
    "false_success": lambda: BillingSystem(fail_first=1),
    "filtered_effect": lambda: BillingSystem(
        fail_first=1, leak_on_fail=True, supports_idempotency=False,
        probeable=True),
    "lost_response": _LostResponse,
    "cut_before": _CutBefore,
    "stale_replica": lambda: BillingSystem(read_consistency="eventual"),
    "blind_honest": lambda: _Blind(),
    "blind_false_success": lambda: _Blind(fail_first=10**6),
}
ARMS = ("trust", "retry", "reread", "reread_wait",
          "reread_no_retry", "vexinter")
COLS = ("silent", "understates", "duplicates", "escalated", "achieved")


def oracle(crm, billing, email):
    """Scenario truth: active customer and EXACTLY one issued invoice.
    Reads the systems' primary state, not the adapter's read."""
    customer = crm.observe({"type": "customer", "where": {"email": email}}).data
    if not customer or customer["status"] != "active":
        return False, 0
    issued = billing.find({"customer_id": customer["id"], "status": "issued"})
    return len(issued) == 1, len(issued)


def _create_customer(crm, email):
    r = crm.invoke("create_customer", OperationRequest(
        tool="crm.create_customer", args={"name": "Ana", "email": email}))
    return r.data["id"] if r.ok else None


def _issue(billing, cid):
    """A direct call, without an idempotency key. Returns ok, or None if
    there was no definitive response."""
    try:
        return billing.invoke("create_invoice", OperationRequest(
            tool="billing.create_invoice",
            args={"customer_id": cid, "amount": 100})).ok
    except IndeterminateOutcome:
        return None


def _visible(billing, cid):
    obs = billing.observe({"type": "invoice", "where": {
        "customer_id": cid, "status": "issued"}})
    return obs.data is not None


def arm_trust(crm, billing, email):
    cid = _create_customer(crm, email)
    return cid is not None and _issue(billing, cid) is True, False


def arm_retry(crm, billing, email, max_retries=2):
    cid = _create_customer(crm, email)
    if cid is None:
        return False, False
    for _ in range(1 + max_retries):
        if _issue(billing, cid) is True:
            return True, False
    return False, False


def arm_reread(crm, billing, email):
    """Naive self-verification: if it doesn't see it, rewrite it."""
    cid = _create_customer(crm, email)
    if cid is None:
        return False, False
    _issue(billing, cid)
    if not _visible(billing, cid):
        _issue(billing, cid)
    return _visible(billing, cid), False


def arm_reread_wait(crm, billing, email):
    """If it doesn't see it, WAIT (the simulator lets time pass with tick())
    and re-read before rewriting. NOTE: tick() is a simulator resource; a
    real agent cannot force a replica to catch up."""
    cid = _create_customer(crm, email)
    if cid is None:
        return False, False
    _issue(billing, cid)
    if not _visible(billing, cid):
        if hasattr(billing, "tick"):
            billing.tick()
        if not _visible(billing, cid):
            _issue(billing, cid)
    return _visible(billing, cid), False


def arm_reread_no_retry(crm, billing, email):
    """Prudent self-verification: does not write again if it cannot confirm;
    reports that it could not."""
    cid = _create_customer(crm, email)
    if cid is None:
        return False, False
    _issue(billing, cid)
    return _visible(billing, cid), False


def arm_vexinter(crm, billing, email):
    vex = Vexinter(embedded=True, adapters=[crm, billing])
    with vex.task(agent="agent", objective="signup and invoice",
                  condition=coordinated_condition(),
                  input={"email": email}) as t:
        c = t.tools.crm.create_customer({"name": "Ana", "email": email})
        try:
            t.tools.billing.create_invoice({"customer_id": c.id, "amount": 100})
        except IndeterminateOutcome:
            pass
        v = t.verify()
        if v.status == "not_met":
            t.recover(failed_tool="billing.create_invoice")
            v = t.verify()
        elif v.status == "inconclusive":
            if hasattr(billing, "tick"):
                billing.tick()
            for k in range(1, 4):       # 3 reviews: exhaust the attempts
                r = t.run_followups(at=now() + timedelta(seconds=61 * k))
                if r:
                    v = r[-1]
                    if v.status != "inconclusive":
                        break
        return v.status == "met", len(t.pending_cases()) > 0


_ARM_FN = {"trust": arm_trust, "retry": arm_retry,
             "reread": arm_reread, "reread_wait": arm_reread_wait,
             "reread_no_retry": arm_reread_no_retry,
             "vexinter": arm_vexinter}


def run(fault, arm, n):
    totals = dict.fromkeys(COLS, 0)
    for i in range(n):
        crm, billing = CrmSystem(), FAULTS[fault]()
        email = f"bench-{fault}-{arm}-{i}@example.com"
        claims, escalated = _ARM_FN[arm](crm, billing, email)
        achieved, issued = oracle(crm, billing, email)
        totals["silent"] += int(claims and not achieved)
        totals["understates"] += int((not claims) and achieved)
        totals["duplicates"] += int(issued > 1)
        totals["escalated"] += int(escalated)
        totals["achieved"] += int(achieved)
    return totals


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=1,
                    help="repetitions per class and arm")
    n = ap.parse_args().n
    print(f"{'class':<18}{'arm':<21}" + "".join(f"{c:>13}" for c in COLS))
    totals = {b: dict.fromkeys(COLS, 0) for b in ARMS}
    for fault in FAULTS:
        for arm in ARMS:
            r = run(fault, arm, n)
            for c in COLS:
                totals[arm][c] += r[c]
            print(f"{fault:<18}{arm:<21}"
                  + "".join(f"{r[c]:>13}" for c in COLS))
        print()
    print(f"TOTAL ({n} repetition(s) per class and arm)")
    for arm in ARMS:
        print(f"{'':<18}{arm:<21}"
              + "".join(f"{totals[arm][c]:>13}" for c in COLS))


if __name__ == "__main__":
    main()
