from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from pydantic import BaseModel

from .crypto import GENESIS, canonical_json, hash_payload, sha256_hex
from .events import Event, EventDraft, EventType, new_id, now


@dataclass
class EventQuery:
    """Queries over the log (§3.4)."""
    task_id: str | None = None
    execution_id: str | None = None
    agent_id: str | None = None
    types: set[EventType] | None = None
    since: datetime | None = None
    until: datetime | None = None
    payload_eq: dict | None = None
    """Exact-match filters on top-level payload fields (e.g. {"to": agent})."""
    limit: int = 1000


class ChainAudit(BaseModel):
    ok: bool
    events_checked: int
    streams_checked: int = 0
    problems: list[str] = []


class CheckpointSigner(Protocol):
    """Signs checkpoints (§3.3). The key NEVER lives in the store."""
    key_id: str
    def sign(self, data: str) -> str: ...


class InMemoryEventStore:
    """In-memory append-only log.

    Production: same interface over PostgreSQL (see sql/schema.sql).
    """

    def __init__(self) -> None:
        self._events: dict[str, Event] = {}
        self._streams: dict[str, list[Event]] = {}
        self._head: dict[str, tuple[int, str]] = {}   
        self._lock = threading.RLock()
        self._checkpoints: list[dict] = []

    def append(self, draft: EventDraft, stream: str) -> Event:
        with self._lock:
            seq, prev = self._head.get(stream, (0, GENESIS))
            ev = Event(
                **draft.model_dump(),
                id=new_id("evt"), stream=stream, seq=seq + 1,
                payload_hash=hash_payload(draft.payload),
                prev_hash=prev, this_hash="",
            ).seal()
            self._events[ev.id] = ev
            self._streams.setdefault(stream, []).append(ev)
            self._head[stream] = (ev.seq, ev.this_hash)
            return ev

    def append_state_transition(self, draft: EventDraft, stream: str,
                                execution_id: str, target) -> Event:
        """Atomically validate-and-append an EXECUTION_STATE_CHANGED event.

        The current state is derived and checked against VALID inside the
        store lock, so check-then-act cannot interleave with another
        in-process writer."""
        from .statemachine import (
            VALID,
            ExecutionState,
            StateTransitionError,
            derive_execution_state,
        )
        tgt = target if isinstance(target, ExecutionState) \
            else ExecutionState(target)
        with self._lock:
            evs = [e for e in self._events.values()
                   if e.execution_id == execution_id]
            current = derive_execution_state(evs)
            if tgt not in VALID[current]:
                raise StateTransitionError(current, tgt)
            draft.payload = {**(draft.payload or {}),
                             "from": current.value, "to": tgt.value}
            return self.append(draft, stream)

    def get(self, event_id: str) -> Event | None:
        return self._events.get(event_id)

    def query(self, q: EventQuery | None = None) -> list[Event]:
        q = q or EventQuery()
        with self._lock:
            events = list(self._events.values())
        out = []
        for ev in events:
            if q.task_id and ev.task_id != q.task_id: continue
            if q.execution_id and ev.execution_id != q.execution_id: continue
            if q.agent_id and ev.agent_id != q.agent_id: continue
            if q.types and ev.type not in q.types: continue
            if q.since and ev.ts < q.since: continue
            if q.until and ev.ts > q.until: continue
            if q.payload_eq and any(
                    (ev.payload or {}).get(k) != v
                    for k, v in q.payload_eq.items()):
                continue
            out.append(ev)
        out.sort(key=lambda e: (e.ts, e.seq))
        return out[: q.limit]

    def head(self, stream: str) -> tuple[int, str]:
        return self._head.get(stream, (0, GENESIS))

    def verify_chain(self, stream: str | None = None) -> ChainAudit:
        streams = [stream] if stream else list(self._streams)
        problems, checked = [], 0
        for s in streams:
            prev = GENESIS
            for ev in self._streams.get(s, []):
                checked += 1
                if ev.prev_hash != prev:
                    problems.append(f"{ev.id}: prev_hash does not link to the chain")
                if ev.payload_hash != hash_payload(ev.payload):
                    problems.append(f"{ev.id}: content altered (hash mismatch)")
                if ev.this_hash != sha256_hex(canonical_json(ev.hash_material())):
                    problems.append(f"{ev.id}: invalid this_hash")
                prev = ev.this_hash
        return ChainAudit(ok=not problems, events_checked=checked,
                           streams_checked=len(streams), problems=problems)

    def take_checkpoint(self, signer: CheckpointSigner | None = None) -> dict:
        """Freezes the heads of all streams and (optionally) signs them.

        In deployments that require it, the signature is verified from an
        independent administrative domain (§3.3).
        """
        with self._lock:
            heads = {s: {"seq": seq, "hash": h} for s, (seq, h) in self._head.items()}
        cp: dict[str, Any] = {"id": new_id("ckpt"), "ts": now(), "heads": heads,
              "head_hash": sha256_hex(canonical_json(heads))}
        if signer is not None:
            cp["signature"] = signer.sign(cp["head_hash"])
            cp["signer"] = signer.key_id
        self._checkpoints.append(cp)
        return cp

class InMemorySequenceGuard:
    """Transport anti-replay, in memory (lost on restart; the timestamp
    window covers that gap).

    Rejects a (credential, sequence) pair already seen within the retention
    window. Does NOT require increasing order: concurrent requests, several
    processes with the same credential or a clock that goes backwards do not
    produce false rejections. `retention_s` must be >= 2 × the service's
    clock tolerance: a request accepted at T with timestamp t satisfies
    |T-t| <= S, so it can only be replayed until T+2S with the timestamp
    still valid.
    """

    def __init__(self, retention_s: float = 900.0) -> None:
        self._retention_s = retention_s
        self._purge_every = min(60.0, retention_s)
        self._seen: dict[tuple[str, int], float] = {}
        self._last_purge = time.monotonic()
        self._lock = threading.Lock()

    def check_and_advance(self, key: str, seq: int) -> bool:
        t = time.monotonic()
        with self._lock:
            if t - self._last_purge >= self._purge_every:
                limite = t - self._retention_s
                self._seen = {k: v for k, v in self._seen.items()
                              if v >= limite}
                self._last_purge = t
            if (key, seq) in self._seen:
                return False
            self._seen[(key, seq)] = t
            return True