import json

import pytest

from examples.demo_identity import COND, DATA
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
                   authenticated=True) # fail-closed
    ids, msgs = vex.identity, vex.messaging
    ids.enroll_agent("agent_a", allowed_tools=set(),
                     allowed_recipients={"agent_b"})
    ids.enroll_agent("agent_b", allowed_tools={"crm.create_customer"})
    ids.enroll_agent("agent_c", allowed_tools={"billing.create_invoice"})
    def spawn(a):
        k = AgentKeyMaterial()
        i = ids.spawn_instance(a)
        c = ids.issue_credential(i.instance_id, k.signing_pub_b64,
                                 k.encryption_pub_b64)
        return k, i, c
    return vex, ids, msgs, spawn


def _valid_send(ids, msgs, kA, cA, to="agent_b", pt=None, seq=None):
    return build_envelope(
        identity_service=ids, sender_keys=kA, credential_id=cA.credential_id,
        to_agent=to, plaintext=pt or json.dumps(DATA),
        sequence=seq or msgs.next_sequence("agent_a", to))


def test_replay_of_same_on_rejected():
    """ Replay-attack protection."""
    _vex, ids, msgs, spawn = _setup()
    kA, _, cA = spawn("agent_a"); spawn("agent_b")
    env = _valid_send(ids, msgs, kA, cA)
    msgs.send(env)
    with pytest.raises(Rejection) as ei:
        msgs.send(env)
    assert ei.value.reason is RejectionReason.REPLAY


def test_tampering_y_false_signature_detected():
    _vex, ids, msgs, spawn = _setup()
    kA, _, cA = spawn("agent_a"); spawn("agent_b")
    env = _valid_send(ids, msgs, kA, cA)
    msgs.send(env)
    altered = env.model_copy(deep=True)
    altered.ct = b64e(b64d(altered.ct) + b"x")
    with pytest.raises(Rejection) as e1:
        msgs.send(altered)
    signed = env.model_copy(deep=True, update={"signature": b64e(b"\x00" * 64)})
    with pytest.raises(Rejection) as e2:
        msgs.send(signed)
    assert {e1.value.reason, e2.value.reason} <= {
        RejectionReason.BAD_SIGNATURE, RejectionReason.TAMPERING}


def test_in_flight_revocation_rejects_the_message():
    _vex, ids, msgs, spawn = _setup()
    kA, _, cA = spawn("agent_a"); spawn("agent_b")
    env = _valid_send(ids, msgs, kA, cA, seq=1)
    ids.revoke_credential(cA.credential_id, "compromiso")
    with pytest.raises(Rejection) as ei:
        msgs.send(env)
    assert ei.value.reason is RejectionReason.INVALID_CREDENTIAL


def test_content_is_not_in_the_log_but_the_hash_is():
    """ Metadata and cryptographic references; content stays E2E-only."""
    vex, ids, msgs, spawn = _setup()
    kA, iA, cA = spawn("agent_a"); kB, _iB, _cB = spawn("agent_b")
    with vex.task(agent="agent_a",
                  principal=Principal(agent_id="agent_a", instance_id=iA.instance_id, credential_id=cA.credential_id),
                  objective="o", condition=COND,
                  input={"email": DATA["email"]}):
        env = _valid_send(ids, msgs, kA, cA)
        msgs.send(env)
        msgs.receive(kB, env)
        sent = [e for e in msgs._store.query(EventQuery(types={EventType.MESSAGE_SENT})) if e.type == EventType.MESSAGE_SENT]
        blob = json.dumps([e.model_dump(mode="json") for e in sent])
        assert "Ana" not in blob and "ana@example" not in blob
        assert env.body_hash in blob           # the commitment IS public


def test_e2e_decryption_and_body_hash_are_verified_by_the_recipient():
    _vex, ids, msgs, spawn = _setup()
    kA, _, cA = spawn("agent_a"); kB, _, _ = spawn("agent_b")
    env = _valid_send(ids, msgs, kA, cA)
    msgs.send(env)
    pt = msgs.receive(kB, env)
    assert json.loads(pt)["email"] == DATA["email"]


def test_fail_closed_without_credential_and_minimal_policies():
    """ +: no credential, no operation; and with a credential,
    only what the agent policy allows."""
    vex, ids, msgs, spawn = _setup()
    kA, iA, cA = spawn("agent_a")
    _kB, iB, cB = spawn("agent_b")
    _kC, _iC, _cC = spawn("agent_c")
    with vex.task(agent="agent_a",
                  principal=Principal(agent_id="agent_a", instance_id=iA.instance_id, credential_id=cA.credential_id),
                  objective="o", condition=COND,
                  input={"email": DATA["email"]}) as t:
        with pytest.raises(AuthorizationError):
            t.call_as(Principal(agent_id="agent_b"),     # anonymous → rejected
                      "crm.create_customer", DATA)
        with pytest.raises(AuthorizationError):
            t.call_as(Principal(agent_id="agent_b", instance_id=iB.instance_id, credential_id=cB.credential_id),
                      "billing.create_invoice",          # outside their policy
                      {"customer_id": "cus_x", "amount": 1})
        with pytest.raises(Rejection) as ei:
            msgs.send(_valid_send(ids, msgs, kA, cA, to="agent_c"))
        assert ei.value.reason is RejectionReason.NOT_AUTHORIZED


def test_revoked_instance_logical_agent_survives():
    """ Revoke one instance without treating all identities
    as equivalent."""
    vex, ids, _msgs, spawn = _setup()
    _kA, iA, cA = spawn("agent_a")
    _, iB, cB = spawn("agent_b")
    with vex.task(agent="agent_a",
                  principal=Principal(agent_id="agent_a", instance_id=iA.instance_id, credential_id=cA.credential_id),
                  objective="o", condition=COND,
                  input={"email": DATA["email"]}) as t:
        ids.revoke_instance(iB.instance_id, "compromiso")
        with pytest.raises(AuthorizationError):
            t.call_as(Principal(agent_id="agent_b", instance_id=iB.instance_id, credential_id=cB.credential_id),
                      "crm.create_customer", DATA)
        kB2 = AgentKeyMaterial()
        iB2 = ids.spawn_instance("agent_b")
        cB2 = ids.issue_credential(iB2.instance_id, kB2.signing_pub_b64,
                                    kB2.encryption_pub_b64)
        r = t.call_as(Principal(agent_id="agent_b", instance_id=iB2.instance_id, credential_id=cB2.credential_id),
                      "crm.create_customer", DATA)
        assert r.ok