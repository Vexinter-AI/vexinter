import pytest

from vexinter.core.store import InMemoryEventStore
from vexinter.identity import (
    AgentKeyMaterial,
    IdentityService,
    MessageService,
    Rejection,
    RejectionReason,
    build_envelope_for,
)


def _net():
    store = InMemoryEventStore()
    ids = IdentityService(store)
    ms = MessageService(ids, store)
    keys, creds = {}, {}
    for ag in ("a", "b"):
        ids.enroll_agent(ag, allowed_tools=set(),
                         allowed_recipients={"a", "b"})
        inst = ids.spawn_instance(ag)
        k = AgentKeyMaterial()
        creds[ag] = ids.issue_credential(inst.instance_id, k.signing_pub_b64,
                                         k.encryption_pub_b64)
        keys[ag] = k
    return ms, keys, creds


def _envelope(creds, signer, text="hello", seq=1):
    return build_envelope_for(
        creds["a"].encryption_pub, sender_keys=signer, from_agent="b",
        from_instance=creds["b"].instance_id,
        credential_id=creds["b"].credential_id, to_agent="a",
        plaintext=text, sequence=seq)


def test_the_recipient_accepts_a_legitimate_message():
    ms, keys, creds = _net()
    env = ms.send(_envelope(creds, keys["b"]))
    assert ms.receive(keys["a"], env) == b"hello"


def test_the_recipient_rejects_a_forged_envelope_even_if_it_can_be_decrypted():
    ms, keys, creds = _net()
    forjado = _envelope(creds, AgentKeyMaterial(), text="wire everything")
    keys["a"].decrypt_body(forjado)        # anyone can encrypt for A
    with pytest.raises(Rejection) as exc:
        ms.receive(keys["a"], forjado)
    assert exc.value.reason == RejectionReason.BAD_SIGNATURE


def test_the_recipient_rejects_an_unknown_credential():
    ms, keys, creds = _net()
    env = _envelope(creds, keys["b"])
    env.header.from_credential = "cred_inexistente"
    with pytest.raises(Rejection) as exc:
        ms.receive(keys["a"], env)
    assert exc.value.reason == RejectionReason.INVALID_CREDENTIAL