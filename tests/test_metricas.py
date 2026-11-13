import re
import sys

import pytest
from fastapi.testclient import TestClient

from services.app import make_app
from vexinter.core.events import EventDraft, EventType
from vexinter.core.store import InMemoryEventStore
from vexinter.demo import BillingSystem, CrmSystem

ADMIN = "admin-token-1234"
LINEA = re.compile(
    r'^(# (HELP|TYPE) [a-z_]+ .+'
    r'|[a-z_]+(\{[a-z_]+="([^"\\\n]|\\.)*"\})? [0-9]+(\.[0-9]+)?)$')


def _app(store=None):
    return make_app(store=store or InMemoryEventStore(),
                    adapters=[CrmSystem(), BillingSystem()],
                    dev_mode=False, admin_token=ADMIN)


def test_metrics_requires_token_in_authenticated_mode():
    c = TestClient(_app())
    assert c.get("/metrics").status_code == 403
    assert c.get("/metrics",
                 headers={"X-Vex-Admin": "otro-token-largo"}).status_code == 403
    assert c.get("/metrics",
                 headers={"X-Vex-Admin": ADMIN}).status_code == 200
    assert c.get("/metrics", headers={
        "Authorization": f"Bearer {ADMIN}"}).status_code == 200


def test_a_metrics_only_token_reads_metrics_and_nothing_else(monkeypatch):
    monkeypatch.setenv("VEXINTER_METRICS_TOKEN", "metrics-only-token")
    c = TestClient(_app())
    h = {"Authorization": "Bearer metrics-only-token"}
    assert c.get("/metrics", headers=h).status_code == 200
    assert c.get("/v1/metrics",
                 headers={"X-Vex-Admin": "metrics-only-token"}
                 ).status_code == 403


def test_a_too_short_metrics_token_is_rejected(monkeypatch):
    monkeypatch.setenv("VEXINTER_METRICS_TOKEN", "corto")
    with pytest.raises(ValueError):
        _app()


def test_rejection_reasons_do_not_break_the_format():
    store = InMemoryEventStore()
    store.append(EventDraft(
        type=EventType.TOOL_REJECTED,
        payload={"tool": "x",
                 "reason": 'invalid arguments: "x"\nvexinter_fake 999'}),
        stream="t")
    text = TestClient(_app(store)).get(
        "/metrics", headers={"X-Vex-Admin": ADMIN}).text
    for line in text.strip().split("\n"):
        assert LINEA.match(line), line
    assert "vexinter_fake 999" not in text


def test_metrics_does_not_depend_on_prometheus_client(monkeypatch):
    monkeypatch.setitem(sys.modules, "prometheus_client", None)
    r = TestClient(_app()).get("/metrics", headers={"X-Vex-Admin": ADMIN})
    assert r.status_code == 200
    assert "vexinter_executions_total" in r.text
