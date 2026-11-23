"""Partial failures in multi-system flows: forward retry,
backward compensation without deleting history, and ambiguous effect → human."""
from vexinter import Vexinter
from vexinter.core.coordinator import CompensatingSpec, Plan, Step
from vexinter.demo import BillingSystem, CrmSystem, NotifierSystem

DATA = {"name": "Ana", "email": "ana@example.com"}


def coordinated_condition():
    """The global condition expresses the BUSINESS objective:
    active customer + ISSUED invoice + correct relationship + no duplicates."""
    return {
        "op": "all",
        "args": [
            {"op": "exists", "system": "crm",
             "query": {"type": "customer",
                       "where": {"email": {"$ref": "input.email"}}},
             "as": "customer"},
            {"op": "exists", "system": "billing",
             "query": {"type": "invoice",
                       "where": {"customer_id": {"$ref": "customer.id"},
                                 "status": "issued"}},
             "as": "invoice"},
            {"op": "var_equals",
             "a": {"from": "invoice", "path": "customer_id"},
             "b": {"from": "customer", "path": "id"}}, #
            {"op": "count", "system": "billing",
             "query": {"type": "invoice",
                       "where": {"customer_id": {"$ref": "customer.id"},
                                 "status": "issued"}},
             "equals": 1},
        ],
    }


def post_recovery_condition():
    """Post-recovery invariant: after compensating, no ISSUED
    invoice may remain for the execution's customer."""
    return {
        "op": "count", "system": "billing",
        "query": {"type": "invoice",
                  "where": {"customer_id": {"$ref": "steps.a1.data.id"},
                            "status": "issued"}},
        "equals": 0,
    }


def _customer_step():
    return Step(
        id="a1", tool="crm.create_customer", args=DATA,
        postcondition={"op": "exists", "system": "crm",
                       "query": {"type": "customer",
                                 "where": {"email": {"$ref": "input.email"}}},
                       "as": "customer"},
        compensation=CompensatingSpec(tool="crm.archive_customer",
                                     args_from_result={"customer_id": "id"}))


def _invoice_step():
    return Step(
        id="a2", tool="billing.create_invoice", args={"amount": 100},
        args_from={"customer_id": "a1.data.id"}, depends_on=["a1"],
        postcondition={"op": "exists", "system": "billing",
                      "query": {"type": "invoice",
                                "where": {"customer_id": {"$ref": "customer.id"},
                                          "status": "issued"}},
                      "as": "invoice"},
        compensation=CompensatingSpec(tool="billing.void_invoice",
                                      args_from_result={"invoice_id": "id"}))


def plan_two_steps() -> Plan:
    return Plan(steps=[_customer_step(), _invoice_step()],
                post_recovery_condition=post_recovery_condition())


def plan_three_steps() -> Plan:
    return Plan(steps=[_customer_step(), _invoice_step(),
                       Step(id="a3", tool="notify.send_welcome_email",
                            args_from={"customer_id": "a1.data.id",
                                       "email": "a1.data.email"},
                            depends_on=["a2"])],
                post_recovery_condition=post_recovery_condition())


def _vex(*adapters):
    return Vexinter(embedded=True, adapters=list(adapters))


def scenario_4():
    print("── Scenario 4: false success in a step → FORWARD retry ──")
    vex = _vex(CrmSystem(), BillingSystem(fail_first=1))
    with vex.task(agent="orchestrator",
                  objective="Customer signup and invoice (coordinated plan)",
                  condition=coordinated_condition(),
                  input={"email": DATA["email"]}) as t:
        v = t.run_plan(plan_two_steps())
        print("  verdict:", v.status)
        print("  state:   ", t.state.value)
        print("  chain intact:", t.audit().ok)


def scenario_5():
    print("── Scenario 5: later failure → BACKWARD compensation ──")
    crm, billing = CrmSystem(), BillingSystem()
    vex = _vex(crm, billing, NotifierSystem())
    with vex.task(agent="orchestrator",
                  objective="Signup, invoice and welcome (coordinated plan)",
                  condition=coordinated_condition(),
                  input={"email": DATA["email"]}) as t:
        v = t.run_plan(plan_three_steps())
        print("  verdict:", v.status)
        print("  state:   ", t.state.value)
        print("  invoice voided (not deleted):",
              billing.find({})[0]["status"])
        print("  customer archived (not deleted):",
              crm.observe({"type": "customer",
                           "where": {"email": DATA["email"]}}).data["status"])
        g = t.graph()
        print("  'compenso_a' edges:",
              [e for e in g["edges"] if e["rel"] == "compenso_a"])
        print("  chain intact:", t.audit().ok)


def scenario_6():
    print("── Scenario 6: ambiguous effect → broken invariant → HUMAN ──")
    billing = BillingSystem(fail_first=1, supports_idempotency=False,
                            leak_on_fail=True)
    vex = _vex(CrmSystem(), billing)
    with vex.task(agent="orchestrator",
                  objective="Customer signup and invoice (coordinated plan)",
                  condition=coordinated_condition(),
                  input={"email": DATA["email"]}) as t:
        v = t.run_plan(plan_two_steps())
        print("  final verdict:",
              v.status if v is not None else "pending human decision")
        print("  state:   ", t.state.value)
        print("  human cases:", len(t.pending_cases()))
        print("  active invoices (evidence):", billing.find({"status": "issued"}))


if __name__ == "__main__":
    scenario_4(); scenario_5(); scenario_6()
