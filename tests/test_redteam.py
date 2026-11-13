"""Tests for Phase 5 (Red Team / Adversarial Testing Framework)."""
from vexinter import Vexinter
from vexinter.core.redteam import (
    FindingSeverity,
    FindingStatus,
    RedTeamEngine,
    RedTeamScenario,
)
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


def test_redteam_engine_standalone():
    """Prueba el RedTeamEngine de manera independiente."""
    from vexinter.core.store import InMemoryEventStore
    store = InMemoryEventStore()
    engine = RedTeamEngine(store)

    adapter = DummyAdapter("crm")
    engine.wrap_adapter("crm", adapter)

    # Inject fake success
    finding = engine.run_scenario(
        RedTeamScenario.FALSE_SUCCESS,
        target_system="crm",
        target_tool="write",
        test_args={"id": "123"},
        task_id="t1",
        execution_id="e1",
        agent_id="ag1"
    )

    assert finding.scenario == RedTeamScenario.FALSE_SUCCESS
    assert finding.severity == FindingSeverity.CRITICAL
    assert finding.status == FindingStatus.CONFIRMED
    assert "crm" in finding.system

    report = engine.generate_report()
    assert report["total_findings"] == 1
    assert report["critical_unmitigated"] == 1

    print("RedTeamEngine standalone test passed!")


def test_redteam_sdk_integration():
    """Tests the SDK integration with the RedTeam Framework."""
    adapter = DummyAdapter("crm")
    vex = Vexinter(embedded=True, adapters=[adapter])

    with vex.task(agent="ag1", objective="test redteam", condition={"op": "exists", "system": "crm", "query": {}}, input={}) as t:
        # Run individual scenario through the SDK
        finding = t.redteam_run_scenario("false_success", "crm", "write", {"id": "456"})
        assert finding.scenario.value == "false_success"

        # Run critical suite through the SDK
        suite_findings = t.redteam_run_suite("crm", "write", {"id": "789"}, critical_only=True)
        assert len(suite_findings) == 4

        # Obtener informe consolidado
        report = t.redteam_report()
        assert report["total_findings"] >= 5

    print("RedTeam SDK integration test passed!")


if __name__ == "__main__":
    test_redteam_engine_standalone()
    test_redteam_sdk_integration()
    print("All Red Team tests passed successfully!")
