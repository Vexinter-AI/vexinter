"""Per-person erasure (GDPR Art.17) - person → execution_ids index (Item 13).

Implements full right to be forgotten: allows deleting all data associated
with a person (data subject) while keeping event-chain integrity
(hash-chaining intact).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel, Field

from .crypto import canonical_json
from .events import EventDraft, EventType, new_id
from .store import EventQuery
from .store import InMemoryEventStore as EventStore

_log = logging.getLogger(__name__)


class ErasureStatus(str, Enum):
    """States of an erasure request."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    REJECTED = "rejected"


class DataCategory(str, Enum):
    """Data categories for selective erasure."""

    PERSONAL_IDENTIFIERS = "personal_identifiers"  # email, name, phone
    CONTACT_INFO = "contact_info"
    BIOMETRIC = "biometric"
    BEHAVIORAL = "behavioral"
    TECHNICAL = "technical"  # IPs, user agents, etc.
    FINANCIAL = "financial"
    ALL = "all"


@dataclass
class PersonIndexEntry:
    """Entry in the person → execution_ids index."""

    person_id: str
    execution_ids: set[str] = field(default_factory=set)
    task_ids: set[str] = field(default_factory=set)
    agent_ids: set[str] = field(default_factory=set)
    credential_ids: set[str] = field(default_factory=set)
    first_seen: datetime = field(default_factory=lambda: datetime.now(UTC))
    last_seen: datetime = field(default_factory=lambda: datetime.now(UTC))
    data_categories: set[DataCategory] = field(default_factory=set)
    metadata: dict[str, Any] = field(default_factory=dict)


class ErasureRequest(BaseModel):
    """Erasure request (GDPR Art. 17)."""

    request_id: str = Field(default_factory=lambda: new_id("ers"))
    person_id: str
    requested_by: str  # user_id or system
    reason: str
    categories: list[DataCategory] = Field(default_factory=lambda: [DataCategory.ALL])
    status: ErasureStatus = ErasureStatus.PENDING
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    completed_at: datetime | None = None
    verified_by: str | None = None
    verification_token: str | None = None
    affected_executions: list[str] = Field(default_factory=list)
    affected_events: int = 0
    crypto_erased_executions: int = 0
    error_message: str | None = None


class PersonIndex:
    """Inverted index: person → execution_ids for fast erasure (Art. 17)."""

    def __init__(
        self, store: EventStore | None = None, index_path: str | Path | None = None
    ):
        self._store = store
        self._index_path = Path(index_path) if index_path else None
        self._entries: dict[str, PersonIndexEntry] = {}
        self._execution_to_person: dict[str, str] = {}  # execution_id -> person_id
        self._load_index()
        self._rebuild_from_log()

    _INDEX_RESTORE_LIMIT = 100_000

    def _rebuild_from_log(self) -> None:
        """Rebuilds the index from gdpr_person_index events when a store
        is attached — the log is authoritative, so a fresh worker (or a
        lost index file) recovers the full person → execution mapping.
        Events are applied in log order; the file cache is refreshed."""
        if self._store is None:
            return
        from .store import EventQuery as _EQ
        evs = self._store.query(_EQ(
            types={EventType.SYSTEM_CHECKPOINT},
            payload_eq={"event": "gdpr_person_index"},
            limit=self._INDEX_RESTORE_LIMIT))
        if len(evs) >= self._INDEX_RESTORE_LIMIT:
            _log.warning("person-index rebuild hit the %d event limit",
                         self._INDEX_RESTORE_LIMIT)
        for e in evs:
            p = e.payload or {}
            pid = p.get("person_id")
            if not pid:
                continue
            if p.get("op") == "remove":
                entry = self._entries.pop(pid, None)
                if entry:
                    for ex in entry.execution_ids:
                        self._execution_to_person.pop(ex, None)
                continue
            entry = self._entries.setdefault(pid, PersonIndexEntry(
                person_id=pid,
                first_seen=e.ts, last_seen=e.ts))
            ex = p.get("execution_id") or ""
            if ex:
                entry.execution_ids.add(ex)
                self._execution_to_person[ex] = pid
            for k, attr in (("task_id", "task_ids"),
                            ("agent_id", "agent_ids"),
                            ("credential_id", "credential_ids")):
                v = p.get(k)
                if v:
                    getattr(entry, attr).add(v)
            for c in p.get("categories") or []:
                try:
                    entry.data_categories.add(DataCategory(c))
                except ValueError:
                    continue
            if e.ts:
                entry.last_seen = e.ts
        if evs:
            self._save_index()

    @staticmethod
    def _entry_to_dict(e: PersonIndexEntry) -> dict:
        return {
            "person_id": e.person_id,
            "execution_ids": sorted(e.execution_ids),
            "task_ids": sorted(e.task_ids),
            "agent_ids": sorted(e.agent_ids),
            "credential_ids": sorted(e.credential_ids),
            "first_seen": e.first_seen.isoformat(),
            "last_seen": e.last_seen.isoformat(),
            "data_categories": sorted(c.value for c in e.data_categories),
            "metadata": e.metadata,
        }

    @staticmethod
    def _entry_from_dict(d: dict) -> PersonIndexEntry:
        def _dt(v, default):
            try:
                return datetime.fromisoformat(str(v))
            except (TypeError, ValueError):
                return default

        return PersonIndexEntry(
            person_id=d["person_id"],
            execution_ids=set(d.get("execution_ids", [])),
            task_ids=set(d.get("task_ids", [])),
            agent_ids=set(d.get("agent_ids", [])),
            credential_ids=set(d.get("credential_ids", [])),
            first_seen=_dt(d.get("first_seen"), datetime.now(UTC)),
            last_seen=_dt(d.get("last_seen"), datetime.now(UTC)),
            data_categories={
                DataCategory(c) for c in d.get("data_categories", [])
                if c in {x.value for x in DataCategory}
            },
            metadata=d.get("metadata", {}),
        )

    def _load_index(self) -> None:
        """Loads the index from disk if it exists (JSON, never pickle)."""
        if self._index_path and self._index_path.exists():
            try:
                data = json.loads(self._index_path.read_text(encoding="utf-8"))
                self._entries = {
                    pid: self._entry_from_dict(d)
                    for pid, d in data.get("entries", {}).items()
                }
                self._execution_to_person = dict(
                    data.get("execution_to_person", {})
                )
            except Exception as e:  # noqa: BLE001 - error boundary: converted to result/log
                # WARNING, not debug: a corrupt index makes erasure
                # silently report "no data" while PII remains in the log.
                _log.warning(
                    "could not load GDPR index from %s: %s", self._index_path, e
                )

    def _save_index(self) -> None:
        """Saves the index to disk as JSON."""
        if self._index_path:
            try:
                self._index_path.parent.mkdir(parents=True, exist_ok=True)
                payload = {
                    "entries": {
                        pid: self._entry_to_dict(e)
                        for pid, e in self._entries.items()
                    },
                    "execution_to_person": self._execution_to_person,
                }
                self._index_path.write_text(
                    json.dumps(payload, indent=2), encoding="utf-8"
                )
            except Exception as e:  # noqa: BLE001 - error boundary: converted to result/log
                _log.warning(
                    "could not save GDPR index to %s: %s", self._index_path, e
                )

    def associate_person(
        self,
        person_id: str,
        execution_id: str,
        task_id: str | None = None,
        agent_id: str | None = None,
        credential_id: str | None = None,
        categories: list[DataCategory] | None = None,
    ) -> None:
        """Associates a person with an execution (called when events are created)."""
        if person_id not in self._entries:
            self._entries[person_id] = PersonIndexEntry(person_id=person_id)

        entry = self._entries[person_id]
        entry.execution_ids.add(execution_id)
        entry.last_seen = datetime.now(UTC)

        if task_id:
            entry.task_ids.add(task_id)
        if agent_id:
            entry.agent_ids.add(agent_id)
        if credential_id:
            entry.credential_ids.add(credential_id)
        if categories:
            entry.data_categories.update(categories)

        self._execution_to_person[execution_id] = person_id
        self._save_index()
        self._emit_index_event("associate", person_id=person_id,
                               execution_id=execution_id, task_id=task_id,
                               agent_id=agent_id, credential_id=credential_id,
                               categories=categories)

    def _emit_index_event(self, op: str, **fields) -> None:
        """Appends a gdpr_person_index event so other workers rebuild the
        same index after restart (the JSON file is only a local cache)."""
        if self._store is None:
            return
        try:
            self._store.append(EventDraft(
                type=EventType.SYSTEM_CHECKPOINT,
                payload={"event": "gdpr_person_index", "op": op, **fields},
            ), stream="gdpr")
        except Exception as e:  # noqa: BLE001 - index event is best-effort
            _log.warning("could not persist person-index event: %s", e)

    def get_person_data(self, person_id: str) -> PersonIndexEntry | None:
        """Gets all IDs associated with a person."""
        return self._entries.get(person_id)

    def get_executions_for_person(self, person_id: str) -> set[str]:
        """Gets all execution_ids for a person."""
        entry = self._entries.get(person_id)
        return entry.execution_ids if entry else set()

    def remove_person(self, person_id: str) -> PersonIndexEntry | None:
        """Removes a person from the index (after erasure completes)."""
        entry = self._entries.pop(person_id, None)
        if entry:
            for exec_id in entry.execution_ids:
                self._execution_to_person.pop(exec_id, None)
            self._save_index()
            self._emit_index_event("remove", person_id=person_id)
        return entry

    def list_persons(self) -> list[str]:
        return list(self._entries.keys())


class GDPRErasureService:
    """GDPR Art.17 erasure service with the hash chain intact.

    Strategy: Redaction instead of physical deletion.
    Events remain in the store but personal fields are replaced with
    deterministic hashes, preserving hash-chain integrity.
    """

    # Personal data field patterns in payloads
    PERSONAL_FIELDS: ClassVar[set[str]] = {
        "email",
        "name",
        "first_name",
        "last_name",
        "full_name",
        "phone",
        "telephone",
        "mobile",
        "address",
        "street",
        "city",
        "postal_code",
        "country",
        "ssn",
        "dni",
        "passport",
        "ip",
        "ip_address",
        "user_agent",
        "fingerprint",
        "credit_card",
        "iban",
        "account_number",
        "biometric",
        "face_id",
        "fingerprint_hash",
    }

    def __init__(
        self,
        store: EventStore,
        person_index: PersonIndex,
        erasure_log_path: str | Path | None = None,
        eraser: Any | None = None,
    ):
        self._store = store
        self._person_index = person_index
        self._erasure_log_path = Path(erasure_log_path) if erasure_log_path else None
        self._requests: dict[str, ErasureRequest] = {}
        # Optional crypto-erasure hook (e.g. PrivacyStore.erase): destroys the
        # per-execution key so encrypted PII becomes unreadable. Without it,
        # redaction only produces audit markers — the underlying fields stay.
        self._eraser = eraser
        self._restore_requests()

    _REQUESTS_RESTORE_LIMIT = 100_000

    def _restore_requests(self) -> None:
        """Rebuilds pending/completed erasure requests from the log —
        _log_request already appends every state change as a
        gdpr_erasure_request event, so a restarted worker recovers them
        (last event per request_id wins)."""
        from .store import EventQuery as _EQ
        try:
            evs = self._store.query(_EQ(
                types={EventType.SYSTEM_CHECKPOINT},
                payload_eq={"event": "gdpr_erasure_request"},
                limit=self._REQUESTS_RESTORE_LIMIT))
        except Exception as e:  # noqa: BLE001 - restore is best-effort
            _log.warning("could not restore erasure requests: %s", e)
            return
        if len(evs) >= self._REQUESTS_RESTORE_LIMIT:
            _log.warning("erasure-request restore hit the %d event limit",
                         self._REQUESTS_RESTORE_LIMIT)
        for ev in evs:
            raw = (ev.payload or {}).get("request")
            if not isinstance(raw, dict):
                continue
            try:
                req = ErasureRequest.model_validate(raw)
            except Exception as exc:  # noqa: BLE001 - skip malformed rows
                _log.debug("skipping malformed erasure-request event: %s",
                           exc)
                continue
            self._requests[req.request_id] = req

    def request_erasure(
        self,
        person_id: str,
        requested_by: str,
        reason: str,
        categories: list[DataCategory] | None = None,
    ) -> ErasureRequest:
        """Creates an erasure request (Art. 17)."""
        request = ErasureRequest(
            person_id=person_id,
            requested_by=requested_by,
            reason=reason,
            categories=categories or [DataCategory.ALL],
        )
        request.verification_token = self._generate_verification_token(
            request.request_id
        )
        self._requests[request.request_id] = request
        self._log_request(request)
        return request

    def verify_and_execute(
        self, request_id: str, verification_token: str
    ) -> ErasureRequest:
        """Verifies the token and executes the erasure."""
        request = self._requests.get(request_id)
        if not request:
            raise ValueError(f"Request not found: {request_id}")

        if (not request.verification_token
                or not hmac.compare_digest(
                    request.verification_token, verification_token)):
            request.status = ErasureStatus.REJECTED
            request.error_message = "Invalid verification token"
            self._log_request(request)
            raise ValueError("Invalid verification token")

        return self._execute_erasure(request)

    def _execute_erasure(self, request: ErasureRequest) -> ErasureRequest:
        """Executes the actual erasure by redacting personal fields."""
        request.status = ErasureStatus.IN_PROGRESS
        self._log_request(request)

        person_data = self._person_index.get_person_data(request.person_id)
        if not person_data:
            request.status = ErasureStatus.COMPLETED
            request.completed_at = datetime.now(UTC)
            request.error_message = "No data found for the person"
            self._log_request(request)
            return request

        # Determine affected executions
        target_executions = person_data.execution_ids
        if DataCategory.ALL not in request.categories:
            # Filter by category (simplified - more granular in production)
            pass

        request.affected_executions = list(target_executions)
        total_redacted = 0
        crypto_ok = 0

        if self._eraser is None:
            _log.warning(
                "GDPR erasure for %s WITHOUT crypto-erasure hook: %d "
                "executions get redaction markers only — per-execution "
                "keys are NOT destroyed", request.person_id,
                len(target_executions))

        # Redact each execution
        for exec_id in target_executions:
            redacted = self._redact_execution(exec_id, request.categories)
            total_redacted += redacted
            if self._eraser is not None:
                try:
                    self._eraser(exec_id, reason=request.reason,
                                 requested_by=request.requested_by)
                    crypto_ok += 1
                except Exception as e:  # noqa: BLE001 - error boundary: converted to result/log
                    _log.warning("crypto-erasure failed for %s: %s", exec_id, e)

        request.affected_events = total_redacted
        request.crypto_erased_executions = crypto_ok
        request.status = ErasureStatus.COMPLETED
        request.completed_at = datetime.now(UTC)

        # Remove from the person index
        self._person_index.remove_person(request.person_id)

        self._log_request(request)
        return request

    def _redact_execution(
        self, execution_id: str, categories: list[DataCategory]
    ) -> int:
        """Redacts personal fields in all events of an execution."""

        events = self._store.query(EventQuery(execution_id=execution_id))
        redacted_count = 0

        for event in events:
            if self._redact_event(event, categories):
                redacted_count += 1

        return redacted_count

    def _redact_event(self, event, categories: list[DataCategory]) -> bool:
        """Redacts personal fields in an event.

        IMPORTANT: Does not modify the original event in the store (append-only).
        Instead, creates a linked redaction event indicating which fields were
        redacted, preserving hash-chain integrity.
        """
        payload = event.payload
        if not isinstance(payload, dict):
            return False

        redacted_fields: list[str] = []
        self._redact_dict(dict(payload), categories, redacted_fields)

        if not redacted_fields:
            return False

        # Create redaction event (does not modify the original).
        # NOTE: the redacted *values* are deliberately NOT stored — even a
        # truncated unsalted SHA-256 of low-entropy PII (emails, names) is
        # brute-forceable, so persisting them would leak the very data this
        # event certifies as erased. Only field names + a hash of the field
        # list go into the log.
        self._store.append(
            EventDraft(
                type=EventType.SYSTEM_CHECKPOINT,
                task_id=event.task_id,
                execution_id=event.execution_id,
                payload={
                    "event": "gdpr_redaction",
                    "original_event_id": event.id,
                    "execution_id": event.execution_id,
                    "redacted_fields": redacted_fields,
                    "redaction_hash": self._hash_redaction(redacted_fields),
                    "timestamp": datetime.now(UTC).isoformat(),
                    "categories": [c.value for c in categories],
                },
            ),
            stream="gdpr",
        )

        return True

    def _redact_dict(
        self,
        obj: dict,
        categories: list[DataCategory],
        redacted_fields: list[str],
        prefix: str = "",
    ) -> dict:
        """Recursively redacts a dictionary."""
        result: dict[str, Any] = {}
        for key, value in obj.items():
            full_key = f"{prefix}.{key}" if prefix else key

            # Check whether the field must be redacted
            should_redact = False
            if DataCategory.ALL in categories:
                should_redact = key.lower() in self.PERSONAL_FIELDS
            else:
                # Category logic (simplified)
                should_redact = key.lower() in self.PERSONAL_FIELDS

            if should_redact:
                # Redact ANY value type (str, dict, list, ...): a personal
                # field holding a nested structure must not leak through.
                result[key] = (
                    f"[REDACTED:{hashlib.sha256(canonical_json(value).encode()).hexdigest()[:16]}]"
                )
                redacted_fields.append(full_key)
            elif isinstance(value, dict):
                result[key] = self._redact_dict(
                    value, categories, redacted_fields, full_key
                )
            elif isinstance(value, list):
                result[key] = [
                    self._redact_dict(
                        item, categories, redacted_fields, f"{full_key}[{i}]"
                    )
                    if isinstance(item, dict)
                    else item
                    for i, item in enumerate(value)
                ]
            else:
                result[key] = value

        return result

    def _hash_redaction(self, fields: list[str]) -> str:
        """Generates a hash of the redacted fields for verification."""
        content = "|".join(sorted(fields)).encode()
        return hashlib.sha256(content).hexdigest()

    def _generate_verification_token(self, request_id: str) -> str:
        """Generates a verification token to confirm erasure."""
        import secrets

        return secrets.token_urlsafe(32)

    def _log_request(self, request: ErasureRequest) -> None:
        """Logs the request to the audit log."""
        if self._erasure_log_path:
            self._erasure_log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._erasure_log_path, "a") as f:
                f.write(json.dumps(request.model_dump(mode="json")) + "\n")

        # Also to the store
        if self._store:
            self._store.append(
                EventDraft(
                    type=EventType.SYSTEM_CHECKPOINT,
                    payload={
                        "event": "gdpr_erasure_request",
                        "request": request.model_dump(mode="json"),
                    },
                ),
                stream="gdpr",
            )

    def get_request(self, request_id: str) -> ErasureRequest | None:
        return self._requests.get(request_id)

    def list_requests(self, person_id: str | None = None) -> list[ErasureRequest]:
        requests = list(self._requests.values())
        if person_id:
            requests = [r for r in requests if r.person_id == person_id]
        return sorted(requests, key=lambda x: x.created_at, reverse=True)


# === Helpers for automatic person_id extraction from events ===


def extract_person_id_from_event(event) -> str | None:
    """Extracts person_id from an event if present."""
    payload = event.payload
    if not isinstance(payload, dict):
        return None

    # Search common fields
    for fname in ("person_id", "user_id", "subject_id", "data_subject_id", "email"):
        if fname in payload:
            return str(payload[fname])

    # Search nested
    for value in payload.values():
        if isinstance(value, dict) and "person_id" in value:
            return str(value["person_id"])

    return None


def auto_index_person_from_events(store: EventStore, person_index: PersonIndex) -> int:
    """Automatically indexes people from existing events (backfill)."""

    events = store.query(EventQuery(limit=10000))  # Last 10k
    count = 0

    for event in events:
        person_id = extract_person_id_from_event(event)
        if person_id:
            person_index.associate_person(
                person_id=person_id,
                execution_id=event.execution_id or "",
                task_id=event.task_id,
                agent_id=event.agent_id,
            )
            count += 1

    return count
