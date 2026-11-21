"""Tests for outbound notification dispatch (vexinter.core.notify)."""

import asyncio
import json

import pytest

from vexinter.core.events import EventDraft, EventType
from vexinter.core.notify import (
    Notification,
    NotificationDispatcher,
    WebhookChannel,
    summarize,
)
from vexinter.core.store import InMemoryEventStore


def _draft(t: EventType, **kw) -> EventDraft:
    return EventDraft(type=t, **kw)


class _Capture:
    name = "capture"

    def __init__(self):
        self.items = []

    async def send(self, n: Notification) -> bool:
        self.items.append(n)
        return True


def test_dispatcher_delivers_matching_events():
    store = InMemoryEventStore()
    cap = _Capture()
    disp = NotificationDispatcher(
        [cap], event_types={EventType.EXECUTION_COMPLETED})
    orig = store.append

    def append(d, s):
        ev = orig(d, s)
        disp.on_event(ev)
        return ev

    store.append = append
    store.append(_draft(EventType.TASK_CREATED, task_id="t1"), "s")
    store.append(_draft(EventType.EXECUTION_COMPLETED,
                        task_id="t1", payload={"result": "ok"}), "s")

    async def drain():
        await disp.start()
        await asyncio.wait_for(disp._queue.join(), 2)
        await disp.stop()

    asyncio.run(drain())
    assert len(cap.items) == 1
    n = cap.items[0]
    assert n.event_type == "execution.completed"
    assert n.task_id == "t1"
    assert "completed" in n.summary


def test_dispatcher_filters_and_queue_full_drops():
    cap = _Capture()
    disp = NotificationDispatcher(
        [cap], event_types={EventType.TOOL_REJECTED}, max_queue=2)
    store = InMemoryEventStore()
    orig = store.append

    def append(d, s):
        ev = orig(d, s)
        disp.on_event(ev)
        return ev

    store.append = append
    for i in range(5):
        store.append(_draft(EventType.TOOL_REJECTED,
                            payload={"tool": f"t{i}"}), "s")
        store.append(_draft(EventType.TASK_CREATED), "s")  # filtered out
    assert disp._queue.qsize() <= 2
    assert disp.dropped >= 3


def test_summarize_includes_verdict_and_tool():
    store = InMemoryEventStore()
    ev = store.append(_draft(
        EventType.VERIFIER_VERDICT, task_id="t9",
        payload={"verdict": "mismatch", "tool": "charge"}), "s")
    s = summarize(ev)
    assert "verdict=mismatch" in s and "tool=charge" in s
    assert "t9" in s


def test_webhook_channel_posts_slack_compatible_json(monkeypatch):
    sent = {}

    class _Resp:
        status_code = 200

    class _Client:
        def __init__(self, timeout):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            sent["url"] = url
            sent["body"] = json
            return _Resp()

    monkeypatch.setattr(
        "vexinter.core.notify.httpx.AsyncClient", _Client)
    ch = WebhookChannel("https://hooks.slack.com/services/X/Y/Z")
    n = Notification(
        event_type="execution.completed", summary="done",
        task_id="t", execution_id=None, agent_id=None,
        event_id="e", ts=__import__("datetime").datetime.now(
            __import__("datetime").UTC))
    assert asyncio.run(ch.send(n)) is True
    assert sent["body"]["text"].startswith("[execution.completed]")
    assert sent["body"]["content"] == sent["body"]["text"]
    assert sent["body"]["task_id"] == "t"


def test_webhook_channel_failure_is_swallowed(monkeypatch):
    class _Client:
        def __init__(self, timeout):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            raise OSError("connection refused")

    monkeypatch.setattr(
        "vexinter.core.notify.httpx.AsyncClient", _Client)
    ch = WebhookChannel("http://127.0.0.1:1/x")
    n = Notification(
        event_type="x", summary="s", task_id=None, execution_id=None,
        agent_id=None, event_id="e",
        ts=__import__("datetime").datetime.now(
            __import__("datetime").UTC))
    assert asyncio.run(ch.send(n)) is False
    assert ch.failed == 1
