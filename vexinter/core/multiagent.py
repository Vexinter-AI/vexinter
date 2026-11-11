from __future__ import annotations

import threading
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .events import EventDraft, EventType, new_id
from .store import InMemoryEventStore


class SubTaskStatus(str, Enum):
    """Status of a sub-task."""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class DelegationMode(str, Enum):
    """Delegation mode."""
    SEQUENTIAL = "sequential"      # Run one after another
    PARALLEL = "parallel"          # Run in parallel
    CONDITIONAL = "conditional"    # Run based on a condition


@dataclass
class SubTask:
    """Sub-task delegated to another agent."""
    id: str
    parent_task_id: str
    parent_execution_id: str
    agent_id: str                    # Agent to which it is delegated
    objective: str
    condition: dict
    input: dict
    status: SubTaskStatus = SubTaskStatus.PENDING
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    completed_at: float | None = None
    result: dict | None = None
    error: str | None = None
    execution_id: str | None = None


@dataclass
class AgentMessage:
    """Message between agents."""
    id: str
    task_id: str
    from_agent: str
    to_agent: str
    content: dict
    message_type: str = "notification"  # notification, request, response
    created_at: float = field(default_factory=time.time)
    read_at: float | None = None


class SubTaskManager:
    """Manager of sub-tasks and multi-agent delegation."""

    def __init__(self, store: InMemoryEventStore, task_runner_factory: Callable):
        self._store = store
        self._task_runner_factory = task_runner_factory
        self._sub_tasks: dict[str, SubTask] = {}
        self._task_subtasks: dict[str, list[str]] = defaultdict(list)  # parent_task_id -> [subtask_ids]
        self._agent_messages: dict[str, list[AgentMessage]] = defaultdict(list)  # agent_id -> messages
        self._lock = threading.RLock()

    def create_subtask(self, parent_task_id: str, parent_execution_id: str,
                       agent_id: str, objective: str, condition: dict,
                       input: dict, parent_agent_id: str) -> SubTask:
        """Creates a sub-task delegated to another agent."""
        subtask = SubTask(
            id=new_id("subtask"),
            parent_task_id=parent_task_id,
            parent_execution_id=parent_execution_id,
            agent_id=agent_id,
            objective=objective,
            condition=condition,
            input=input,
        )
        self._sub_tasks[subtask.id] = subtask
        self._task_subtasks[parent_task_id].append(subtask.id)

        # Emit sub-task created event
        self._store.append(EventDraft(
            type=EventType.SUB_TASK_CREATED,
            agent_id=parent_agent_id,
            execution_id=parent_execution_id,
            task_id=parent_task_id,
            payload={
                "subtask_id": subtask.id,
                "agent_id": agent_id,
                "objective": objective,
                "condition": condition,
                "input": input,
            }
        ), stream=f"task:{parent_task_id}")
        return subtask

    def delegate_task(self, parent_task_id: str, parent_execution_id: str,
                      agent_id: str, objective: str, condition: dict,
                      input: dict, parent_agent_id: str) -> SubTask:
        """Delegates a task to another agent (alias for create_subtask with a delegation event)."""
        subtask = self.create_subtask(parent_task_id, parent_execution_id,
                                       agent_id, objective, condition, input, parent_agent_id)
        
        # Emit delegation event
        self._store.append(EventDraft(
            type=EventType.TASK_DELEGATED,
            agent_id=parent_agent_id,
            execution_id=parent_execution_id,
            task_id=parent_task_id,
            payload={
                "subtask_id": subtask.id,
                "delegated_to": agent_id,
                "objective": objective,
            }
        ), stream=f"task:{parent_task_id}")
        return subtask

    def get_subtask(self, subtask_id: str) -> SubTask | None:
        with self._lock:
            return self._sub_tasks.get(subtask_id)

    def get_subtasks_for_task(self, task_id: str) -> list[SubTask]:
        with self._lock:
            return [self._sub_tasks[sid] for sid in self._task_subtasks.get(task_id, [])]

    def start_subtask(self, subtask_id: str, execution_id: str) -> None:
        with self._lock:
            """Marks a sub-task as started."""
            subtask = self._sub_tasks.get(subtask_id)
            if subtask:
                subtask.status = SubTaskStatus.RUNNING
                subtask.started_at = time.time()
                subtask.execution_id = execution_id

    def complete_subtask(self, subtask_id: str, result: dict) -> None:
        with self._lock:
            """Marks a sub-task as completed."""
            subtask = self._sub_tasks.get(subtask_id)
            if subtask:
                subtask.status = SubTaskStatus.COMPLETED
                subtask.completed_at = time.time()
                subtask.result = result
                # Emit completed event
                self._store.append(EventDraft(
                    type=EventType.SUB_TASK_COMPLETED,
                    agent_id=subtask.agent_id,
                    execution_id=subtask.execution_id or subtask.parent_execution_id,
                    task_id=subtask.parent_task_id,
                    payload={
                        "subtask_id": subtask_id,
                        "result": result,
                    }
                ), stream=f"task:{subtask.parent_task_id}")

    def fail_subtask(self, subtask_id: str, error: str) -> None:
        with self._lock:
            """Marks a sub-task as failed."""
            subtask = self._sub_tasks.get(subtask_id)
            if subtask:
                subtask.status = SubTaskStatus.FAILED
                subtask.completed_at = time.time()
                subtask.error = error

    def cancel_subtask(self, subtask_id: str) -> None:
        with self._lock:
            """Cancels a sub-task."""
            subtask = self._sub_tasks.get(subtask_id)
            if subtask:
                subtask.status = SubTaskStatus.CANCELLED
                subtask.completed_at = time.time()

    def get_pending_subtasks(self, parent_task_id: str) -> list[SubTask]:
        with self._lock:
            return [s for s in self.get_subtasks_for_task(parent_task_id)
                    if s.status == SubTaskStatus.PENDING]

    def get_running_subtasks(self, parent_task_id: str) -> list[SubTask]:
        with self._lock:
            return [s for s in self.get_subtasks_for_task(parent_task_id)
                    if s.status == SubTaskStatus.RUNNING]

    _TERMINAL = frozenset({SubTaskStatus.COMPLETED, SubTaskStatus.FAILED,
                           SubTaskStatus.CANCELLED})

    def wait_for_subtasks(self, parent_task_id: str, timeout_s: float = 60.0,
                          poll_s: float = 0.1) -> list[SubTask]:
        """Blocks until every sub-task of `parent_task_id` reaches a
        terminal state (completed/failed/cancelled) or `timeout_s`
        elapses. Delegated agents execute asynchronously — callers that
        need results must wait here (or poll aggregate_results)."""
        deadline = time.time() + timeout_s
        while True:
            subs = self.get_subtasks_for_task(parent_task_id)
            if not subs or all(s.status in self._TERMINAL for s in subs):
                return subs
            if time.time() >= deadline:
                return subs
            time.sleep(poll_s)

    def are_all_subtasks_completed(self, parent_task_id: str) -> bool:
        with self._lock:
            subtasks = self.get_subtasks_for_task(parent_task_id)
            return all(s.status in (SubTaskStatus.COMPLETED, SubTaskStatus.FAILED, SubTaskStatus.CANCELLED)
                       for s in subtasks) and len(subtasks) > 0


class AgentMessaging:
    """Messaging system between agents."""

    def __init__(self, store: InMemoryEventStore):
        self._store = store
        self._messages: dict[str, list[AgentMessage]] = defaultdict(list)  # agent_id -> messages
        self._lock = threading.RLock()

    def send_message(self, task_id: str, from_agent: str, to_agent: str,
                     content: dict, message_type: str = "notification") -> AgentMessage:
        """Sends a message from one agent to another."""
        message = AgentMessage(
            id=new_id("msg"),
            task_id=task_id,
            from_agent=from_agent,
            to_agent=to_agent,
            content=content,
            message_type=message_type,
        )
        self._messages[to_agent].append(message)

        # Emit message sent event
        self._store.append(EventDraft(
            type=EventType.AGENT_MESSAGE_SENT,
            agent_id=from_agent,
            task_id=task_id,
            payload={
                "message_id": message.id,
                "to_agent": to_agent,
                "content": content,
                "message_type": message_type,
            }
        ), stream=f"task:{task_id}")

        # Emit message received event (for the recipient)
        self._store.append(EventDraft(
            type=EventType.AGENT_MESSAGE_RECEIVED,
            agent_id=to_agent,
            task_id=task_id,
            payload={
                "message_id": message.id,
                "from_agent": from_agent,
                "content": content,
                "message_type": message_type,
            }
        ), stream=f"task:{task_id}")
        return message

    def get_messages(self, agent_id: str, unread_only: bool = False) -> list[AgentMessage]:
        with self._lock:
            """Gets messages for an agent."""
            messages = self._messages.get(agent_id, [])
            if unread_only:
                return [m for m in messages if m.read_at is None]
            return messages

    def mark_read(self, agent_id: str, message_id: str) -> bool:
        with self._lock:
            """Marks a message as read."""
            for msg in self._messages.get(agent_id, []):
                if msg.id == message_id:
                    msg.read_at = time.time()
                    return True
            return False

    def get_messages_for_task(self, agent_id: str, task_id: str) -> list[AgentMessage]:
        with self._lock:
            """Gets messages for an agent on a specific task."""
            return [m for m in self._messages.get(agent_id, []) if m.task_id == task_id]


class MultiAgentOrchestrator:
    """Multi-agent orchestrator for complex delegation patterns."""

    def __init__(self, subtask_manager: SubTaskManager, messaging: AgentMessaging):
        self._subtasks = subtask_manager
        self._messaging = messaging

    def run_sequential(self, parent_task_id: str, parent_execution_id: str,
                       parent_agent_id: str, steps: list[dict]) -> list[SubTask]:
        """
        Runs sub-tasks sequentially.
        steps: list of dicts with {agent_id, objective, condition, input}
        """
        subtasks = []
        previous_result: dict[str, Any] = {}
        for i, step in enumerate(steps):
            input_data = {**step.get("input", {}), "previous_result": previous_result}
            subtask = self._subtasks.delegate_task(
                parent_task_id=parent_task_id,
                parent_execution_id=parent_execution_id,
                agent_id=step["agent_id"],
                objective=step["objective"],
                condition=step["condition"],
                input=input_data,
                parent_agent_id=parent_agent_id
            )
            subtasks.append(subtask)
            # Delegation is asynchronous: the delegated agent picks the
            # sub-task up on its own schedule. Callers that need the
            # result must wait_for_subtasks() / poll aggregate_results().
        return subtasks

    def run_parallel(self, parent_task_id: str, parent_execution_id: str,
                     parent_agent_id: str, steps: list[dict]) -> list[SubTask]:
        """
        Runs sub-tasks in parallel.
        steps: list of dicts with {agent_id, objective, condition, input}
        """
        subtasks = []
        for step in steps:
            subtask = self._subtasks.delegate_task(
                parent_task_id=parent_task_id,
                parent_execution_id=parent_execution_id,
                agent_id=step["agent_id"],
                objective=step["objective"],
                condition=step["condition"],
                input=step.get("input", {}),
                parent_agent_id=parent_agent_id
            )
            subtasks.append(subtask)
        return subtasks

    def run_conditional(self, parent_task_id: str, parent_execution_id: str,
                        parent_agent_id: str, condition: dict,
                        true_steps: list[dict], false_steps: list[dict]) -> dict[str, list[dict]]:
        """
        Runs sub-tasks based on a condition.
        """
        # The real condition evaluation would happen at runtime
        # For now we return both branches so the caller decides
        return {
            "true_branch": true_steps,
            "false_branch": false_steps
        }

    def aggregate_results(self, parent_task_id: str) -> dict:
        """Aggregates results from all completed sub-tasks."""
        subtasks = self._subtasks.get_subtasks_for_task(parent_task_id)
        completed = [s for s in subtasks if s.status == SubTaskStatus.COMPLETED]
        failed = [s for s in subtasks if s.status == SubTaskStatus.FAILED]
        
        return {
            "completed_count": len(completed),
            "failed_count": len(failed),
            "total_count": len(subtasks),
            "results": {s.id: s.result for s in completed},
            "errors": {s.id: s.error for s in failed},
        }

    def notify_agents(self, task_id: str, from_agent: str, to_agents: list[str],
                      content: dict, message_type: str = "notification") -> list[AgentMessage]:
        """Sends a notification to multiple agents."""
        messages = []
        for to_agent in to_agents:
            msg = self._messaging.send_message(task_id, from_agent, to_agent, content, message_type)
            messages.append(msg)
        return messages