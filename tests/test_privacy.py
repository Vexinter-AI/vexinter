import json

from examples.demo_crm_billing import CONDITION, DATA
from vexinter.core.events import EventDraft, EventType
from vexinter.core.runtime import LocalRuntime
from vexinter.core.store import EventQuery, InMemoryEventStore
from vexinter.demo import BillingAdapter, CrmAdapter
from vexinter.privacy import FileKeyRing, InMemoryKeyRing, PrivacyStore
from vexinter.sdk import Task

EMAIL = DATA["email"]


def _rt(billing=None):
    inner = InMemoryEventStore()
    ps = PrivacyStore(inner, InMemoryKeyRing())
    rt = LocalRuntime([CrmAdapter(), billing or BillingAdapter()], store=ps)
    return rt, ps, inner


def _task(rt):
    return Task(rt, agent="a", objective="Alta de Ana", condition=CONDITION,
                input={"email": EMAIL})


def _crudo(inner):
    return json.dumps(
        [e.payload for e in inner.query(EventQuery(limit=100_000))],
        default=str)


def _high_completes(rt):
    with _task(rt) as t:
        c = t.tools.crm.create_customer(DATA)
        t.tools.billing.create_invoice({"customer_id": c.id, "amount": 100})
        t.verify()
        return t.execution_id, t.task_id


def test_personal_content_does_not_stay_cleartext_in_the_log():
    rt, ps, inner = _rt()
    eid, _ = _high_completes(rt)
    crudo = _crudo(inner)
    assert EMAIL not in crudo and "Alta de Ana" not in crudo
    leido = json.dumps(
        [e.payload for e in ps.query(EventQuery(execution_id=eid,
                                                limit=100_000))], default=str)
    assert EMAIL in leido                    # la read normal descifra
    assert ps.verify_chain().ok


def test_the_recovery_flow_works_the_same_with_privacy():
    rt, _ps, _inner = _rt(billing=BillingAdapter(fail_first=1))
    with _task(rt) as t:
        c = t.tools.crm.create_customer(DATA)
        t.tools.billing.create_invoice({"customer_id": c.id, "amount": 100})
        assert t.verify().status == "not_met"
        t.recover(failed_tool="billing.create_invoice")
        assert t.verify().status == "met"
        assert t.audit().ok


def test_delete_destroys_content_and_chain_stays_valid():
    rt, ps, _inner = _rt()
    eid, _ = _high_completes(rt)
    assert ps.erase(eid, reason="pedido del titular") is True
    evs = ps.query(EventQuery(execution_id=eid, limit=100_000))
    assert EMAIL not in json.dumps([e.payload for e in evs], default=str)
    creado = next(e for e in evs if e.type == EventType.TASK_CREATED)
    assert "input" in creado.payload["_redacted"]
    invoked = next(e for e in evs if e.type == EventType.TOOL_INVOKED)
    assert invoked.payload["tool"] == "crm.create_customer"   # estructura
    assert "args" not in invoked.payload
    assert "args" in invoked.payload["_redacted"]
    assert any(e.type == EventType.PRIVACY_ERASED
               for e in ps.query(EventQuery(limit=100_000)))
    assert ps.verify_chain().ok


def test_an_erased_execution_is_not_re_encrypted_with_a_new_key():
    rt, ps, inner = _rt()
    eid, tid = _high_completes(rt)
    ps.erase(eid)
    ps.append(EventDraft(
        type=EventType.TOOL_INVOKED, task_id=tid, execution_id=eid,
        payload={"tool": "crm.create_customer", "args": {"email": EMAIL}}),
        stream=f"task:{tid}")
    crudo = inner.query(EventQuery(execution_id=eid,
                                   types={EventType.TOOL_INVOKED}))[-1]
    assert "_enc" not in crudo.payload
    assert crudo.payload["_redacted"] == ["args"]
    assert EMAIL not in _crudo(inner)


def test_the_file_keyring_persists_and_erases(tmp_path):
    r1 = FileKeyRing(tmp_path)
    k = r1.get_or_create("exec_1")
    assert len(k) == 32 and r1.get_or_create("exec_1") == k
    r2 = FileKeyRing(tmp_path)                      # restart
    assert r2.get("exec_1") == k
    assert r2.destroy("exec_1") is True
    r3 = FileKeyRing(tmp_path)
    assert r3.get("exec_1") is None and r3.get_or_create("exec_1") is None
    assert r3.is_erased("exec_1")
    assert not any(p.suffix == ".key" for p in tmp_path.iterdir())


def test_forget_tasks_removes_the_episodes_of_the_memory():
    rt = LocalRuntime([CrmAdapter(), BillingAdapter()])
    with Task(rt, agent="a", objective="o", condition=CONDITION,
              input={"email": EMAIL}) as t:
        t.tools.crm.create_customer(DATA)
        t.verify()
        tid = t.task_id
    assert rt.memory.retrieve(agent="a")
    rt.memory.forget_tasks([tid])
    assert rt.memory.retrieve(agent="a") == []


def test_a_payload_with_fake_enc_does_not_break_reading():
    _, ps, inner = _rt()
    inner.append(EventDraft(
        type=EventType.TOOL_INVOKED, execution_id="exec_x",
        payload={"tool": "t", "_enc": {"fields": ["args"], "n": "AAAA",
                                       "ct": "AAAA"}}), stream="task:x")
    ev = ps.query(EventQuery(execution_id="exec_x"))[0]
    assert ev.payload["_redacted"] == ["args"]

def test_e2e_envelopes_are_encrypted_under_privacy():
    _, ps, inner = _rt()
    ps.append(EventDraft(
        type=EventType.MESSAGE_SENT, execution_id="exec_msg",
        agent_id="agent_a",
        payload={"message_id": "m1", "to": "agent_b", "sequence": 1,
                 "body_hash": "h",
                 "envelope": {"header": {"from_agent": "agent_a",
                                         "to_agent": "agent_b"},
                              "ct": "cifrado"}}),
        stream="task:x")
    crudo = inner.query(EventQuery())[0].payload
    assert "envelope" not in crudo and "_enc" in crudo
    assert crudo["message_id"] == "m1" and crudo["to"] == "agent_b"
    leido = ps.query(EventQuery())[0].payload
    assert leido["envelope"]["ct"] == "cifrado"


def test_concurrent_erase_is_serialized():
    """Race en erase(): un solo PRIVACY_ERASED con had_key=True."""
    from concurrent.futures import ThreadPoolExecutor
    inner = InMemoryEventStore()
    ps = PrivacyStore(inner, InMemoryKeyRing())
    eid = "exec_race"
    ps._ring.get_or_create(eid)
    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(
            lambda i: ps.erase(eid, reason=f"r{i}"), range(16)))
    assert results.count(True) == 1
    erased = [e for e in ps.query(EventQuery(limit=100_000))
              if e.type.value == "privacy.erased"]
    assert sum(1 for e in erased if e.payload.get("had_key")) == 1


def test_erased_set_survives_keyring_restart():
    """InMemoryKeyRing pierde _erased al reiniciar; el store lo rebuilds
    from the persisted PRIVACY_ERASED events."""
    inner = InMemoryEventStore()
    ring = InMemoryKeyRing()
    ps = PrivacyStore(inner, ring)
    eid = "exec_persist"
    ps._ring.get_or_create(eid)
    assert ps.erase(eid) is True
    # simulates restart: new empty ring + new PrivacyStore over the same log
    ps2 = PrivacyStore(inner, InMemoryKeyRing())
    assert ps2.is_erased(eid) is True
    # a new event for the deleted execution must not create a new key:
    # el payload remains redactado, no cifrado con una key resucitada
    draft = EventDraft(type=EventType.TOOL_INVOKED, execution_id=eid,
                       payload={"tool": "x", "args": {"secret": 1}})
    ev = ps2.append(draft, "s")
    assert "_enc" not in ev.payload
    assert "args" in ev.payload.get("_redacted", [])
