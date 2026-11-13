import json

import pytest

from examples.demo_identidad import COND, DATOS
from vexinter import Vexinter
from vexinter.core.events import EventType
from vexinter.core.store import EventQuery
from vexinter.core.tools import AuthorizationError
from vexinter.demo import BillingSystem, CrmSystem
from vexinter.identity import (
    AgentKeyMaterial,
    Principal,
    Rejection,
    RejectionReason,
    b64d,
    b64e,
    build_envelope,
)


def _setup():
    vex = Vexinter(embedded=True, adapters=[CrmSystem(), BillingSystem()],
                   authenticated=True)          # fail-closed (§14)
    ids, msgs = vex.identity, vex.messaging
    ids.enroll_agent("agente_a", allowed_tools=set(),
                     allowed_recipients={"agente_b"})
    ids.enroll_agent("agente_b", allowed_tools={"crm.create_customer"})
    ids.enroll_agent("agente_c", allowed_tools={"billing.create_invoice"})
    def spawn(a):
        k = AgentKeyMaterial()
        i = ids.spawn_instance(a)
        c = ids.issue_credential(i.instance_id, k.signing_pub_b64,
                                 k.encryption_pub_b64)
        return k, i, c
    return vex, ids, msgs, spawn


def _envio_valido(ids, msgs, kA, cA, to="agente_b", pt=None, seq=None):
    return build_envelope(
        identity_service=ids, sender_keys=kA, credential_id=cA.credential_id,
        to_agent=to, plaintext=pt or json.dumps(DATOS),
        sequence=seq or msgs.next_sequence("agente_a", to))


def test_replay_del_mismo_sobre_rechazado():
    """§9.4: replay-attack protection."""
    _vex, ids, msgs, spawn = _setup()
    kA, _, cA = spawn("agente_a"); spawn("agente_b")
    env = _envio_valido(ids, msgs, kA, cA)
    msgs.send(env)
    with pytest.raises(Rejection) as ei:
        msgs.send(env)
    assert ei.value.reason is RejectionReason.REPLAY


def test_manipulacion_y_falsa_firma_detectadas():
    _vex, ids, msgs, spawn = _setup()
    kA, _, cA = spawn("agente_a"); spawn("agente_b")
    env = _envio_valido(ids, msgs, kA, cA)
    msgs.send(env)
    alterado = env.model_copy(deep=True)
    alterado.ct = b64e(b64d(alterado.ct) + b"x")
    with pytest.raises(Rejection) as e1:
        msgs.send(alterado)
    firmado = env.model_copy(deep=True, update={"signature": b64e(b"\x00" * 64)})
    with pytest.raises(Rejection) as e2:
        msgs.send(firmado)
    assert {e1.value.reason, e2.value.reason} <= {
        RejectionReason.BAD_SIGNATURE, RejectionReason.TAMPERING}


def test_revocacion_en_vuelo_rechaza_el_mensaje():
    _vex, ids, msgs, spawn = _setup()
    kA, _, cA = spawn("agente_a"); spawn("agente_b")
    env = _envio_valido(ids, msgs, kA, cA, seq=1)
    ids.revoke_credential(cA.credential_id, "compromiso")
    with pytest.raises(Rejection) as ei:
        msgs.send(env)
    assert ei.value.reason is RejectionReason.INVALID_CREDENTIAL


def test_content_is_not_in_the_log_but_the_hash_is():
    """§9.5: metadata and cryptographic references; content stays E2E-only."""
    vex, ids, msgs, spawn = _setup()
    kA, iA, cA = spawn("agente_a"); kB, _iB, _cB = spawn("agente_b")
    with vex.task(agent="agente_a",
                  principal=Principal(agent_id="agente_a", instance_id=iA.instance_id, credential_id=cA.credential_id),
                  objective="o", condition=COND,
                  input={"email": DATOS["email"]}):
        env = _envio_valido(ids, msgs, kA, cA)
        msgs.send(env)
        msgs.receive(kB, env)
        sent = [e for e in msgs._store.query(EventQuery(types={EventType.MESSAGE_SENT})) if e.type == EventType.MESSAGE_SENT]
        blob = json.dumps([e.model_dump(mode="json") for e in sent])
        assert "Ana" not in blob and "ana@ejemplo" not in blob
        assert env.body_hash in blob           # the commitment IS public


def test_e2e_decryption_and_body_hash_are_verified_by_the_recipient():
    _vex, ids, msgs, spawn = _setup()
    kA, _, cA = spawn("agente_a"); kB, _, _ = spawn("agente_b")
    env = _envio_valido(ids, msgs, kA, cA)
    msgs.send(env)
    pt = msgs.receive(kB, env)
    assert json.loads(pt)["email"] == DATOS["email"]


def test_fail_closed_sin_credencial_y_politicas_minimas():
    """§9.2 + §14: no credential, no operation; and with a credential,
    only what the agent policy allows."""
    vex, ids, msgs, spawn = _setup()
    kA, iA, cA = spawn("agente_a")
    _kB, iB, cB = spawn("agente_b")
    _kC, _iC, _cC = spawn("agente_c")
    with vex.task(agent="agente_a",
                  principal=Principal(agent_id="agente_a", instance_id=iA.instance_id, credential_id=cA.credential_id),
                  objective="o", condition=COND,
                  input={"email": DATOS["email"]}) as t:
        with pytest.raises(AuthorizationError):
            t.call_as(Principal(agent_id="agente_b"),     # anonymous → rejected
                      "crm.create_customer", DATOS)
        with pytest.raises(AuthorizationError):
            t.call_as(Principal(agent_id="agente_b", instance_id=iB.instance_id, credential_id=cB.credential_id),
                      "billing.create_invoice",          # outside their policy
                      {"customer_id": "cus_x", "amount": 1})
        with pytest.raises(Rejection) as ei:
            msgs.send(_envio_valido(ids, msgs, kA, cA, to="agente_c"))
        assert ei.value.reason is RejectionReason.NOT_AUTHORIZED


def test_instancia_revocada_agente_logico_sobrevive():
    """§9.1: revoke one instance without treating all identities
    as equivalent."""
    vex, ids, _msgs, spawn = _setup()
    _kA, iA, cA = spawn("agente_a")
    _, iB, cB = spawn("agente_b")
    with vex.task(agent="agente_a",
                  principal=Principal(agent_id="agente_a", instance_id=iA.instance_id, credential_id=cA.credential_id),
                  objective="o", condition=COND,
                  input={"email": DATOS["email"]}) as t:
        ids.revoke_instance(iB.instance_id, "compromiso")
        with pytest.raises(AuthorizationError):
            t.call_as(Principal(agent_id="agente_b", instance_id=iB.instance_id, credential_id=cB.credential_id),
                      "crm.create_customer", DATOS)
        kB2 = AgentKeyMaterial()
        iB2 = ids.spawn_instance("agente_b")
        cB2 = ids.issue_credential(iB2.instance_id, kB2.signing_pub_b64,
                                    kB2.encryption_pub_b64)
        r = t.call_as(Principal(agent_id="agente_b", instance_id=iB2.instance_id, credential_id=cB2.credential_id),
                      "crm.create_customer", DATOS)
        assert r.ok