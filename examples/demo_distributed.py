"""The complete example against the deployed service.

    docker compose up --build -d
    pip install -e ".[client]"   # or already installed
    python examples/demo_distributed.py
"""
import json
import os

from vexinter import Vexinter
from vexinter.identity import AgentKeyMaterial, build_envelope_for

ENDPOINT = os.environ.get("VEXINTER_ENDPOINT", "http://localhost:8000")
ADMIN = os.environ.get("VEXINTER_ADMIN_TOKEN", "demo-admin-token")

from examples.demo_identity import COND, DATA


def main():
    adm = Vexinter(endpoint=ENDPOINT, admin_token=ADMIN)

    # step 2: agents with least privilege
    adm.remote.enroll_agent("agent_a", allowed_tools=set(),
                            allowed_recipients={"agent_b"})
    adm.remote.enroll_agent("agent_b",
                            allowed_tools={"crm.create_customer"})
    adm.remote.enroll_agent("agent_c",
                            allowed_tools={"billing.create_invoice"})

    # private keys: ONLY on the client
    keys = {a: AgentKeyMaterial() for a in ("agent_a", "agent_b", "agent_c")}
    creds = {}
    for a, key in keys.items():
        inst = adm.remote.spawn_instance(a)
        creds[a] = adm.remote.issue_credential(inst["instance_id"], key)

    vex_a = Vexinter(endpoint=ENDPOINT, credentials=creds["agent_a"])
    vex_b = Vexinter(endpoint=ENDPOINT, credentials=creds["agent_b"])

    with vex_a.task(agent="agent_a",
                    objective="Register customer and issue invoice (distributed)",
                    condition=COND, input={"email": DATA["email"]}) as t:
        # step 4: secure communication A → B
        env = build_envelope_for(
            vex_a.remote.encryption_pub("agent_b"),
            sender_keys=keys["agent_a"], from_agent="agent_a",
            from_instance=creds["agent_a"].instance_id,
            credential_id=creds["agent_a"].credential_id,
            to_agent="agent_b", plaintext=json.dumps(DATA),
            sequence=vex_a.remote.message_sequence("agent_a", "agent_b"),
            task_id=t.task_id, execution_id=t.execution_id)
        vex_a.remote.message_send(env)
        data_b = json.loads(vex_b.remote.receive_all(keys["agent_b"])[-1])

        # step 5: B registers the customer with THEIR identity
        r = t.call_as(creds["agent_b"], "crm.create_customer", data_b)
        # step 6: C issues the invoice (the first call "lies")
        t.call_as(creds["agent_c"], "billing.create_invoice",
                  {"customer_id": r.id, "amount": 100})

        v = t.verify()                                   # step 7
        print("initial verification:", v.status)
        if v.status != "met":
            case = t.recover(failed_tool="billing.create_invoice")  # steps 8-9
            print("recovery:", case.status.value)
            print("final verification:", t.verify().status)
        print("state:", t.state.value)
        g = t.graph()
        print(f"graph: {len(g['nodes'])} nodes, {len(g['edges'])} edges")
        print("chain intact:", t.audit().ok)
        print("metrics:", {k: v for k, v in adm.remote.metrics().items()
                            if k in ("executions", "false_successes_detected",
                                     "final_states")})
        print("task_id:", t.task_id, "→ use it in demo_restart.py")


if __name__ == "__main__":
    main()
