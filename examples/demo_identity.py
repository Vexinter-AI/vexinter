""" in action: authenticated multi-agent (scenario 7), detected attacks
(scenario 8) and response to a compromised instance (scenario 9)."""
import json

from vexinter import Vexinter
from vexinter.core.tools import AuthorizationError
from vexinter.demo import BillingSystem, CrmSystem
from vexinter.identity import (
    AgentKeyMaterial,
    IdentityService,
    Principal,
    Rejection,
    build_envelope,
)

DATA = {"name": "Ana", "email": "ana@example.com"}

COND = {
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
         "b": {"from": "customer", "path": "id"}},
        {"op": "count", "system": "billing",
         "query": {"type": "invoice",
                   "where": {"customer_id": {"$ref": "customer.id"},
                             "status": "issued"}},
         "equals": 1},
    ],
}


def _spawn(ids: IdentityService, agent_id: str):
    """Spins up an instance with its keys: private keys NEVER go
    through Vexinter."""
    keys = AgentKeyMaterial()
    inst = ids.spawn_instance(agent_id)
    cred = ids.issue_credential(inst.instance_id,
                                keys.signing_pub_b64, keys.encryption_pub_b64)
    return keys, inst, cred


def _authenticated_vex():
    vex = Vexinter(embedded=True, adapters=[CrmSystem(), BillingSystem()],
                   authenticated=True)
    ids = vex.identity
    # step 2 + least privilege:
    ids.enroll_agent("agent_a", allowed_tools=set(),          # messaging only
                     allowed_recipients={"agent_b"})
    ids.enroll_agent("agent_b", allowed_tools={"crm.create_customer"},
                     allowed_recipients={"agent_a"})
    ids.enroll_agent("agent_c", allowed_tools={"billing.create_invoice"})
    return vex, ids


def scenario_7():
    print("── Scenario 7: steps 1-7 with identities and secure channel ──")
    vex, ids = _authenticated_vex()
    msgs = vex.messaging
    kA, iA, cA = _spawn(ids, "agent_a")
    kB, iB, cB = _spawn(ids, "agent_b")
    _kC, iC, cC = _spawn(ids, "agent_c")

    with vex.task(agent="agent_a",
                  principal=Principal(
                      agent_id="agent_a",
                      instance_id=iA.instance_id,
                      credential_id=cA.credential_id
                  ),
                  objective="Register customer and issue invoice (multi-agent)",
                  condition=COND, input={"email": DATA["email"]}) as t:
        # step 4: A → B over secure communication
        env = build_envelope(
            identity_service=ids, sender_keys=kA,
            credential_id=cA.credential_id, to_agent="agent_b",
            plaintext=json.dumps(DATA),
            sequence=msgs.next_sequence("agent_a", "agent_b"),
            task_id=t.task_id, execution_id=t.execution_id)
        msgs.send(env)
        data_b = json.loads(msgs.receive(kB, env))      # B decrypts with THEIR key
        # step 5: B registers the customer with its identity (least privilege)
        r = t.call_as(Principal(
                          agent_id="agent_b",
                          instance_id=iB.instance_id,
                          credential_id=cB.credential_id
                      ),
                      "crm.create_customer", data_b)
        # step 6: C issues the invoice with its identity
        t.call_as(Principal(
                      agent_id="agent_c",
                      instance_id=iC.instance_id,
                      credential_id=cC.credential_id
                  ),
                  "billing.create_invoice",
                  {"customer_id": r.id, "amount": 100})
        # step 7: independent verification
        v = t.verify()
        print("  verdict:", v.status, "| state:", t.state.value)
        g = t.graph()
        print("  messages in the graph:",
              sum(1 for n in g["nodes"] if n["type"].startswith("message")))
        print("  chain intact:", t.audit().ok)


def scenario_8():
    print("── Scenario 8:, replay, tampering, invalid signature ──")
    vex, ids = _authenticated_vex()
    msgs = vex.messaging
    kA, _iA, cA = _spawn(ids, "agent_a")
    _spawn(ids, "agent_b")

    env = build_envelope(identity_service=ids, sender_keys=kA,
                         credential_id=cA.credential_id, to_agent="agent_b",
                         plaintext=json.dumps(DATA),
                         sequence=msgs.next_sequence("agent_a", "agent_b"))
    msgs.send(env)                                       # legitimate message

    from vexinter.identity import b64d, b64e
    for label, mutate in [
        ("replay of the same envelope", lambda e: e),
        ("tampered ciphertext", lambda e: e.model_copy(
            deep=True, update={"ct": b64e(b64d(e.ct) + b"x")})),
        ("invalid signature", lambda e: e.model_copy(
            deep=True, update={"signature": b64e(b"\x00" * 64)})),
    ]:
        try:
            msgs.send(mutate(env))
            print(f"  {label}: NOT DETECTED (error!)")
        except Rejection as r:
            print(f"  {label}: rejected ({r.reason.value})")

    # revocation IN FLIGHT: the envelope was built with a valid credential,
    # but the revocation arrives before the send
    env2 = build_envelope(identity_service=ids, sender_keys=kA,
                          credential_id=cA.credential_id, to_agent="agent_b",
                          plaintext=json.dumps(DATA), sequence=2)
    ids.revoke_credential(cA.credential_id, "revocation in flight (demo)")
    try:
        msgs.send(env2)
    except Rejection as r:
        print(f"  credential revoked in flight: rejected ({r.reason.value})")


def scenario_9():
    print("── Scenario 9:, compromised instance, intact agent ──")
    vex, ids = _authenticated_vex()
    _kA, iA, cA = _spawn(ids, "agent_a")
    _kB, iB, cB = _spawn(ids, "agent_b")
    _kC, iC, cC = _spawn(ids, "agent_c")

    with vex.task(agent="agent_a",
                  principal=Principal(
                      agent_id="agent_a",
                      instance_id=iA.instance_id,
                      credential_id=cA.credential_id
                  ),
                  objective="Register customer and issue invoice",
                  condition=COND, input={"email": DATA["email"]}) as t:
        # B's instance gets compromised and is revoked LIVE
        ids.revoke_instance(iB.instance_id, "compromised instance")
        try:
            t.call_as(Principal(
                          agent_id="agent_b",
                          instance_id=iB.instance_id,
                          credential_id=cB.credential_id
                      ),
                      "crm.create_customer", DATA)
        except AuthorizationError as e:
            print("  compromised B, invocation rejected:", e)
        # the LOGICAL agent survives: a new instance of agent_b operates
        kB2 = AgentKeyMaterial()
        iB2 = ids.spawn_instance("agent_b")
        cB2 = ids.issue_credential(iB2.instance_id, kB2.signing_pub_b64,
                                   kB2.encryption_pub_b64)
        r = t.call_as(Principal(
                          agent_id="agent_b",
                          instance_id=iB2.instance_id,
                          credential_id=cB2.credential_id
                      ),
                      "crm.create_customer", DATA)
        print("  new instance of agent_b: customer", r.data["id"])
        t.call_as(Principal(
                      agent_id="agent_c",
                      instance_id=iC.instance_id,
                      credential_id=cC.credential_id
                  ),
                  "billing.create_invoice", {"customer_id": r.id, "amount": 100})
        print("  verdict:", t.verify().status)
        print("  audited rejections:",
              sum(1 for e in t.events() if e.type.value == "tool.rejected"))


if __name__ == "__main__":
    scenario_7(); scenario_8(); scenario_9()
