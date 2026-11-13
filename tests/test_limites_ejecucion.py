"""Execution guardrails and task-creation semantics.

Covers: idempotent task creation (network-cut retries must not pay
twice), validation rejections never consuming quota, per-task budget
propagation to invocations, conservative step/deadline defaults,
cycle + no-progress detection, and bounded budget-inheriting subtask
delegation (depth and per-task count limits).
"""
import pytest
from fastapi.testclient import TestClient

from services.app import make_app
from vexinter.core.events import EventType
from vexinter.core.store import EventQuery, InMemoryEventStore
from vexinter.core.tiers import Tier, TierManager, get_tier_manager, set_tier_manager
from vexinter.demo import BillingSystem, CrmSystem

COND = {"op": "exists", "system": "crm",
        "query": {"type": "customer", "where": {"email": "a@b.c"}}}
EMAIL = "community@vexinter.com"


def _app(store=None):
    store = store or InMemoryEventStore()
    # get_tier_manager is a process-wide singleton: bind a fresh manager
    # to this test's store so usage counters don't leak between tests.
    set_tier_manager(TierManager(store=store))
    return make_app(store=store,
                    adapters=[CrmSystem(), BillingSystem()],
                    dev_mode=True, admin_token="tok")


def _task(c, **extra):
    body = {"agent": "agente1", "objective": "do something",
            "condition": COND, "input": {}}
    body.update(extra)
    r = c.post("/v1/tasks", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _invoke(c, tid, tool, args, agent="agente1"):
    return c.post(f"/v1/tasks/{tid}/invoke",
                  json={"tool": tool, "args": args, "agent": agent})


def _usage(store):
    return get_tier_manager(store).get_usage(EMAIL, Tier.COMMUNITY).tasks_used


# ── idempotent creation ────────────────────────────────────────────────────

def test_retry_with_same_key_returns_same_task_and_pays_once():
    store = InMemoryEventStore()
    c = TestClient(_app(store))
    body = {"agent": "agente1", "objective": "do something",
            "condition": COND, "idempotency_key": "retry-1"}

    r1 = c.post("/v1/tasks", json=body)
    r2 = c.post("/v1/tasks", json=body)   # network-cut retry
    assert r1.status_code == r2.status_code == 200
    assert r2.json()["task_id"] == r1.json()["task_id"]
    assert r2.json()["deduplicated"] is True

    created = store.query(EventQuery(types={EventType.TASK_CREATED}))
    assert len(created) == 1
    assert _usage(store) == 1            # quota consumed exactly once


def test_same_key_with_different_payload_is_a_conflict():
    c = TestClient(_app())
    body = {"agent": "agente1", "objective": "do something",
            "condition": COND, "idempotency_key": "retry-2"}
    assert c.post("/v1/tasks", json=body).status_code == 200
    r = c.post("/v1/tasks",
               json={**body, "objective": "a different objective"})
    assert r.status_code == 409


def test_without_key_every_post_creates_a_task():
    c = TestClient(_app())
    t1 = _task(c)
    t2 = _task(c)
    assert t1["task_id"] != t2["task_id"]


# ── validation rejections never consume quota ─────────────────────────────

def test_invalid_condition_is_422_and_creates_nothing():
    store = InMemoryEventStore()
    c = TestClient(_app(store))
    r = c.post("/v1/tasks", json={
        "agent": "agente1", "objective": "o",
        "condition": {"op": "bogus"}, "input": {}})
    assert r.status_code == 422
    assert store.query(EventQuery(types={EventType.TASK_CREATED})) == []
    assert _usage(store) == 0


def test_missing_agent_is_400_and_consumes_nothing():
    store = InMemoryEventStore()
    c = TestClient(_app(store))
    r = c.post("/v1/tasks", json={
        "objective": "o", "condition": COND, "input": {}})
    assert r.status_code == 400
    assert _usage(store) == 0


# ── budget propagation (regression: HTTP path dropped run.budget) ─────────

def test_task_budget_reaches_the_invocation_tracker():
    """Before the fix, CallContext never carried run.budget, so
    client-supplied limits were silently ignored over HTTP."""
    c = TestClient(_app())
    t = _task(c, max_tokens=0)      # 0 tokens allowed: exceeded immediately
    r = _invoke(c, t["task_id"], "crm.create_customer",
                {"name": "Ana", "email": "ana@x.co"})
    assert r.status_code == 422    # BudgetExceededError boundary


def test_task_without_budget_gets_service_defaults_and_a_warning():
    c = TestClient(_app())
    t = _task(c)
    assert any("no explicit budget" in w for w in t["warnings"])
    # community ceiling applies even without client fields
    r = _invoke(c, t["task_id"], "crm.create_customer",
                {"name": "Ana", "email": "ana@x.co"})
    assert r.status_code == 200


def test_community_max_tokens_is_capped():
    c = TestClient(_app())
    t = _task(c, max_tokens=10_000_000)
    assert any("Community ceiling" in w for w in t["warnings"])


# ── execution limits ───────────────────────────────────────────────────────

def test_max_steps_blocks_the_next_call(monkeypatch):
    monkeypatch.setenv("VEXINTER_MAX_STEPS", "3")
    c = TestClient(_app())
    t = _task(c)
    tid = t["task_id"]
    codes = []
    for i in range(4):
        # distinct args each call: no loop/cycle detector may fire first
        r = _invoke(c, tid, "crm.create_customer",
                    {"name": f"U{i}", "email": f"u{i}@x.co"})
        codes.append(r.status_code)
    assert codes == [200, 200, 200, 429]


def test_deadline_blocks_invocations(monkeypatch):
    monkeypatch.setenv("VEXINTER_TASK_DEADLINE_S", "0")
    c = TestClient(_app())
    t = _task(c)
    r = _invoke(c, t["task_id"], "crm.create_customer",
                {"name": "Ana", "email": "ana@x.co"})
    assert r.status_code == 429


def test_cycle_detection_catches_alternating_tools(monkeypatch):
    """A-B-A-B with identical args slips past the consecutive-identical
    check; the cycle detector must catch the repeating pattern."""
    monkeypatch.setenv("VEXINTER_CYCLE_THRESHOLD", "2")
    monkeypatch.setenv("VEXINTER_LOOP_THRESHOLD", "100")
    monkeypatch.setenv("VEXINTER_NO_PROGRESS_WINDOW", "100")
    monkeypatch.setenv("VEXINTER_MAX_STEPS", "50")
    store = InMemoryEventStore()
    c = TestClient(_app(store))
    t = _task(c)
    tid = t["task_id"]
    seq = [("crm.create_customer",
            {"name": "Ana", "email": "ana@x.co"}),
           ("billing.create_invoice",
            {"customer_id": "cus_x", "amount": 10})]
    codes = [_invoke(c, tid, tool, args).status_code
             for tool, args in seq * 2]
    assert codes[-1] == 422          # LoopViolationError boundary
    loops = store.query(EventQuery(types={EventType.LOOP_DETECTED}))
    assert loops and "cycle_detected" in str(
        loops[-1].payload.get("suggested_action"))


def test_no_progress_window_catches_non_consecutive_repeats(monkeypatch):
    """Four distinct calls repeated twice: no periodic cycle (period 4
    needs 12 elems at threshold 3) but the last-4 window is all repeats."""
    monkeypatch.setenv("VEXINTER_NO_PROGRESS_WINDOW", "4")
    monkeypatch.setenv("VEXINTER_LOOP_THRESHOLD", "100")
    monkeypatch.setenv("VEXINTER_CYCLE_THRESHOLD", "100")
    monkeypatch.setenv("VEXINTER_MAX_STEPS", "50")
    store = InMemoryEventStore()
    c = TestClient(_app(store))
    t = _task(c)
    tid = t["task_id"]
    # Four distinct calls that all fail identically (archiving
    # nonexistent customers): repeating them twice yields a full window
    # of previously-seen outcomes — no progress.
    seq = [
        ("crm.archive_customer", {"customer_id": f"nope{i}"})
        for i in range(4)
    ]
    codes = [_invoke(c, tid, tool, args).status_code
             for tool, args in seq * 2]
    assert codes[:7] == [200] * 7
    assert codes[7] == 422           # no-progress violation on call 8
    loops = store.query(EventQuery(types={EventType.LOOP_DETECTED}))
    assert loops and "no_progress" in str(
        loops[-1].payload.get("suggested_action"))


# ── subtasks: inherit budget, bounded depth and count, no quota ───────────

def test_subtasks_inherit_parent_budget(monkeypatch):
    c = TestClient(_app())
    t = _task(c, max_tokens=12345)
    r = c.post(f"/v1/tasks/{t['task_id']}/subtasks", json={
        "agent_id": "agente2", "objective": "sub", "condition": COND})
    assert r.status_code == 200
    sid = r.json()["subtask_id"]
    g = c.get(f"/v1/tasks/{t['task_id']}/subtasks/{sid}")
    assert g.json()["budget"]["max_tokens"] == 12345
    assert g.json()["depth"] == 1


def test_subtask_count_is_bounded(monkeypatch):
    monkeypatch.setenv("VEXINTER_MAX_SUBTASKS_PER_TASK", "3")
    c = TestClient(_app())
    t = _task(c)
    for _ in range(3):
        r = c.post(f"/v1/tasks/{t['task_id']}/subtasks", json={
            "agent_id": "agente2", "objective": "sub",
            "condition": COND})
        assert r.status_code == 200
    r = c.post(f"/v1/tasks/{t['task_id']}/subtasks", json={
        "agent_id": "agente2", "objective": "sub", "condition": COND})
    assert r.status_code == 429


def test_cascading_delegation_is_bounded_by_depth(monkeypatch):
    monkeypatch.setenv("VEXINTER_MAX_SUBTASK_DEPTH", "2")
    monkeypatch.setenv("VEXINTER_MAX_SUBTASKS_PER_TASK", "50")
    c = TestClient(_app())
    t = _task(c)
    s1 = c.post(f"/v1/tasks/{t['task_id']}/subtasks", json={
        "agent_id": "agente2", "objective": "s1", "condition": COND}
    ).json()["subtask_id"]
    s2 = c.post(f"/v1/tasks/{s1}/subtasks", json={
        "agent_id": "agente3", "objective": "s2", "condition": COND}
    ).json()["subtask_id"]
    assert c.get(f"/v1/tasks/{t['task_id']}/subtasks/{s2}"
                 ).json()["depth"] == 2
    # depth 3 exceeds the limit of 2
    r = c.post(f"/v1/tasks/{s2}/subtasks", json={
        "agent_id": "agente4", "objective": "s3", "condition": COND})
    assert r.status_code == 429


def test_subtasks_do_not_consume_task_quota():
    store = InMemoryEventStore()
    c = TestClient(_app(store))
    t = _task(c)
    for _ in range(5):
        r = c.post(f"/v1/tasks/{t['task_id']}/subtasks", json={
            "agent_id": "agente2", "objective": "sub",
            "condition": COND})
        assert r.status_code == 200
    assert _usage(store) == 1     # only the parent task consumed quota


# ── quota visibility ───────────────────────────────────────────────────────

def test_quota_warning_at_80_percent():
    from vexinter.core.tiers import TIER_DEFINITIONS, TierLimits

    original = TIER_DEFINITIONS[Tier.COMMUNITY]
    TIER_DEFINITIONS[Tier.COMMUNITY] = TierLimits(
        name=Tier.COMMUNITY, monthly_task_limit=10, hard_block=True,
        overage_per_1k_usd=0.0, monthly_price_usd=0.0)
    try:
        store = InMemoryEventStore()
        c = TestClient(_app(store))
        early = _task(c)
        assert not any("monthly quota" in w
                       for w in early.get("warnings", []))
        for _ in range(6):           # now at 7/10
            _task(c)
        at_80 = _task(c)             # 8/10 = 80%
        assert any("monthly quota 8/10" in w
                   for w in at_80.get("warnings", []))
    finally:
        TIER_DEFINITIONS[Tier.COMMUNITY] = original
