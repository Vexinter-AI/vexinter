import pytest

from vexinter.core.store import InMemoryEventStore
from vexinter.identity import AgentKeyMaterial
from vexinter.identity.authorizer import IdentityAwareAuthorizer
from vexinter.identity.service import IdentityError, IdentityService


def _credencial(svc, agente, tools=None, recipients=None):
    svc.enroll_agent(agente, allowed_tools=tools,
                     allowed_recipients=recipients)
    inst = svc.spawn_instance(agente)
    k = AgentKeyMaterial()
    cred = svc.issue_credential(inst.instance_id, k.signing_pub_b64,
                                k.encryption_pub_b64)
    return inst, cred


def test_a_revoked_credential_does_not_work_again_after_a_restart():
    store = InMemoryEventStore()
    svc = IdentityService(store)
    _, cred = _credencial(svc, "a", tools={"x"}, recipients={"b"})
    svc.revoke_credential(cred.credential_id, "comprometida")
    nuevo = IdentityService.rebuild(store)
    with pytest.raises(IdentityError):
        nuevo.assert_credential_valid(cred.credential_id)


def test_an_empty_policy_stays_empty_after_a_restart():
    store = InMemoryEventStore()
    svc = IdentityService(store)
    _, cred = _credencial(svc, "a", tools=set(), recipients=set())
    assert not svc.can_invoke_tool(cred.credential_id, "crm.create_customer")
    nuevo = IdentityService.rebuild(store)
    assert not nuevo.can_invoke_tool(cred.credential_id, "crm.create_customer")
    assert not nuevo.can_communicate_with(cred.credential_id, "b")


def test_the_authorizer_requires_the_credential_to_belong_to_whoever_claims_to_act():
    svc = IdentityService(InMemoryEventStore())
    inst_b, cred_b = _credencial(svc, "b", tools={"billing.create_invoice"})
    _credencial(svc, "c", tools=set())
    aut = IdentityAwareAuthorizer(svc)
    assert aut.can_invoke("b", "billing.create_invoice",
                          instance_id=inst_b.instance_id,
                          credential_id=cred_b.credential_id)
    # C no puede actuar con la credencial de B (ni dejar el acto atribuido a C)
    assert not aut.can_invoke("c", "billing.create_invoice",
                              credential_id=cred_b.credential_id)
    # ni con una instancia que no es la de la credencial
    assert not aut.can_invoke("b", "billing.create_invoice",
                              instance_id="inst_ajena",
                              credential_id=cred_b.credential_id)