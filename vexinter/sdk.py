from __future__ import annotations

from typing import TYPE_CHECKING, Self

if TYPE_CHECKING:
    from .remote import RemoteTask

import logging

from .core.conditions import (
    EvalContext,
    snapshot_key,
    substitute_refs,
    validate_condition,
)
from .core.coordinator import Plan
from .core.events import EventDraft, EventType, new_id
from .core.graph import execution_graph
from .core.runtime import LocalRuntime
from .core.statemachine import ExecutionState, state_from_verdict
from .core.store import EventQuery
from .core.taskrun import TaskRun
from .core.tools import CallContext, OperationRequest
from .identity.service import Principal

_log = logging.getLogger(__name__)


class _SystemTools:
    def __init__(self, run: TaskRun, system: str):
        self._run, self._system = run, system

    def __getattr__(self, tool: str):
        def _call(*args, **kwargs):
            if len(args) == 1 and isinstance(args[0], dict):
                a = args[0]
            elif not args:
                a = dict(kwargs)
            else:
                raise TypeError("pass a dict or named arguments")
            return self._run._intermediary.call(
                self._run.call_context(), f"{self._system}.{tool}", a)
        return _call


class ToolNamespace:
    def __init__(self, run: TaskRun, intermediary):
        run._intermediary = intermediary
        self._run = run

    def __getattr__(self, system: str):
        return _SystemTools(self._run, system)


class Task:
    """Supervised task context with FinOps support (§11.1)."""

    def __init__(self, rt: LocalRuntime, *, agent, objective, condition,
                 input=None, snapshot=None, principal=None, budget=None):
        self._rt = rt
        self.principal = principal
        self.agent = principal.agent_id if principal else agent
        self.objective = objective
        self.condition, self.input = condition, (input or {})
        self.snapshot = snapshot or []
        self.budget = budget
        self._run: TaskRun | None = None
        self._last_verdict = None

    def __enter__(self) -> Self:
        validate_condition(self.condition)
        from .models.budget import Budget
        run = TaskRun(task_id=new_id("task"), execution_id=new_id("exec"),
                      agent_id=self.agent, objective=self.objective,
                      condition=self.condition, input=self.input,
                      instance_id=(self.principal.instance_id
                                   if self.principal else None),
                      credential_id=(self.principal.credential_id
                                     if self.principal else None),
                      budget=self.budget if isinstance(self.budget, Budget) else None)
        rt = self._rt
        rt.store.append(EventDraft(
            type=EventType.TASK_CREATED, task_id=run.task_id,
            execution_id=run.execution_id, agent_id=run.agent_id,
            payload={"objective": run.objective, "condition": run.condition,
                     "input": run.input, "instance_id": run.instance_id,
                     "credential_id": run.credential_id,
                     "budget": run.budget.model_dump() if run.budget else None}),
            stream=run.stream)
        rt.store.append(EventDraft(
            type=EventType.EXECUTION_STARTED, task_id=run.task_id,
            execution_id=run.execution_id), stream=run.stream)

        ectx = EvalContext(run.input, {})
        for s in self.snapshot:
            q = substitute_refs(s["query"], ectx)
            obs = rt.router.observe(s["system"], q)
            run.pre_snapshot[snapshot_key(s["system"], q)] = obs
            rt.store.append(EventDraft(
                type=EventType.STATE_OBSERVED, task_id=run.task_id,
                execution_id=run.execution_id,
                payload={"tag": "pre", "system": s["system"], "query": q,
                         "observed": obs.data,
                         "consistency": obs.consistency}), stream=run.stream)

        rt.runs[run.task_id] = run
        self._run = run
        return self

    def __exit__(self, exc_type, exc, tb):
        run, rt = self.run, self._rt
        last = self._last_verdict.status if self._last_verdict else None
        rt.store.append(EventDraft(
            type=EventType.EXECUTION_COMPLETED, task_id=run.task_id,
            execution_id=run.execution_id,
            payload={"exception": exc_type is not None,
                     "last_status": last}), stream=run.stream)
        if self._last_verdict is not None:
            rt.memory.record_episode(run, self._last_verdict)
        else:
            target = (ExecutionState.INCONCLUSIVE if exc_type is not None
                      else ExecutionState.PENDING_VERIFICATION)
            if rt.tracker.can(run.execution_id, target):
                rt.tracker.transition(run, target,
                                      "execution ended without verification")
        return False

    @property
    def available_tools(self) -> dict:
        """Catalog of registered tools (§11.2): what an agent
        can discover to operate this execution."""
        self._require_open()
        return self._rt.intermediary.list_tools()

    @property
    def task_id(self) -> str:
        self._require_open()
        return self.run.task_id

    @property
    def execution_id(self) -> str:
        self._require_open()
        return self.run.execution_id

    @property
    def state(self) -> ExecutionState:
        return self._rt.tracker.current(self.run.execution_id)

    def call_as(self, principal, tool: str, args: dict):
        self._require_open()
        p = principal if isinstance(principal, Principal) else Principal(**principal)
        ctx = CallContext(agent_id=p.agent_id, instance_id=p.instance_id,
                          credential_id=p.credential_id,
                          task_id=self.run.task_id,
                          execution_id=self.run.execution_id,
                          stream=self.run.stream)
        return self._rt.intermediary.call(ctx, tool, args)

    @property
    def tools(self) -> ToolNamespace:
        self._require_open()
        return ToolNamespace(self.run, self._rt.intermediary)

    def verify(self):
        self._require_open()
        tr = self._rt.tracker
        if tr.can(self.run.execution_id, ExecutionState.PENDING_VERIFICATION):
            tr.transition(self.run, ExecutionState.PENDING_VERIFICATION,
                          "verification requested")
        v = self._rt.verifier.verify(self.run)
        self._last_verdict = v
        tr.transition(self.run, state_from_verdict(v.status, v.provisional),
                      f"verdict: {v.status}")
        if v.status == "inconclusive" or v.followup_required:
            self._rt.scheduler.schedule(self.run, self._rt.followup_delay_s,
                                        reason="inconclusive or provisional result (§4.4)")
        return v

    def recover(self, failed_tool: str | None = None,
                invoked_event: str | None = None):
        self._require_open()
        v = self._last_verdict or self.verify()
        return self._rt.recovery.handle_discrepancy(
            self.run, v, failed_tool=failed_tool,
            invoked_event=invoked_event)

    def run_plan(self, plan: Plan):
        self._require_open()
        try:
            v = self._rt.coordinator.run(plan, self.run)
        except Exception as e:
            # A crashed plan must not leave the execution stuck
            # IN_PROGRESS forever: record the failure, then propagate.
            try:
                tr = self._rt.tracker
                if tr.can(self.run.execution_id, ExecutionState.FAILED):
                    tr.transition(self.run, ExecutionState.FAILED,
                                  f"plan aborted: {type(e).__name__}: {e}")
            except Exception as be:  # noqa: BLE001 - bookkeeping must not mask the real error
                _log.debug("could not mark execution FAILED: %s", be)
            raise
        if v is not None and (v.status == "inconclusive"
                              or v.followup_required):
            self._rt.scheduler.schedule(
                self.run, self._rt.followup_delay_s,
                reason="inconclusive or provisional result (§4.4)")
        return v

    def run_followups(self, at=None):
        return self._rt.scheduler.run_due(at)

    def pending_cases(self):
        return self._rt.human_queue.pending()

    def case_options(self, case_id):
        return self._rt.human_queue.options(case_id)

    def decide(self, case_id, action, operator="operador"):
        self._require_open()
        res = self._rt.human_queue.decide(case_id, action, operator)
        self._rt.store.append(EventDraft(
            type=EventType.HITL_RESOLVED, task_id=self.run.task_id,
            execution_id=self.run.execution_id, agent_id=self.agent,
            payload={"case_id": case_id, "action": action, "operator": operator,
                     "result": res.status if hasattr(res, "status") else "resolved"}),
            stream=self.run.stream)
        return res

    def memory(self, **kw):
        return self._rt.memory.retrieve(**kw)

    def memory_block(self, **kw) -> str:
        """Memories ready for the agent context: DATA, not instructions."""
        return self._rt.memory.to_prompt_block(self.memory(**kw))

    def events(self):
        return self._rt.store.query(EventQuery(task_id=self.run.task_id))

    def audit(self):
        return self._rt.store.verify_chain(self.run.stream)

    def graph(self) -> dict:
        return execution_graph(self.events())

    # Phase 3: MCP Gateway
    def mcp_list_tools(self, system: str | None = None):
        """Lists tools available in MCP systems."""
        tools = {}
        for name, (adapter, spec) in self._rt.intermediary._by_tool.items():
            if system and not name.startswith(system + "."):
                continue
            tools[name] = spec.model_dump()
        return {"tools": tools}

    def mcp_invoke(self, system: str, tool: str, args: dict):
        """Invokes a tool through the MCP gateway."""
        self._require_open()
        adapter = None
        for a in self._rt.intermediary._router._by_system.values():
            if hasattr(a, 'system') and a.system == system:
                adapter = a
                break
        
        if adapter is None or not hasattr(adapter, '_client'):
            raise ValueError(f"MCP system not found: {system}")
        
        return adapter.invoke(tool, OperationRequest(
            tool=tool, args=args,
            task_id=self.run.task_id,
            execution_id=self.run.execution_id,
            agent_id=self.agent
        ))

    # Phase 4: Policy Engine
    def policy_list_rules(self):
        """Lists active policy rules."""
        return [
            {
                "id": r.id,
                "name": r.name,
                "description": r.description,
                "effect": r.effect.value,
                "conditions": r.conditions,
                "priority": r.priority
            }
            for r in self._rt.intermediary._policy.list_rules()
        ]

    def policy_add_rule(self, rule_id: str, name: str, description: str, effect: str, conditions: dict, priority: int = 0):
        """Adds a policy rule."""
        from .core.policy import PolicyDecision, PolicyRule
        rule = PolicyRule(
            id=rule_id,
            name=name,
            description=description,
            effect=PolicyDecision(effect),
            conditions=conditions,
            priority=priority
        )
        self._rt.intermediary._policy.add_rule(rule)
        return {"ok": True, "rule_id": rule.id}

    def policy_remove_rule(self, rule_id: str):
        """Removes a policy rule."""
        return self._rt.intermediary._policy.remove_rule(rule_id)

    def policy_evaluate(self, tool: str, args: dict):
        """Evaluates a tool against the policies."""
        from .core.policy import PolicyContext
        spec = None
        for name, (adapter, s) in self._rt.intermediary._by_tool.items():
            if name == tool or name.endswith("." + tool):
                spec = s
                break
        
        ctx = PolicyContext(
            agent_id=self.agent,
            tool=tool,
            args=args,
            task_id=self.run.task_id,
            execution_id=self.run.execution_id,
            metadata={"operation_class": spec.operation_class.value if spec else "read_only"}
        )
        return self._rt.intermediary._policy.evaluate(ctx).value

    # Phase 5: Red Team Framework
    def redteam_run_scenario(self, scenario: str, system: str, tool: str, args: dict):
        """Runs a red team scenario against a tool."""
        self._require_open()
        from .core.redteam import RedTeamScenario
        return self._rt.redteam.run_scenario(
            RedTeamScenario(scenario),
            target_system=system,
            target_tool=tool,
            test_args=args,
            task_id=self.run.task_id,
            execution_id=self.run.execution_id,
            agent_id=self.agent
        )

    def redteam_run_suite(self, system: str, tool: str, args: dict, critical_only: bool = False):
        """Runs a red team test suite."""
        self._require_open()
        from .core.redteam import CRITICAL_REDTEAM_SUITE, STANDARD_REDTEAM_SUITE
        suite = CRITICAL_REDTEAM_SUITE if critical_only else STANDARD_REDTEAM_SUITE
        return self._rt.redteam.run_suite(
            scenarios=suite,
            target_system=system,
            target_tool=tool,
            test_args=args,
            task_id=self.run.task_id,
            execution_id=self.run.execution_id,
            agent_id=self.agent
        )

    def redteam_findings(self, status: str | None = None):
        """Lists red team findings."""
        from .core.redteam import FindingStatus
        st = FindingStatus(status) if status else None
        return self._rt.redteam.list_findings(st)

    def redteam_report(self):
        """Generates a red team campaign report."""
        return self._rt.redteam.generate_report()

    # Phase 6: Advanced Memory & Learning
    def memory_get_lessons(self, tags: dict | None = None, min_confidence: float | None = None):
        """Gets lessons extracted from memory."""
        return self._rt.memory.get_lessons(tags, min_confidence)

    def memory_apply_lesson(self, lesson_id: str, success: bool):
        """Records application of a lesson."""
        return self._rt.memory.apply_lesson(lesson_id, success)

    def memory_search_similar(self, query: str, top_k: int = 10, min_score: float = 0.1):
        """Semantic similarity search in memory."""
        return self._rt.memory.search_similar(query, top_k, min_score)

    def memory_get_cross_task_patterns(self):
        """Gets patterns learned across tasks."""
        return self._rt.memory.get_cross_task_patterns()

    def memory_cleanup_expired(self):
        """Cleans up expired memories."""
        return self._rt.memory.cleanup_expired()

    # Phase 7: Multi-agent Orchestration
    def delegate_subtask(self, agent_id: str, objective: str, condition: dict,
                         input: dict | None = None) -> str:
        """Delegates a sub-task to another agent. Returns the subtask_id."""
        self._require_open()
        run = self.run
        subtask = self._rt.subtasks.delegate_task(
            parent_task_id=run.task_id,
            parent_execution_id=run.execution_id,
            agent_id=agent_id,
            objective=objective,
            condition=condition,
            input=input or {},
            parent_agent_id=run.agent_id
        )
        return subtask.id

    def get_subtasks(self) -> list[dict]:
        """Gets all sub-tasks of the current task."""
        self._require_open()
        run = self.run
        subtasks = self._rt.subtasks.get_subtasks_for_task(run.task_id)
        return [
            {
                "id": s.id,
                "agent_id": s.agent_id,
                "objective": s.objective,
                "status": s.status.value,
                "created_at": s.created_at,
                "started_at": s.started_at,
                "completed_at": s.completed_at,
                "result": s.result,
                "error": s.error,
            }
            for s in subtasks
        ]

    def get_subtask(self, subtask_id: str) -> dict | None:
        """Gets a sub-task by ID."""
        self._require_open()
        s = self._rt.subtasks.get_subtask(subtask_id)
        if not s:
            return None
        return {
            "id": s.id,
            "agent_id": s.agent_id,
            "objective": s.objective,
            "condition": s.condition,
            "input": s.input,
            "status": s.status.value,
            "created_at": s.created_at,
            "started_at": s.started_at,
            "completed_at": s.completed_at,
            "result": s.result,
            "error": s.error,
        }

    def send_agent_message(self, to_agent: str, content: dict,
                           message_type: str = "notification") -> str:
        """Sends a message to another agent in the same task. Returns message_id."""
        self._require_open()
        run = self.run
        msg = self._rt.agent_messaging.send_message(
            task_id=run.task_id,
            from_agent=run.agent_id,
            to_agent=to_agent,
            content=content,
            message_type=message_type
        )
        return msg.id

    def get_agent_messages(self, unread_only: bool = False) -> list[dict]:
        """Gets messages for the current agent."""
        self._require_open()
        run = self.run
        messages = self._rt.agent_messaging.get_messages(run.agent_id, unread_only)
        return [
            {
                "id": m.id,
                "task_id": m.task_id,
                "from_agent": m.from_agent,
                "content": m.content,
                "message_type": m.message_type,
                "created_at": m.created_at,
                "read_at": m.read_at,
            }
            for m in messages
        ]

    def mark_message_read(self, message_id: str) -> bool:
        """Marks a message as read."""
        self._require_open()
        run = self.run
        return self._rt.agent_messaging.mark_read(run.agent_id, message_id)

    def run_sequential_delegation(self, steps: list[dict]) -> list[str]:
        """Runs sequential delegation of sub-tasks. Returns list of subtask_ids."""
        self._require_open()
        run = self.run
        subtasks = self._rt.orchestrator.run_sequential(
            parent_task_id=run.task_id,
            parent_execution_id=run.execution_id,
            parent_agent_id=run.agent_id,
            steps=steps
        )
        return [s.id for s in subtasks]

    def run_parallel_delegation(self, steps: list[dict]) -> list[str]:
        """Runs parallel delegation of sub-tasks. Returns list of subtask_ids."""
        self._require_open()
        run = self.run
        subtasks = self._rt.orchestrator.run_parallel(
            parent_task_id=run.task_id,
            parent_execution_id=run.execution_id,
            parent_agent_id=run.agent_id,
            steps=steps
        )
        return [s.id for s in subtasks]

    def aggregate_subtask_results(self) -> dict:
        """Aggregates results of all sub-tasks."""
        self._require_open()
        run = self.run
        return self._rt.orchestrator.aggregate_results(run.task_id)

    def notify_agents(self, to_agents: list[str], content: dict,
                      message_type: str = "notification") -> list[str]:
        """Notifies multiple agents. Returns list of message_ids."""
        self._require_open()
        run = self.run
        messages = self._rt.orchestrator.notify_agents(
            task_id=run.task_id,
            from_agent=run.agent_id,
            to_agents=to_agents,
            content=content,
            message_type=message_type
        )
        return [m.id for m in messages]

    @property
    def run(self) -> TaskRun:
        """Active TaskRun. Outside the context ('with') raises RuntimeError."""
        if self._run is None:
            raise RuntimeError("the task is not started (use 'with')")
        return self._run

    def _require_open(self):
        _ = self.run  # validates that the task is started


class Vexinter:
    """SDK entry point (§11.1).

    - Vexinter(embedded=True, adapters=[...])  → all in one process (§12.1)
    - Vexinter(endpoint=..., credentials=...)  → against a service (§12.2/12.3)
    """

    def __init__(self, endpoint=None, credentials=None, *, embedded=False,
                 adapters=None, authorizer=None, identity_service=None,
                 authenticated=False, followup_delay_s=30.0, admin_token=None,
                 transport=None):
        if endpoint is not None:
            from .remote import RemoteRuntime
            self._remote = RemoteRuntime(endpoint, credentials,
                                         admin_token=admin_token,
                                         transport=transport)
            self._rt = None
            return
        self._remote = None
        self._rt = LocalRuntime(adapters or [], authorizer=authorizer,
                                identity_service=identity_service,
                                authenticated=authenticated,
                                followup_delay_s=followup_delay_s)

    @property
    def remote(self):
        if self._remote is None:
            raise RuntimeError("this client is not connected to a service")
        return self._remote

    @property
    def identity(self):
        return self._rt.identity

    @property
    def messaging(self):
        return self._rt.messaging

    def task(self, *, agent, objective, condition, input=None, snapshot=None,
             principal=None, credentials=None) -> Task | RemoteTask:
        if self._remote is not None:
            from .remote import RemoteTask
            return RemoteTask(self._remote, agent=agent, objective=objective,
                              condition=condition, input=input,
                              snapshot=snapshot, credentials=credentials)
        return Task(self._rt, agent=agent, objective=objective,
                    condition=condition, input=input, snapshot=snapshot,
                    principal=principal)