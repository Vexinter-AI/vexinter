import pytest
from fastapi.testclient import TestClient

from services.app import make_app
from vexinter.core.store import InMemoryEventStore
from vexinter.demo import CrmSystem
from vexinter.identity import (
    AgentKeyMaterial,
    IdentityService,
    MessageService,
    Rejection,
    RejectionReason,
    build_envelope_for,
)


def _consola():
    return make_app(store=InMemoryEventStore(), adapters=[CrmSystem()],
                    dev_mode=True)


def test_the_console_does_not_turn_an_empty_field_into_unlimited_access():
    app = _consola()
    c = TestClient(app)
    r = c.post("/console/agents/enroll",
               data={"agent_id": "vacio", "allowed_tools": "",
                     "allowed_recipients": ""})
    assert r.status_code == 200
    a = app.state.runtime.identity.identity("vacio")
    assert a.allowed_tools == set() and a.allowed_recipients == set()


def test_the_console_does_not_insert_external_data_with_innerhtml():
    html = TestClient(_consola()).get("/console/cases").text
    assert "innerHTML" not in html


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
    keys["a"].decrypt_body(forjado)        # cualquiera puede cifrar para A
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