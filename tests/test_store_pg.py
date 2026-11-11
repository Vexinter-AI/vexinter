import os
from concurrent.futures import ThreadPoolExecutor

import pytest

from vexinter.core.events import EventDraft, EventType
from vexinter.core.store import EventQuery
from vexinter.store_pg import PostgresEventStore, init_schema


def _store():
    dsn = os.environ.get("VEXINTER_TEST_PG_DSN")
    if not dsn:
        pytest.skip("requiere VEXINTER_TEST_PG_DSN (testcontainers)")
    store = PostgresEventStore(dsn)
    init_schema(store)
    with store._pool.connection() as conn:
        conn.execute("TRUNCATE vex_event, vex_stream_head, "
                     "vex_checkpoint, vex_api_seq, vex_api_nonce, "
                     "vex_tenant_member, vex_tenant_agent, "
                     "vex_tenant_usage, vex_tenant, vex_tier_usage")
    return store


def test_anexos_concurrentes_producen_una_cadena_valida():
    store = _store()
    try:
        def worker(i):
            for n in range(20):
                store.append(EventDraft(type=EventType.TOOL_INVOKED,
                                        payload={"worker": i, "n": n}),
                              stream="task:conc")

        with ThreadPoolExecutor(max_workers=8) as ex:
            list(ex.map(worker, range(8)))

        audit = store.verify_chain("task:conc")
        assert audit.ok
        assert audit.events_checked == 160
        assert store.head("task:conc")[0] == 160
    finally:
        store.close()


def test_append_y_query_filtran_correctamente():
    store = _store()
    try:
        task_id = "task_test_1"
        exec_id = "exec_test_1"
        agent_id = "agent_test_1"
        
        store.append(EventDraft(
            type=EventType.TASK_CREATED, task_id=task_id,
            execution_id=exec_id, agent_id=agent_id,
            payload={"foo": "bar"}), stream=f"task:{task_id}")
        store.append(EventDraft(
            type=EventType.TOOL_INVOKED, task_id=task_id,
            execution_id=exec_id, agent_id=agent_id,
            payload={"tool": "test"}), stream=f"task:{task_id}")
        
        from vexinter.core.store import EventQuery
        evs = store.query(EventQuery(task_id=task_id, limit=100))
        assert len(evs) == 2
    finally:
        store.close()


def test_verify_chain_detecta_alteracion():
    store = _store()
    try:
        store.append(EventDraft(type=EventType.TASK_CREATED, task_id="t1",
                                execution_id="e1", agent_id="a1"),
                      stream="task:t1")
        
        # Corrupt this_hash directly in the DB
        with store._pool.connection() as conn:
            conn.execute("UPDATE vex_event SET this_hash = 'badhash'")
        
        audit = store.verify_chain("task:t1")
        assert not audit.ok
        assert len(audit.problems) > 0
    finally:
        store.close()


def test_checkpoint_y_verificacion():
    store = _store()
    try:
        store.append(EventDraft(type=EventType.TASK_CREATED, task_id="t1",
                                execution_id="e1", agent_id="a1"),
                      stream="task:t1")
        cp = store.take_checkpoint()
        assert "id" in cp
        assert "heads" in cp
    finally:
        store.close()


def test_init_schema_rechaza_base_mas_nueva():
    """A deploy with old code on a DB migrated by new code must
    fail explicitly, not corrupt the schema."""
    from vexinter.store_pg import SCHEMA_VERSION, init_schema
    store = _store()
    try:
        with store._pool.connection() as conn:
            conn.execute(
                "INSERT INTO vex_schema_migrations (version) "
                "VALUES (%s) ON CONFLICT DO NOTHING",
                (SCHEMA_VERSION + 1,))
        with pytest.raises(RuntimeError, match="newer than this code"):
            init_schema(store)
    finally:
        with store._pool.connection() as conn:
            conn.execute(
                "DELETE FROM vex_schema_migrations WHERE version = %s",
                (SCHEMA_VERSION + 1,))
        store.close()


def test_tenant_state_survives_restart():
    """TenantService state (tenants, members, agent bindings, daily usage)
    must be restored from Postgres on a fresh instance — a restart cannot
    silently drop tenants or reset quota counters."""
    from vexinter.core.multitenancy import TenantRole, TenantService, TenantTier
    store = _store()
    try:
        svc = TenantService(store)
        t = svc.create_tenant("acme", tier=TenantTier.STARTER,
                              owner_user_id="user_1")
        svc.add_member(t.tenant_id, "dev_1", {TenantRole.DEVELOPER})
        svc.assign_agent(t.tenant_id, "agent_1")
        svc.record_execution(t.tenant_id)
        svc.record_execution(t.tenant_id)
        assert svc.executions_today(t.tenant_id) == 2
        tid = t.tenant_id

        # Simulate a process restart: brand-new service over the same DB.
        svc2 = TenantService(store)
        t2 = svc2.get_tenant(tid)
        assert t2 is not None and t2.name == "acme"
        assert t2.tier == TenantTier.STARTER
        assert svc2.get_tenant_by_user("user_1") is t2
        assert svc2.get_member(tid, "dev_1") is not None
        assert svc2.tenant_of_agent("agent_1") is t2
        assert svc2.executions_today(tid) == 2

        # Usage keeps accumulating atomically across instances.
        svc2.record_execution(tid)
        assert TenantService(store).executions_today(tid) == 3
    finally:
        store.close()

def test_tenant_agent_tenant_id_index_exists():
    """M-11: agents_of_tenant no debe hacer full scan sobre vex_tenant_agent."""
    store = _store()
    try:
        with store._pool.connection() as conn:
            rows = conn.execute(
                "SELECT indexname FROM pg_indexes "
                "WHERE tablename = 'vex_tenant_agent'").fetchall()
        names = {r[0] for r in rows}
        assert "idx_vex_tenant_agent_tenant" in names
    finally:
        store.close()


def test_append_state_transition_es_atomico():
    """La transición se valida y anexa dentro del FOR UPDATE del stream."""
    from concurrent.futures import ThreadPoolExecutor

    from vexinter.core.events import EventDraft, EventType
    from vexinter.core.statemachine import ExecutionState, StateTransitionError
    store = _store()
    try:
        def trans(to):
            return store.append_state_transition(
                EventDraft(type=EventType.EXECUTION_STATE_CHANGED,
                           task_id="t", execution_id="e1",
                           payload={"to": to, "reason": "test"}),
                "s1", "e1", to)

        # in_progress -> satisfied es válida; satisfied -> provisionally no.
        trans(ExecutionState.SATISFIED)
        with pytest.raises(StateTransitionError):
            trans(ExecutionState.PROVISIONALLY_SATISFIED)

        # 8 writers concurrentes a estados válidos desde SATISFIED:
        # todos los eventos quedan en la cadena, ninguno corrupto.
        # PENDING_HUMAN no es válido desde SATISFIED -> INCONCLUSIVE en su
        # lugar (lista construida ya válida, sin mutación in-place).
        targets = [ExecutionState.INCONCLUSIVE, ExecutionState.FAILED,
                   ExecutionState.PENDING_RECOVERY, ExecutionState.SATISFIED,
                   ExecutionState.INCONCLUSIVE, ExecutionState.FAILED,
                   ExecutionState.INCONCLUSIVE, ExecutionState.SATISFIED]
        with ThreadPoolExecutor(max_workers=8) as ex:
            evs = list(ex.map(trans, targets))
        assert len(evs) == 8
        audit = store.verify_chain("s1")
        assert audit.ok
    finally:
        store.close()


def test_tenant_usage_counter_atomic_under_concurrency():
    """ON CONFLICT ... count+1 must not lose increments when many threads
    (and separate service instances) record executions at once."""
    from concurrent.futures import ThreadPoolExecutor

    from vexinter.core.multitenancy import TenantService, TenantTier
    store = _store()
    try:
        svc = TenantService(store)
        t = svc.create_tenant("acme", tier=TenantTier.STARTER,
                              owner_user_id="user_1")
        svc2 = TenantService(store)   # second "process" view
        N = 40
        with ThreadPoolExecutor(max_workers=8) as ex:
            list(ex.map(lambda i: (svc if i % 2 else svc2)
                        .record_execution(t.tenant_id), range(N)))
        assert svc.executions_today(t.tenant_id) == N
        assert svc2.executions_today(t.tenant_id) == N
    finally:
        store.close()


def test_payload_eq_handles_json_scalars():
    """payload->> yields lowercase JSON text: True must match 'true',
    not 'True' — regression for bool filters on Postgres."""
    store = _store()
    try:
        init_schema(store)
        store.append(EventDraft(
            type=EventType.MESSAGE_SENT, task_id="t", execution_id="e",
            payload={"to": "agent1", "flag": True, "n": 3}), stream="s")
        store.append(EventDraft(
            type=EventType.MESSAGE_SENT, task_id="t", execution_id="e",
            payload={"to": "agent2", "flag": False, "n": 3}), stream="s")
        hits = store.query(EventQuery(
            types={EventType.MESSAGE_SENT}, payload_eq={"flag": True}))
        assert len(hits) == 1 and hits[0].payload["to"] == "agent1"
        hits = store.query(EventQuery(
            types={EventType.MESSAGE_SENT}, payload_eq={"to": "agent2"}))
        assert len(hits) == 1
    finally:
        store.close()


def test_tier_usage_hard_block_atomic_across_managers():
    """Community hard-block (100 tasks) must hold under concurrent
    consume_task calls split across two TierManager instances sharing
    the same Postgres pool — no check-then-record TOCTOU."""
    from vexinter.core.tiers import TierManager

    store = _store()
    try:
        init_schema(store)
        m1, m2 = TierManager(store=store), TierManager(store=store)

        # 120 consume attempts split across two manager instances —
        # exactly the 100 Community tasks may succeed.
        managers = [m1, m2] * 60
        with ThreadPoolExecutor(max_workers=8) as ex:
            results = list(ex.map(
                lambda m: m.consume_task("u@x.com", "community"), managers))
        allowed = sum(1 for ok, _ in results if ok)
        assert allowed == 100, f"expected exactly 100 allowed, got {allowed}"
        from vexinter.core.tiers import Tier
        usage = m1.get_usage("u@x.com", Tier.COMMUNITY)
        assert usage.tasks_used == 100

        # A fresh manager (post-restart) sees the persisted counters
        m3 = TierManager(store=store)
        ok, reason = m3.consume_task("u@x.com", "community")
        assert ok is False and "limit reached" in (reason or "")
    finally:
        store.close()
