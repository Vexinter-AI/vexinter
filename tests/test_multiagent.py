"""Tests for Phase 7 (Multi-agent Orchestration)."""
from vexinter import Vexinter
from vexinter.core.events import EventType
from vexinter.core.multiagent import (
    AgentMessaging,
    MultiAgentOrchestrator,
    SubTaskManager,
    SubTaskStatus,
)
from vexinter.core.store import InMemoryEventStore
from vexinter.core.tools import (
    Observation,
    OperationClass,
    OperationRequest,
    OperationResult,
    ToolAdapter,
    ToolSpec,
)


class DummyAdapter(ToolAdapter):
    def __init__(self, system: str = "sys"):
        self.system = system

    def tools(self) -> dict[str, ToolSpec]:
        return {
            "write": ToolSpec(
                system=self.system,
                name="write",
                description="Write data",
                input_schema={"type": "object"},
                operation_class=OperationClass.EFFECTFUL_IRREVERSIBLE
            )
        }

    def invoke(self, tool: str, req: OperationRequest) -> OperationResult:
        return OperationResult(ok=True, data={"written": True})

    def observe(self, query: dict) -> Observation:
        return Observation(data={"value": "ok"}, observed_at=None, source=self.system, consistency="strong")


def test_subtask_manager():
    """Prueba el gestor de sub-tareas."""
    store = InMemoryEventStore()
    
    def task_runner_factory(task_id, execution_id, agent_id, objective, condition, input, instance_id=None, credential_id=None):
        from vexinter.core.taskrun import TaskRun
        return TaskRun(
            task_id=task_id,
            execution_id=execution_id,
            agent_id=agent_id,
            objective=objective,
            condition=condition,
            input=input,
            instance_id=instance_id,
            credential_id=credential_id,
        )
    
    manager = SubTaskManager(store, task_runner_factory)
    
    # Crear sub-tarea
    subtask = manager.create_subtask(
        parent_task_id="task1",
        parent_execution_id="exec1",
        agent_id="agent2",
        objective="Procesar pago",
        condition={"op": "exists", "system": "sys", "query": {}},
        input={"amount": 100},
        parent_agent_id="agent1"
    )
    
    assert subtask.id.startswith("subtask_")
    assert subtask.agent_id == "agent2"
    assert subtask.status == SubTaskStatus.PENDING
    
    # Verificar que se encuentra
    found = manager.get_subtask(subtask.id)
    assert found is not None
    assert found.id == subtask.id
    
    # Verificar lista de sub-tareas
    subtasks = manager.get_subtasks_for_task("task1")
    assert len(subtasks) == 1
    assert subtasks[0].id == subtask.id
    
    # Delegar tarea (con evento TASK_DELEGATED)
    manager.delegate_task(
        parent_task_id="task1",
        parent_execution_id="exec1",
        agent_id="agent3",
        objective="Send notification",
        condition={"op": "exists", "system": "sys", "query": {}},
        input={"message": "Hola"},
        parent_agent_id="agent1"
    )
    
    # Verificar eventos en el store
    events = store.query({})
    subtask_created_events = [e for e in events if e.type == EventType.SUB_TASK_CREATED]
    task_delegated_events = [e for e in events if e.type == EventType.TASK_DELEGATED]
    assert len(subtask_created_events) == 2
    assert len(task_delegated_events) == 1
    
    # Cambiar estado
    manager.start_subtask(subtask.id, "exec_sub1")
    assert subtask.status == SubTaskStatus.RUNNING
    assert subtask.started_at is not None
    
    manager.complete_subtask(subtask.id, {"result": "ok"})
    assert subtask.status == SubTaskStatus.COMPLETED
    assert subtask.result == {"result": "ok"}
    assert subtask.completed_at is not None
    
    # Verificar evento de completado
    events = store.query({})
    completed_events = [e for e in events if e.type == EventType.SUB_TASK_COMPLETED]
    assert len(completed_events) == 1
    
    print("SubTaskManager test passed!")


def test_agent_messaging():
    """Tests the agent-to-agent messaging system."""
    store = InMemoryEventStore()
    messaging = AgentMessaging(store)
    
    # Enviar mensaje
    msg = messaging.send_message(
        task_id="task1",
        from_agent="agent1",
        to_agent="agent2",
        content={"action": "process", "data": {"id": 123}},
        message_type="request"
    )
    
    assert msg.id.startswith("msg_")
    assert msg.from_agent == "agent1"
    assert msg.to_agent == "agent2"
    assert msg.message_type == "request"
    
    # Obtener mensajes
    messages = messaging.get_messages("agent2")
    assert len(messages) == 1
    assert messages[0].id == msg.id
    
    # Unread messages
    unread = messaging.get_messages("agent2", unread_only=True)
    assert len(unread) == 1
    
    # Mark as read
    assert messaging.mark_read("agent2", msg.id)
    unread = messaging.get_messages("agent2", unread_only=True)
    assert len(unread) == 0
    
    # Messages for a specific task
    task_messages = messaging.get_messages_for_task("agent2", "task1")
    assert len(task_messages) == 1
    
    # Verificar eventos
    events = store.query({})
    sent_events = [e for e in events if e.type == EventType.AGENT_MESSAGE_SENT]
    received_events = [e for e in events if e.type == EventType.AGENT_MESSAGE_RECEIVED]
    assert len(sent_events) == 1
    assert len(received_events) == 1
    
    print("AgentMessaging test passed!")


def test_multiagent_orchestrator():
    """Prueba el orquestador multi-agente."""
    store = InMemoryEventStore()
    
    def task_runner_factory(task_id, execution_id, agent_id, objective, condition, input, instance_id=None, credential_id=None):
        from vexinter.core.taskrun import TaskRun
        return TaskRun(
            task_id=task_id,
            execution_id=execution_id,
            agent_id=agent_id,
            objective=objective,
            condition=condition,
            input=input,
            instance_id=instance_id,
            credential_id=credential_id,
        )
    
    subtask_manager = SubTaskManager(store, task_runner_factory)
    messaging = AgentMessaging(store)
    orchestrator = MultiAgentOrchestrator(subtask_manager, messaging)
    
    # Parallel delegation
    steps = [
        {"agent_id": "agent2", "objective": "Paso 1", "condition": {"op": "exists", "system": "sys", "query": {}}, "input": {}},
        {"agent_id": "agent3", "objective": "Paso 2", "condition": {"op": "exists", "system": "sys", "query": {}}, "input": {}},
    ]
    
    subtasks = orchestrator.run_parallel(
        parent_task_id="task1",
        parent_execution_id="exec1",
        parent_agent_id="agent1",
        steps=steps
    )
    
    assert len(subtasks) == 2
    assert subtasks[0].agent_id == "agent2"
    assert subtasks[1].agent_id == "agent3"
    
    # Sequential delegation
    steps_seq = [
        {"agent_id": "agent2", "objective": "Paso A", "condition": {"op": "exists", "system": "sys", "query": {}}, "input": {}},
        {"agent_id": "agent3", "objective": "Paso B", "condition": {"op": "exists", "system": "sys", "query": {}}, "input": {}},
    ]
    
    subtasks_seq = orchestrator.run_sequential(
        parent_task_id="task1",
        parent_execution_id="exec1",
        parent_agent_id="agent1",
        steps=steps_seq
    )
    
    assert len(subtasks_seq) == 2
    
    # Agregar resultados
    for s in subtasks:
        subtask_manager.complete_subtask(s.id, {"output": f"result_{s.id}"})
    
    for s in subtasks_seq:
        subtask_manager.complete_subtask(s.id, {"output": f"result_{s.id}"})
    
    # Agregar resultados
    aggregated = orchestrator.aggregate_results("task1")
    assert aggregated["total_count"] == 4
    assert aggregated["completed_count"] == 4
    assert len(aggregated["results"]) == 4
    
    # Notificar agentes
    messages = orchestrator.notify_agents(
        task_id="task1",
        from_agent="agent1",
        to_agents=["agent2", "agent3"],
        content={"notification": "done"},
        message_type="notification"
    )
    
    assert len(messages) == 2
    
    print("MultiAgentOrchestrator test passed!")


def test_multiagent_sdk():
    """Tests SDK integration with multi-agent."""
    adapter = DummyAdapter("sys")
    vex = Vexinter(embedded=True, adapters=[adapter])
    
    with vex.task(agent="ag1", objective="test multiagent", 
                  condition={"op": "exists", "system": "sys", "query": {}}, input={}) as t:
        # Test delegate_subtask
        subtask_id = t.delegate_subtask(
            agent_id="ag2",
            objective="Procesar datos",
            condition={"op": "exists", "system": "sys", "query": {}},
            input={"data": "test"}
        )
        assert subtask_id.startswith("subtask_")
        
        # Test get_subtasks
        subtasks = t.get_subtasks()
        assert len(subtasks) == 1
        assert subtasks[0]["id"] == subtask_id
        assert subtasks[0]["agent_id"] == "ag2"
        
        # Test get_subtask
        subtask = t.get_subtask(subtask_id)
        assert subtask is not None
        assert subtask["id"] == subtask_id
        
        # Test send_agent_message
        msg_id = t.send_agent_message("ag2", {"action": "notify"}, "notification")
        assert msg_id.startswith("msg_")
        
        # Test get_agent_messages
        _messages = t.get_agent_messages()
        # Los mensajes van al destinatario, no al remitente
        # En este test, el remitente es ag1, destinatario es ag2
        
        # Test run_parallel_delegation
        subtask_ids = t.run_parallel_delegation([
            {"agent_id": "ag2", "objective": "Paso 1", "condition": {"op": "exists", "system": "sys", "query": {}}, "input": {}},
            {"agent_id": "ag3", "objective": "Paso 2", "condition": {"op": "exists", "system": "sys", "query": {}}, "input": {}},
        ])
        assert len(subtask_ids) == 2
        
        # Test run_sequential_delegation
        subtask_ids = t.run_sequential_delegation([
            {"agent_id": "ag2", "objective": "Paso A", "condition": {"op": "exists", "system": "sys", "query": {}}, "input": {}},
            {"agent_id": "ag3", "objective": "Paso B", "condition": {"op": "exists", "system": "sys", "query": {}}, "input": {}},
        ])
        assert len(subtask_ids) == 2
        
        # Test aggregate_subtask_results
        agg = t.aggregate_subtask_results()
        assert "total_count" in agg
        
        # Test notify_agents
        msg_ids = t.notify_agents(["ag2", "ag3"], {"notify": "all"})
        assert len(msg_ids) == 2
    
    print("Multi-agent SDK integration test passed!")


if __name__ == "__main__":
    test_subtask_manager()
    test_agent_messaging()
    test_multiagent_orchestrator()
    test_multiagent_sdk()
    print("All Multi-agent Orchestration tests passed successfully!")