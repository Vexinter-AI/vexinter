"""Populates the service with varied states to walk through the console.

Prerequisite: demo_distributed has already run (it consumes the initial
false success of billing). Generates three tasks: fulfilled, failed with
compensation, and pending human decision."""
import os
import sys

from examples.demo_coordinated import coordinated_condition
from vexinter import Vexinter
from vexinter.core.coordinator import CompensatingSpec, Plan, Step
from vexinter.identity import AgentKeyMaterial
from vexinter.remote import RemoteError

ENDPOINT = os.environ.get("VEXINTER_ENDPOINT", "http://localhost:8000")
ADMIN = os.environ.get("VEXINTER_ADMIN_TOKEN", "demo-admin-token")

ORCHESTRATOR_TOOLS = {
    "crm.create_customer", "billing.create_invoice",
    "notify.send_welcome_email",
    "crm.archive_customer", "billing.void_invoice"}


def _enroll(adm):
    try:
        adm.remote.enroll_agent("orchestrator",
                                allowed_tools=ORCHESTRATOR_TOOLS)
    except RemoteError as e:
        print(f"could not enroll 'orchestrator': {e}")
        print("if it already exists with incomplete permissions, run:")
        print("  docker compose down -v && docker compose up --build -d")
        print("and repeat demo_distributed before this demo.")
        sys.exit(1)


def _a1(email, name, compensatable=True):
    return Step(
        id="a1", tool="crm.create_customer",
        args={"name": name, "email": email},
        postcondition={"op": "exists", "system": "crm",
                       "query": {"type": "customer",
                                 "where": {"email": email}},
                       "as": "customer"},
        compensation=(CompensatingSpec(tool="crm.archive_customer",
                                       args_from_result={"customer_id": "id"})
                     if compensatable else None))


def _a2():
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


def _a3(depends_on=("a2",)):
    return Step(id="a3", tool="notify.send_welcome_email",
                args_from={"customer_id": "a1.data.id",
                           "email": "a1.data.email"},
                depends_on=list(depends_on))


def _post_recovery():
    return {"op": "count", "system": "billing",
            "query": {"type": "invoice",
                      "where": {"customer_id": {"$ref": "steps.a1.data.id"},
                                "status": "issued"}},
            "equals": 0}


def main():
    adm = Vexinter(endpoint=ENDPOINT, admin_token=ADMIN)
    _enroll(adm)
    keys = AgentKeyMaterial()
    inst = adm.remote.spawn_instance("orchestrator")
    cred = adm.remote.issue_credential(inst["instance_id"], keys)
    vex = Vexinter(endpoint=ENDPOINT, credentials=cred)

    def run_task(objetivo, name, email, plan):
        with vex.task(agent="orchestrator", objective=objetivo,
                      condition=coordinated_condition(),
                      input={"email": email}) as t:
            v = t.run_plan(plan)
            print(f"{objetivo}: {v.status if v else 'no verdict'} "
                  f"→ state {t.state.value} ({t.task_id})")

    # 1) happy path → fulfilled
    run_task("Signup and invoice (happy)", "Luis", "luis@example.com",
          Plan(steps=[_a1("luis@example.com", "Luis"), _a2()],
               post_recovery_condition=_post_recovery()))

    # 2) notification failure → backward compensation → failed
    run_task("Signup, invoice and welcome (compensation)", "Eva", "eva@example.com",
          Plan(steps=[_a1("eva@example.com", "Eva"), _a2(), _a3()],
               post_recovery_condition=_post_recovery()))

    run_task("Signup and welcome (human decision)", "Sofia", "sofia@example.com",
          Plan(steps=[_a1("sofia@example.com", "Sofia", compensatable=False),
                      _a3(depends_on=("a1",))]))

    print("\nConsole: http://localhost:8000/console/login "
          "(token: the value of VEXINTER_ADMIN_TOKEN)")


if __name__ == "__main__":
    main()
