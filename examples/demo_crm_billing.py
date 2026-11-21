"""Reproduces the complete example from the document in three scenarios."""
from datetime import timedelta

from vexinter import Vexinter
from vexinter.core.events import now
from vexinter.demo import BillingAdapter, CrmAdapter

CONDITION = {
    "op": "all",
    "args": [
        {"op": "exists", "system": "crm",
         "query": {"type": "customer", "where": {"email": {"$ref": "input.email"}}},
         "as": "customer"},
        {"op": "exists", "system": "billing",
         "query": {"type": "invoice", "where": {"customer_id": {"$ref": "customer.id"}}},
         "as": "invoice"},
        {"op": "var_equals",
         "a": {"from": "invoice", "path": "customer_id"},
         "b": {"from": "customer", "path": "id"}},
        {"op": "count", "system": "billing",
         "query": {"type": "invoice", "where": {"customer_id": {"$ref": "customer.id"}}},
         "equals": 1},
    ],
}

DATA = {"name": "Ana", "email": "ana@example.com"}


def _new_vex(adapters):
    return Vexinter(embedded=True, adapters=adapters)


def scenario_1():
    print("── Scenario 1: false success detected and recovered ──")
    vex = _new_vex([CrmAdapter(), BillingAdapter(fail_first=1)])
    with vex.task(agent="billing_agent",
                  objective="Create a customer and generate their invoice",
                  condition=CONDITION, input={"email": DATA["email"]}) as t:
        customer = t.tools.crm.create_customer(DATA)
        t.tools.billing.create_invoice(
            {"customer_id": customer.id, "amount": 100})
        r = t.verify()
        print("  initial verification:", r.status)
        if r.status != "met":
            t.recover(failed_tool="billing.create_invoice")
            print("  final verification:  ", t.verify().status)
        print("  chain intact:", t.audit().ok)


def scenario_2():
    print("── Scenario 2: stale replica, inconclusive result ──")
    billing = BillingAdapter(read_consistency="eventual")
    vex = _new_vex([CrmAdapter(), billing])
    with vex.task(agent="billing_agent",
                  objective="Create a customer and generate their invoice",
                  condition=CONDITION, input={"email": DATA["email"]}) as t:
        customer = t.tools.crm.create_customer(DATA)
        t.tools.billing.create_invoice(
            {"customer_id": customer.id, "amount": 100})
        r = t.verify()
        print("  initial verification:", r.status)
        billing.tick()
        res = t.run_followups(at=now() + timedelta(seconds=61))
        print("  after the review:    ", res[-1].status)


def scenario_3():
    print("── Scenario 3: human decision; the state changed while waiting ──")
    billing = BillingAdapter(fail_first=1, supports_idempotency=False)
    vex = _new_vex([CrmAdapter(), billing])
    with vex.task(agent="billing_agent",
                  objective="Create a customer and generate their invoice",
                  condition=CONDITION, input={"email": DATA["email"]}) as t:
        customer = t.tools.crm.create_customer(DATA)
        t.tools.billing.create_invoice(
            {"customer_id": customer.id, "amount": 100})
        t.verify()
        case = t.recover(failed_tool="billing.create_invoice")
        print("  case:", case.status)
        print("  options:", [o.action for o in t.case_options(case.id)])
        billing.simulate_external_sync({"customer_id": customer.id, "amount": 100})
        v = t.decide(case.id, action="retry")
        print("  decision 'retry' → result:", v.status)
        print("  case:", case.status)


if __name__ == "__main__":
    scenario_1(); scenario_2(); scenario_3()
