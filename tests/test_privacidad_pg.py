import os
import uuid

import pytest

from examples.demo_crm_billing import CONDICION, DATOS
from vexinter.core.events import EventType
from vexinter.core.runtime import LocalRuntime
from vexinter.core.store import EventQuery
from vexinter.demo import BillingAdapter, CrmAdapter
from vexinter.privacy import InMemoryKeyRing, PrivacyStore
from vexinter.sdk import Task


def _almacen():
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


def _alta(ps, email, objetivo):
    rt = LocalRuntime([CrmAdapter(), BillingAdapter()], store=ps)
    with Task(rt, agent="a", objective=objetivo, condition=CONDICION,
              input={"email": email}) as t:
        c = t.tools.crm.create_customer({**DATOS, "email": email})
        t.tools.billing.create_invoice({"customer_id": c.id, "amount": 100})
        assert t.verify().status == "met"
        return t.task_id, t.execution_id


def _apariciones(inner, execution_id, texto):
    with inner._pool.connection() as conn:
        return conn.execute(
            "SELECT count(*) FROM vex_event WHERE execution_id = %s "
            "AND position(%s in payload::text) > 0",
            (execution_id, texto)).fetchone()[0]


def _leido(almacen, eid):
    return str([e.payload for e in almacen.query(
        EventQuery(execution_id=eid, limit=100_000))])


def test_personal_content_does_not_stay_cleartext_in_the_database():
    inner, _ring, ps = _almacen()
    try:
        email = f"priv-{uuid.uuid4().hex}@ejemplo.com"
        objetivo = f"Alta privada {uuid.uuid4().hex}"
        tid, eid = _alta(ps, email, objetivo)
        assert _apariciones(inner, eid, email) == 0
        assert _apariciones(inner, eid, objetivo) == 0
        assert email in _leido(ps, eid)
        assert ps.verify_chain(f"task:{tid}").ok
    finally:
        inner.close()


def test_borrar_en_postgres_destruye_el_contenido_y_conserva_la_cadena():
    inner, _ring, ps = _almacen()
    try:
        email = f"priv-{uuid.uuid4().hex}@ejemplo.com"
        tid, eid = _alta(ps, email, "Alta privada")
        antes = len(inner.query(EventQuery(execution_id=eid, limit=100_000)))
        assert ps.erase(eid, reason="pedido del titular") is True
        evs = ps.query(EventQuery(execution_id=eid, limit=100_000))
        assert len(evs) == antes
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


def test_otra_instancia_solo_lee_si_comparte_el_llavero():
    inner, ring, ps = _almacen()
    try:
        email = f"priv-{uuid.uuid4().hex}@ejemplo.com"
        _tid, eid = _alta(ps, email, "Alta privada")
        otro = PrivacyStore(inner, ring)
        assert email in _leido(otro, eid)
        sin_llave = PrivacyStore(inner, InMemoryKeyRing())
        assert email not in _leido(sin_llave, eid)
    finally:
        inner.close()