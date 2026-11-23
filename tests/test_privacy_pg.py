import os
import uuid

import pytest

from examples.demo_crm_billing import CONDITION, DATA
from vexinter.core.events import EventType
from vexinter.core.runtime import LocalRuntime
from vexinter.core.store import EventQuery
from vexinter.demo import BillingAdapter, CrmAdapter
from vexinter.privacy import InMemoryKeyRing, PrivacyStore
from vexinter.sdk import Task


def _storage():
    dsn = os.environ.get("VEXINTER_TEST_PG_DSN")
    if not dsn:
        pytest.skip("requiere VEXINTER_TEST_PG_DSN (testcontainers)")
    from vexinter.store_pg import PostgresEventStore, init_schema
    inner = PostgresEventStore(dsn)
    init_schema(inner)
    with inner._pool.connection() as conn:
        conn.execute("TRUNCATE vex_event, vex_stream_head, "
                     "vex_checkpoint, vex_api_seq, vex_api_nonce")
    ring = InMemoryKeyRing()
    return inner, ring, PrivacyStore(inner, ring)


def _high(ps, email, objective):
    rt = LocalRuntime([CrmAdapter(), BillingAdapter()], store=ps)
    with Task(rt, agent="a", objective=objective, condition=CONDITION,
              input={"email": email}) as t:
        c = t.tools.crm.create_customer({**DATA, "email": email})
        t.tools.billing.create_invoice({"customer_id": c.id, "amount": 100})
        assert t.verify().status == "met"
        return t.task_id, t.execution_id


def _occurrences(inner, execution_id, text):
    with inner._pool.connection() as conn:
        return conn.execute(
            "SELECT count(*) FROM vex_event WHERE execution_id = %s "
            "AND position(%s in payload::text) > 0",
            (execution_id, text)).fetchone()[0]


def _read(almacen, eid):
    return str([e.payload for e in almacen.query(
        EventQuery(execution_id=eid, limit=100_000))])


def test_personal_content_does_not_stay_cleartext_in_the_database():
    inner, _ring, ps = _storage()
    try:
        email = f"priv-{uuid.uuid4().hex}@example.com"
        objective = f"Alta private {uuid.uuid4().hex}"
        tid, eid = _high(ps, email, objective)
        assert _occurrences(inner, eid, email) == 0
        assert _occurrences(inner, eid, objective) == 0
        assert email in _read(ps, eid)
        assert ps.verify_chain(f"task:{tid}").ok
    finally:
        inner.close()


def test_delete_in_postgres_destroys_the_content_y_preserves_the_chain():
    inner, _ring, ps = _storage()
    try:
        email = f"priv-{uuid.uuid4().hex}@example.com"
        tid, eid = _high(ps, email, "Alta private")
        before = len(inner.query(EventQuery(execution_id=eid, limit=100_000)))
        assert ps.erase(eid, reason="pedido del titular") is True
        evs = ps.query(EventQuery(execution_id=eid, limit=100_000))
        assert len(evs) == before
        assert email not in str([e.payload for e in evs])
        creado = next(e for e in evs if e.type == EventType.TASK_CREATED)
        assert "input" in creado.payload["_redacted"]
        assert ps.verify_chain(f"task:{tid}").ok
        assert ps.verify_chain("privacy").ok
        borrados = [e for e in ps.query(EventQuery(
            types={EventType.PRIVACY_ERASED}, limit=100_000))
            if e.payload.get("execution_id") == eid]
        assert len(borrados) == 1
    finally:
        inner.close()


def test_other_instance_only_reads_si_shares_the_keyring():
    inner, ring, ps = _storage()
    try:
        email = f"priv-{uuid.uuid4().hex}@example.com"
        _tid, eid = _high(ps, email, "Alta private")
        other = PrivacyStore(inner, ring)
        assert email in _read(other, eid)
        no_key = PrivacyStore(inner, InMemoryKeyRing())
        assert email not in _read(no_key, eid)
    finally:
        inner.close()