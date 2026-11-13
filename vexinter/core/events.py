from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from .crypto import canonical_json, sha256_hex


def now() -> datetime:
    return datetime.now(UTC)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


class EventType(str, Enum):
    """Catalog of events (§3.1)."""
    TASK_CREATED             = "task.created"
    TASK_ASSIGNED            = "task.assigned"
    EXECUTION_STARTED        = "execution.started"
    EXECUTION_COMPLETED      = "execution.completed"
    MESSAGE_SENT             = "message.sent"
    MESSAGE_RECEIVED         = "message.received"
    TOOL_INVOKED             = "tool.invoked"
    TOOL_RETURNED            = "tool.returned"
    TOOL_REJECTED            = "tool.rejected"
    TOOL_REPLAYED            = "tool.replayed"
    STATE_OBSERVED           = "state.observed"
    VERIFIER_VERDICT         = "verifier.verdict"
    DISCREPANCY_DETECTED     = "recovery.discrepancy"
    RECOVERY_DECIDED         = "recovery.decided"
    RECOVERY_EXECUTED        = "recovery.executed"
    HUMAN_DECISION_REQUESTED = "recovery.human_requested"
    HUMAN_DECISION_MADE      = "recovery.human_decided"
    CASE_RESOLVED            = "recovery.case_resolved"
    FOLLOWUP_SCHEDULED       = "followup.scheduled"
    FOLLOWUP_EXECUTED        = "followup.executed"
    MEMORY_RETRIEVED         = "memory.retrieved"
    MEMORY_UPDATED           = "memory.updated"
    CORRECTION_RECORDED      = "correction.recorded"
    EXECUTION_STATE_CHANGED  = "execution.state_changed"
    IDENTITY_ENROLLED        = "identity.enrolled"     
    IDENTITY_REVOKED         = "identity.revoked"   
    MESSAGE_REJECTED         = "message.rejected"
    CREDENTIAL_ISSUED        = "identity.credential_issued"
    CREDENTIAL_REVOKED       = "identity.credential_revoked"
    INSTANCE_SPAWNED         = "instance_spawned"
    PRIVACY_ERASED           = "privacy.erased"
    # FinOps & Loop Detection telemetry (Phase 1)
    COST_EVENT               = "cost.event"
    LOOP_DETECTED            = "loop.detected"
    EXECUTION_LIMIT_EXCEEDED = "execution.limit_exceeded"
    # Human-in-the-Loop (Phase 2)
    HITL_REQUESTED           = "hitl.requested"
    HITL_RESOLVED            = "hitl.resolved"
    # MCP Gateway (Phase 3)
    MCP_PROXIED              = "mcp.proxied"
    # Policy Engine (Phase 4)
    POLICY_VIOLATION         = "policy.violation"
    # Self-Healing Memory (Phase 5)
    LESSON_EXTRACTED         = "memory.lesson_extracted"
    # Red-Teaming (Phase 6)
    REDTEAM_FINDING          = "redteam.finding"
    # Multi-agent Orchestration (Phase 7)
    TASK_DELEGATED           = "task.delegated"
    SUB_TASK_CREATED         = "subtask.created"
    SUB_TASK_COMPLETED       = "subtask.completed"
    AGENT_MESSAGE_SENT       = "agent.message.sent"
    AGENT_MESSAGE_RECEIVED   = "agent.message.received"
    SYSTEM_CHECKPOINT        = "system.checkpoint"


# Export constants for compatibility
COST_EVENT = EventType.COST_EVENT
LOOP_DETECTED = EventType.LOOP_DETECTED
HITL_REQUESTED = EventType.HITL_REQUESTED
HITL_RESOLVED = EventType.HITL_RESOLVED
MCP_PROXIED = EventType.MCP_PROXIED
POLICY_VIOLATION = EventType.POLICY_VIOLATION
LESSON_EXTRACTED = EventType.LESSON_EXTRACTED
REDTEAM_FINDING = EventType.REDTEAM_FINDING
TASK_DELEGATED = EventType.TASK_DELEGATED
SUB_TASK_CREATED = EventType.SUB_TASK_CREATED
SUB_TASK_COMPLETED = EventType.SUB_TASK_COMPLETED
AGENT_MESSAGE_SENT = EventType.AGENT_MESSAGE_SENT
AGENT_MESSAGE_RECEIVED = EventType.AGENT_MESSAGE_RECEIVED
SYSTEM_CHECKPOINT = EventType.SYSTEM_CHECKPOINT


class EventDraft(BaseModel):
    """Event before being sealed (the store assigns seq and prev_hash)."""
    model_config = ConfigDict(extra="forbid")

    type: EventType
    ts: datetime = Field(default_factory=now)
    task_id: str | None = None
    execution_id: str | None = None
    agent_id: str | None = None
    operation_id: str | None = None
    caused_by: str | None = None          
    depends_on: list[str] = Field(default_factory=list)  
    payload: dict[str, Any] | None = None
    payload_ref: str | None = None        
    compensates: str | None = None


class Event(EventDraft):
    """Sealed event: immutable by logical construction."""
    id: str
    stream: str
    seq: int
    payload_hash: str
    prev_hash: str
    this_hash: str
    signature: str | None = None

    def hash_material(self) -> dict:
        material = {
            "id": self.id, "stream": self.stream, "seq": self.seq,
            "type": self.type.value, "ts": self.ts,
            "task_id": self.task_id, "execution_id": self.execution_id,
            "agent_id": self.agent_id, "operation_id": self.operation_id,
            "caused_by": self.caused_by, "depends_on": self.depends_on,
            "compensates": self.compensates,            # NEW (v0.2)
            "payload_hash": self.payload_hash, "prev_hash": self.prev_hash,
        }
        # Externalized payloads: the reference itself is part of the sealed
        # material so swapping the pointer is detected. Only included when
        # set, so events without payload_ref keep their historical hashes.
        if self.payload_ref is not None:
            material["payload_ref"] = self.payload_ref
        return material

    def seal(self) -> Event:
        self.this_hash = sha256_hex(canonical_json(self.hash_material()))
        return self