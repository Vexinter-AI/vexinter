from datetime import UTC, datetime, timedelta

import pytest
from cryptography.exceptions import InvalidTag

import vexinter.identity.service as svc_mod
from vexinter.core.events import EventDraft, EventType
from vexinter.core.store import InMemoryEventStore
from vexinter.identity import (
    AgentKeyMaterial,
    IdentityError,
    IdentityService,
    MessageService,
    Rejection,
    RejectionReason,
    build_envelope_for,
)
from vexinter.identity.messaging import verify_envelope


class _Reloj:
    def __init__(self):
        self.t = datetime.now(UTC)

    def __call__(self):
        return self.t

    def avanzar(self, segundos):
        self.t += timedelta(seconds=segundos)


@pytest.fixture
def reloj(monkeypatch):
    r = _Reloj()
    monkeypatch.setattr(svc_mod, "now", r)
    return r


def _alta(svc, agente):
    svc.enroll_agent(agente, allowed_tools=set(),
                     allowed_recipients={"a", "b"})
    inst = svc.spawn_instance(agente)
    k = AgentKeyMaterial()
    cred = svc.issue_credential(inst.instance_id, k.signing_pub_b64,
                                k.encryption_pub_b64)
    return inst, k, cred


def _rotar(svc, cred, k):
    k2 = k.successor()
    c2 = svc.rotate_credential(cred.credential_id, k2.signing_pub_b64,
                               k2.encryption_pub_b64)
    return k2, c2


def test_the_previous_credential_is_valid_during_grace_then_not(reloj):
    svc = IdentityService(InMemoryEventStore(), credential_ttl_s=3600,
                          rotation_grace_s=900)
    _, k1, c1 = _alta(svc, "a")
    _, c2 = _rotar(svc, c1, k1)
    assert c2.credential_id != c1.credential_id
    svc.assert_credential_valid(c1.credential_id)
    svc.assert_credential_valid(c2.credential_id)
    reloj.avanzar(899)
    svc.assert_credential_valid(c1.credential_id)
    reloj.avanzar(2)                                  # 901 s: fin de la gracia
    with pytest.raises(IdentityError):
        svc.assert_credential_valid(c1.credential_id)
    assert not svc.can_invoke_tool(c1.credential_id, "x")
    svc.assert_credential_valid(c2.credential_id)


def test_rotation_requires_new_valid_keys(reloj):
    svc = IdentityService(InMemoryEventStore())
    inst, _k1, c1 = _alta(svc, "a")
    with pytest.raises(IdentityError):
        svc.rotate_credential(c1.credential_id, c1.signing_pub,
                              c1.encryption_pub)          # las mismas claves
    k2 = AgentKeyMaterial()
    with pytest.raises(IdentityError):
        svc.rotate_credential(c1.credential_id, "no-es-una-clave",
                              k2.encryption_pub_b64)
    with pytest.raises(IdentityError):
        svc.issue_credential(inst.instance_id, k2.signing_pub_b64, "tampoco")


def test_no_se_puede_rotar_dos_veces_la_misma_credencial(reloj):
    svc = IdentityService(InMemoryEventStore())
    _, k1, c1 = _alta(svc, "a")
    k2, c2 = _rotar(svc, c1, k1)
    k3 = k2.successor()
    with pytest.raises(IdentityError):
        svc.rotate_credential(c1.credential_id, k3.signing_pub_b64,
                              k3.encryption_pub_b64)
    c3 = svc.rotate_credential(c2.credential_id, k3.signing_pub_b64,
                               k3.encryption_pub_b64)
    assert c3.credential_id not in (c1.credential_id, c2.credential_id)


def test_rotation_survives_a_restart(reloj):
    store = InMemoryEventStore()
    svc = IdentityService(store, rotation_grace_s=900)
    _, k1, c1 = _alta(svc, "a")
    _, c2 = _rotar(svc, c1, k1)
    nuevo = IdentityService.rebuild(store, rotation_grace_s=900)
    nuevo.assert_credential_valid(c1.credential_id)
    reloj.avanzar(901)
    with pytest.raises(IdentityError):
        nuevo.assert_credential_valid(c1.credential_id)
    nuevo.assert_credential_valid(c2.credential_id)


def test_revocar_la_instancia_revoca_la_vieja_y_la_nueva(reloj):
    svc = IdentityService(InMemoryEventStore())
    inst, k1, c1 = _alta(svc, "a")
    _, c2 = _rotar(svc, c1, k1)
    svc.revoke_instance(inst.instance_id, "comprometida")
    for c in (c1, c2):
        with pytest.raises(IdentityError):
            svc.assert_credential_valid(c.credential_id)


def test_new_senders_encrypt_to_the_new_key(reloj):
    svc = IdentityService(InMemoryEventStore(), rotation_grace_s=900)
    _, k1, c1 = _alta(svc, "a")
    k2, _ = _rotar(svc, c1, k1)
    assert svc.encryption_pub_for_agent("a") == k2.encryption_pub_b64
    reloj.avanzar(901)
    assert svc.encryption_pub_for_agent("a") == k2.encryption_pub_b64


def _mensaje_en_vuelo():
    """B encrypts a message to A's key BEFORE A rotates; A rotates; the
    message arrives afterwards."""
    store = InMemoryEventStore()
    ids = IdentityService(store)
    ms = MessageService(ids, store)
    _, ka1, ca1 = _alta(ids, "a")
    _, kb, cb = _alta(ids, "b")
    env = build_envelope_for(
        ca1.encryption_pub, sender_keys=kb, from_agent="b",
        from_instance=cb.instance_id, credential_id=cb.credential_id,
        to_agent="a", plaintext="en vuelo", sequence=1)
    return ids, ms, ka1, ca1, env


def test_the_recipient_decrypts_an_in_flight_message_encrypted_to_the_previous_key():
    ids, ms, ka1, ca1, env = _mensaje_en_vuelo()
    ka2 = ka1.successor()
    ids.rotate_credential(ca1.credential_id, ka2.signing_pub_b64,
                          ka2.encryption_pub_b64)
    ms.send(env)
    assert ms.receive(ka2, env) == b"en vuelo"


def test_the_previous_key_is_dropped_after_grace_ends():
    ids, ms, ka1, ca1, env = _mensaje_en_vuelo()
    ka2 = ka1.successor(grace_s=0)
    ids.rotate_credential(ca1.credential_id, ka2.signing_pub_b64,
                          ka2.encryption_pub_b64)
    ms.send(env)
    with pytest.raises(InvalidTag):
        ms.receive(ka2, env)


def test_the_recipient_rejects_a_signature_after_grace_ends():
    ids = IdentityService(InMemoryEventStore())
    _, _, ca = _alta(ids, "a")
    _, kb, cb = _alta(ids, "b")
    env = build_envelope_for(
        ca.encryption_pub, sender_keys=kb, from_agent="b",
        from_instance=cb.instance_id, credential_id=cb.credential_id,
        to_agent="a", plaintext="x", sequence=1)
    verify_envelope(env, cb)                              # vigente: pasa
    cb.grace_until = datetime.now(UTC) - timedelta(seconds=1)
    with pytest.raises(Rejection) as exc:
        verify_envelope(env, cb)
    assert exc.value.reason == RejectionReason.STALE_TS


def test_the_receipt_finds_messages_after_more_than_10000_sends():
    store = InMemoryEventStore()
    ids = IdentityService(store)
    _, _, ca = _alta(ids, "a")
    _, kb, cb = _alta(ids, "b")
    for i in range(10_001):
        store.append(EventDraft(
            type=EventType.MESSAGE_SENT, agent_id="b",
            payload={"message_id": f"relleno_{i}", "to": "a",
                     "sequence": 0}), stream="comms")
    ms = MessageService(ids, store)
    env = build_envelope_for(
        ca.encryption_pub, sender_keys=kb, from_agent="b",
        from_instance=cb.instance_id, credential_id=cb.credential_id,
        to_agent="a", plaintext="hola", sequence=1)
    ms.send(env)
    ms2 = MessageService(ids, store)
    ms2.restore_from_log()
    assert ms2.acknowledge(env.header.message_id, env.body_hash) is not None

def test_prune_credentials_drops_dead_ones(reloj):
    store = InMemoryEventStore()
    svc = IdentityService(store, rotation_grace_s=60)
    _, k1, c1 = _alta(svc, "a")
    _, c2 = _rotar(svc, c1, k1)
    reloj.avanzar(61)
    # c1 past grace, c2 still valid -> only c2 survives pruning
    assert svc.prune_credentials() == 1
    assert c1.credential_id not in svc._credentials
    svc.assert_credential_valid(c2.credential_id)


def test_rebuild_prunes_historical_dead_credentials(reloj):
    store = InMemoryEventStore()
    svc = IdentityService(store, rotation_grace_s=60)
    _, k1, c1 = _alta(svc, "a")
    _, c2 = _rotar(svc, c1, k1)
    svc.revoke_credential(c2.credential_id, "test")
    reloj.avanzar(61)
    nuevo = IdentityService.rebuild(store, rotation_grace_s=60)
    assert len(nuevo._credentials) == 0
