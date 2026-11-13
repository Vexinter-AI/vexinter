from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, PrivateAttr

from ..models.budget import Budget
from .tools import CallContext, Observation


class TaskRun(BaseModel):
    """State of a supervised task during its execution."""
    task_id: str
    execution_id: str
    agent_id: str
    objective: str
    condition: dict
    instance_id: str | None = None
    credential_id: str | None = None
    input: dict = Field(default_factory=dict)
    pre_snapshot: dict[str, Observation] = Field(default_factory=dict)
    collaborators: list[str] = Field(default_factory=list)
    budget: Budget | None = None
    completed: bool = False
    _intermediary: Any = PrivateAttr(default=None)

    @property
    def stream(self) -> str:
        return f"task:{self.task_id}"

    def call_context(self) -> CallContext:
        return CallContext(agent_id=self.agent_id, task_id=self.task_id,
                           execution_id=self.execution_id, stream=self.stream,
                           instance_id=self.instance_id,
                           credential_id=self.credential_id,
                           budget=self.budget)