"""Outbound notification dispatch.

Subscribes to the event store and fans noteworthy events out to
configured channels (generic webhooks, which covers Slack incoming
webhooks, Discord, and custom receivers, with room for more channel
types such as WhatsApp later).

Delivery is best-effort and asynchronous: the dispatcher never blocks
the append path. Events are queued (bounded); if the queue is full the
notification is dropped and counted, never the event itself.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

import httpx

from .events import Event, EventType

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Notification:
    """A user-facing notification derived from a store event."""

    event_type: str
    summary: str
    task_id: str | None
    execution_id: str | None
    agent_id: str | None
    event_id: str
    ts: datetime
    data: dict[str, Any] = field(default_factory=dict)


class NotificationChannel(Protocol):
    """Outbound channel. Implementations must be cheap to call and must
    not raise, failures are logged and counted internally."""

    name: str

    async def send(self, n: Notification) -> bool:
        ...


_SUMMARY_KEYS = {
    EventType.EXECUTION_COMPLETED: "execution completed",
    EventType.VERIFIER_VERDICT: "verifier verdict",
    EventType.DISCREPANCY_DETECTED: "discrepancy detected",
    EventType.RECOVERY_DECIDED: "recovery decided",
    EventType.RECOVERY_EXECUTED: "recovery executed",
    EventType.HUMAN_DECISION_REQUESTED: "human decision requested",
    EventType.CASE_RESOLVED: "case resolved",
    EventType.TOOL_REJECTED: "tool call rejected",
    EventType.MESSAGE_REJECTED: "message rejected",
    EventType.EXECUTION_LIMIT_EXCEEDED: "execution limit exceeded",
    EventType.LOOP_DETECTED: "loop detected",
    EventType.PRIVACY_ERASED: "privacy erasure",
    EventType.HITL_REQUESTED: "human approval requested",
}


def _clip(v: Any, n: int = 160) -> str:
    s = v if isinstance(v, str) else json.dumps(v, default=str)
    return s if len(s) <= n else s[: n - 1] + "…"


def summarize(ev: Event) -> str:
    """One-line human-readable summary for an event."""
    base = _SUMMARY_KEYS.get(ev.type, ev.type.value)
    p = ev.payload or {}
    bits: list[str] = []
    for key in ("verdict", "reason", "tool", "decision", "state", "to",
                "limit", "kind"):
        if key in p:
            bits.append(f"{key}={_clip(p[key], 80)}")
    if ev.type == EventType.VERIFIER_VERDICT:
        v = p.get("verdict") or p.get("result")
        if v is not None:
            bits.insert(0, f"verdict={_clip(v, 80)}")
    suffix = f" ({'; '.join(bits)})" if bits else ""
    who = ev.agent_id or ev.task_id or ev.execution_id or "system"
    return f"{base}: {who}{suffix}"


class WebhookChannel:
    """POSTs a notification as JSON to a URL.

    Payload carries Slack-style `text`, Discord-style `content`, and
    structured fields, so Slack and Discord incoming-webhook URLs work
    as-is and generic receivers can use the structured fields.
    """

    def __init__(self, url: str, *, name: str = "webhook",
                 headers: dict[str, str] | None = None,
                 timeout: float = 10.0) -> None:
        self.name = name
        self.url = url
        self._headers = {"Content-Type": "application/json",
                         **(headers or {})}
        self._timeout = timeout
        self.sent = 0
        self.failed = 0

    async def send(self, n: Notification) -> bool:
        text = f"[{n.event_type}] {n.summary}"
        body = {
            "text": text,      # Slack incoming webhooks
            "content": text,   # Discord webhooks
            "event_type": n.event_type,
            "summary": n.summary,
            "task_id": n.task_id,
            "execution_id": n.execution_id,
            "agent_id": n.agent_id,
            "event_id": n.event_id,
            "ts": n.ts.isoformat(),
            "data": n.data,
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                r = await client.post(self.url, json=body,
                                      headers=self._headers)
            if r.status_code < 300:
                self.sent += 1
                return True
            self.failed += 1
            log.warning("notify webhook %s -> HTTP %s", self.name,
                        r.status_code)
            return False
        except Exception as e:  # noqa: BLE001 - network/timeout, never propagate
            self.failed += 1
            log.warning("notify webhook %s failed: %s", self.name, e)
            return False


class NotificationDispatcher:
    """Queues noteworthy events and delivers them to channels.

    `on_event` is called synchronously from the store append path, it
    must be cheap (it is: filter + bounded put_nowait).
    """

    def __init__(self, channels: list[NotificationChannel],
                 event_types: set[EventType] | None = None,
                 max_queue: int = 1000) -> None:
        self.channels = channels
        self.event_types = event_types
        self._queue: asyncio.Queue[Notification] = asyncio.Queue(
            maxsize=max_queue)
        self._task: asyncio.Task | None = None
        self.dropped = 0
        self.delivered = 0

    def on_event(self, ev: Event) -> None:
        if self.event_types is not None and ev.type not in self.event_types:
            return
        n = Notification(
            event_type=ev.type.value,
            summary=summarize(ev),
            task_id=ev.task_id,
            execution_id=ev.execution_id,
            agent_id=ev.agent_id,
            event_id=ev.id,
            ts=ev.ts,
            data=ev.payload or {},
        )
        try:
            self._queue.put_nowait(n)
        except asyncio.QueueFull:
            self.dropped += 1

    async def _worker(self) -> None:
        while True:
            n = await self._queue.get()
            try:
                for ch in self.channels:
                    await ch.send(n)
                self.delivered += 1
            except Exception:
                log.exception("notify dispatch failed for %s", n.event_id)
            finally:
                self._queue.task_done()

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._worker())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        # Flush whatever is still queued (bounded by max_queue).
        while not self._queue.empty():
            try:
                n = self._queue.get_nowait()
                for ch in self.channels:
                    await ch.send(n)
                self._queue.task_done()
            except asyncio.QueueEmpty:
                break
            except Exception:
                log.exception("notify flush failed")
