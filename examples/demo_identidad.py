"""§9 in action: authenticated multi-agent (scenario 7), detected attacks
(scenario 8) and response to a compromised instance (scenario 9, §14)."""
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

DATOS = {"name": "Ana", "email": "ana@ejemplo.com"}

COND = {
    "op": "all",
    "args": [
        {"op": "exists", "system": "crm",
         "query": {"type": "customer",
                   "where": {"email": {"$ref": "input.email"}}},
         "as": "cliente"},
        {"op": "exists", "system": "billing",
         "query": {"type": "invoice",
                   "where": {"customer_id": {"$ref": "cliente.id"},
                             "status": "issued"}},
         "as": "factura"},
        {"op": "var_equals",
         "a": {"from": "factura", "path": "customer_id"},
         "b": {"from": "cliente", "path": "id"}},
        {"op": "count", "system": "billing",
         "query": {"type": "invoice",
                   "where": {"customer_id": {"$ref": "cliente.id"},
                             "status": "issued"}},
         "equals": 1},
    ],
}


def _spawn(ids: IdentityService, agent_id: str):
    """Spins up an instance with its keys: private keys NEVER go
    through Vexinter (§14)."""
    keys = AgentKeyMaterial()
    inst = ids.spawn_instance(agent_id)
    cred = ids.issue_credential(inst.instance_id,
                                keys.signing_pub_b64, keys.encryption_pub_b64)
    return keys, inst, cred


def _vex_autenticada():
    vex = Vexinter(embedded=True, adapters=[CrmSystem(), BillingSystem()],
                   authenticated=True)
    ids = vex.identity
    # §10 step 2 + §14 least privilege:
    ids.enroll_agent("agente_a", allowed_tools=set(),          # messaging only
                     allowed_recipients={"agente_b"})
    ids.enroll_agent("agente_b", allowed_tools={"crm.create_customer"},
                     allowed_recipients={"agente_a"})
    ids.enroll_agent("agente_c", allowed_tools={"billing.create_invoice"})
    return vex, ids


def escenario_7():
    print("── Scenario 7: §10 steps 1-7 with identities and secure channel ──")
    vex, ids = _vex_autenticada()
    msgs = vex.messaging
    kA, iA, cA = _spawn(ids, "agente_a")
    kB, iB, cB = _spawn(ids, "agente_b")
    _kC, iC, cC = _spawn(ids, "agente_c")

    with vex.task(agent="agente_a",
                  principal=Principal(
                      agent_id="agente_a",
                      instance_id=iA.instance_id,
                      credential_id=cA.credential_id
                  ),
                  objective="Register customer and issue invoice (multi-agent)",
                  condition=COND, input={"email": DATOS["email"]}) as t:
        # step 4 (§10): A → B over secure communication
        env = build_envelope(
            identity_service=ids, sender_keys=kA,
            credential_id=cA.credential_id, to_agent="agente_b",
            plaintext=json.dumps(DATOS),
            sequence=msgs.next_sequence("agente_a", "agente_b"),
            task_id=t.task_id, execution_id=t.execution_id)
        msgs.send(env)
        datos_b = json.loads(msgs.receive(kB, env))      # B decrypts with THEIR key
        # step 5: B registers the customer with its identity (least privilege)
        r = t.call_as(Principal(
                          agent_id="agente_b",
                          instance_id=iB.instance_id,
                          credential_id=cB.credential_id
                      ),
                      "crm.create_customer", datos_b)
        # step 6: C issues the invoice with its identity
        t.call_as(Principal(
                      agent_id="agente_c",
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


def escenario_8():
    print("── Scenario 8: §9.4 — replay, tampering, invalid signature ──")
    vex, ids = _vex_autenticada()
    msgs = vex.messaging
    kA, _iA, cA = _spawn(ids, "agente_a")
    _spawn(ids, "agente_b")

    env = build_envelope(identity_service=ids, sender_keys=kA,
                         credential_id=cA.credential_id, to_agent="agente_b",
                         plaintext=json.dumps(DATOS),
                         sequence=msgs.next_sequence("agente_a", "agente_b"))
    msgs.send(env)                                       # legitimate message

    from vexinter.identity import b64d, b64e
    for etiqueta, mutar in [
        ("replay of the same envelope", lambda e: e),
        ("tampered ciphertext", lambda e: e.model_copy(
            deep=True, update={"ct": b64e(b64d(e.ct) + b"x")})),
        ("invalid signature", lambda e: e.model_copy(
            deep=True, update={"signature": b64e(b"\x00" * 64)})),
    ]:
        try:
            msgs.send(mutar(env))
            print(f"  {etiqueta}: NOT DETECTED (error!)")
        except Rejection as r:
            print(f"  {etiqueta}: rejected ({r.reason.value})")

    # revocation IN FLIGHT: the envelope was built with a valid credential,
    # but the revocation arrives before the send
    env2 = build_envelope(identity_service=ids, sender_keys=kA,
                          credential_id=cA.credential_id, to_agent="agente_b",
                          plaintext=json.dumps(DATOS), sequence=2)
    ids.revoke_credential(cA.credential_id, "revocation in flight (demo)")
    try:
        msgs.send(env2)
    except Rejection as r:
        print(f"  credential revoked in flight: rejected ({r.reason.value})")


def escenario_9():
    print("── Scenario 9: §14 — compromised instance, intact agent ──")
    vex, ids = _vex_autenticada()
    _kA, iA, cA = _spawn(ids, "agente_a")
    _kB, iB, cB = _spawn(ids, "agente_b")
    _kC, iC, cC = _spawn(ids, "agente_c")

    with vex.task(agent="agente_a",
                  principal=Principal(
                      agent_id="agente_a",
                      instance_id=iA.instance_id,
                      credential_id=cA.credential_id
                  ),
                  objective="Register customer and issue invoice",
                  condition=COND, input={"email": DATOS["email"]}) as t:
        # B's instance gets compromised and is revoked LIVE
        ids.revoke_instance(iB.instance_id, "compromised instance (§14)")
        try:
            t.call_as(Principal(
                          agent_id="agente_b",
                          instance_id=iB.instance_id,
                          credential_id=cB.credential_id
                      ),
                      "crm.create_customer", DATOS)
        except AuthorizationError as e:
            print("  compromised B, invocation rejected:", e)
        # the LOGICAL agent survives: a new instance of agente_b operates
        kB2 = AgentKeyMaterial()
        iB2 = ids.spawn_instance("agente_b")
        cB2 = ids.issue_credential(iB2.instance_id, kB2.signing_pub_b64,
                                   kB2.encryption_pub_b64)
        r = t.call_as(Principal(
                          agent_id="agente_b",
                          instance_id=iB2.instance_id,
                          credential_id=cB2.credential_id
                      ),
                      "crm.create_customer", DATOS)
        print("  new instance of agente_b: customer", r.data["id"])
        t.call_as(Principal(
                      agent_id="agente_c",
                      instance_id=iC.instance_id,
                      credential_id=cC.credential_id
                  ),
                  "billing.create_invoice", {"customer_id": r.id, "amount": 100})
        print("  verdict:", t.verify().status)
        print("  audited rejections:",
              sum(1 for e in t.events() if e.type.value == "tool.rejected"))


if __name__ == "__main__":
    escenario_7(); escenario_8(); escenario_9()
