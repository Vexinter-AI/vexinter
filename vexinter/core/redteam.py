"""Red Team / Adversarial Testing Framework (Fase 5).

Framework for adversarial fault injection and resilience validation.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any
from uuid import uuid4

from .events import REDTEAM_FINDING, EventDraft, now
from .tools import (
    Observation,
    OperationRequest,
    OperationResult,
    ToolAdapter,
    ToolSpec,
)

_log = logging.getLogger(__name__)

class RedTeamScenario(str, Enum):
    """Types of predefined adversarial scenarios."""
    FALSE_SUCCESS = "false_success"              # Responds ok but does not persist
    FILTERED_EFFECT = "filtered_effect"          # Responds error but persists
    LOST_RESPONSE = "lost_response"              # Persists but no response
    NETWORK_BEFORE = "network_before"            # Does not persist, no response
    STALE_REPLICA = "stale_replica"              # Eventual read
    HONEST_BLIND = "honest_blind"                # Writes correctly but cannot be read
    BLIND_FALSE_SUCCESS = "blind_false_success"  # Cannot be read, always lies
    AMBIGUOUS_RESPONSE = "ambiguous_response"    # Responds "pending confirmation"


class FindingSeverity(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class FindingStatus(str, Enum):
    OPEN = "open"
    INVESTIGATING = "investigating"
    CONFIRMED = "confirmed"
    MITIGATED = "mitigated"
    FALSE_POSITIVE = "false_positive"


@dataclass
class RedTeamFinding:
    """Red team finding."""
    id: str
    scenario: RedTeamScenario
    severity: FindingSeverity
    status: FindingStatus
    title: str
    description: str
    tool: str
    system: str
    evidence: dict[str, Any]
    recommendation: str
    created_at: datetime = field(default_factory=datetime.now)
    mitigated_at: datetime | None = None


class RedTeamFaultInjector:
    """Fault injector for adapters."""

    def __init__(self, base_adapter: ToolAdapter):
        self._base = base_adapter
        self.system = base_adapter.system  # Delegate system to the base adapter
        self._faults: dict[str, Callable] = {}
        self._active_scenario: RedTeamScenario | None = None
        self._fault_config: dict = {}

    def inject(self, scenario: RedTeamScenario, config: dict | None = None):
        """Activates a fault injection scenario."""
        self._active_scenario = scenario
        self._fault_config = config or {}

    def clear(self):
        """Deactivates all faults."""
        self._active_scenario = None
        self._fault_config = {}

    def snapshot(self) -> dict:
        """Active fault state for diagnostics: which scenario/config was
        injected when a finding was recorded."""
        return {
            "scenario": (self._active_scenario.value
                         if self._active_scenario is not None else None),
            "config": dict(self._fault_config),
        }

    def tools(self) -> dict[str, ToolSpec]:
        return self._base.tools()

    def __getattr__(self, name: str):
        """Delegates attributes not found to the base adapter."""
        return getattr(self._base, name)

    def invoke(self, tool: str, req: OperationRequest) -> OperationResult:
        if self._active_scenario is None:
            return self._base.invoke(tool, req)

        fault_fn = self._get_fault_handler(self._active_scenario)
        if fault_fn:
            return fault_fn(tool, req)

        return self._base.invoke(tool, req)

    def _get_fault_handler(self, scenario: RedTeamScenario):
        handlers = {
            RedTeamScenario.FALSE_SUCCESS: self._fault_false_success,
            RedTeamScenario.FILTERED_EFFECT: self._fault_filtered_effect,
            RedTeamScenario.LOST_RESPONSE: self._fault_lost_response,
            RedTeamScenario.NETWORK_BEFORE: self._fault_network_before,
            RedTeamScenario.STALE_REPLICA: self._fault_stale_replica,
            RedTeamScenario.HONEST_BLIND: self._fault_honest_blind,
            RedTeamScenario.BLIND_FALSE_SUCCESS: self._fault_blind_false_success,
            RedTeamScenario.AMBIGUOUS_RESPONSE: self._fault_ambiguous_response,
        }
        return handlers.get(scenario)

    def _fault_false_success(self, tool: str, req: OperationRequest) -> OperationResult:
        # Responds ok but does not actually execute
        return OperationResult(ok=True, data={"injected": "false_success", "tool": tool})

    def _fault_filtered_effect(self, tool: str, req: OperationRequest) -> OperationResult:
        # Executes but reports an error
        real_result = self._base.invoke(tool, req)
        if real_result.ok:
            return OperationResult(ok=False, error="injected: filtered_effect", data=real_result.data)
        return real_result

    def _fault_lost_response(self, tool: str, req: OperationRequest) -> OperationResult:
        # Executes correctly but loses the response
        real_result = self._base.invoke(tool, req)
        if real_result.ok:
            # Simulate a lost response: the operation happened but there is no return
            from vexinter.core.tools import IndeterminateOutcome
            raise IndeterminateOutcome("injected: lost_response")
        return real_result

    def _fault_network_before(self, tool: str, req: OperationRequest) -> OperationResult:
        # Network failure before executing
        from vexinter.core.tools import IndeterminateOutcome
        raise IndeterminateOutcome("injected: network_before")

    def _fault_stale_replica(self, tool: str, req: OperationRequest) -> OperationResult:
        # Normal invocation, but the observation will be eventual
        return self._base.invoke(tool, req)

    def _fault_honest_blind(self, tool: str, req: OperationRequest) -> OperationResult:
        # Writes correctly but observe fails
        return self._base.invoke(tool, req)

    def _fault_blind_false_success(self, tool: str, req: OperationRequest) -> OperationResult:
        # Cannot be read and it lies
        return OperationResult(ok=True, data={"injected": "blind_false_success"})

    def _fault_ambiguous_response(self, tool: str, req: OperationRequest) -> OperationResult:
        return OperationResult(ok=False, data={"status": "pending_confirmation"},
                               error="ambiguous_response")

    def observe(self, query: dict) -> Observation:
        if self._active_scenario == RedTeamScenario.STALE_REPLICA:
            # Simulate eventual read
            return Observation(data=None, observed_at=now(), source=f"{self._base.system}/redteam", consistency="eventual")
        if self._active_scenario == RedTeamScenario.HONEST_BLIND:
            # Cannot be read
            return Observation(data=None, observed_at=now(), source=f"{self._base.system}/redteam", consistency="unknown")
        if self._active_scenario == RedTeamScenario.BLIND_FALSE_SUCCESS:
            # Lies in the read
            return Observation(data={"lied": True}, observed_at=now(), source=f"{self._base.system}/redteam", consistency="strong")
        return self._base.observe(query)


class RedTeamEngine:
    """Red Team engine for adversarial testing."""

    def __init__(self, store: Any):
        self._store = store
        self._findings: list[RedTeamFinding] = []
        self._adapters: dict[str, RedTeamFaultInjector] = {}
        self._running = False
        self._current_run_id: str | None = None

    def wrap_adapter(self, system: str, adapter: ToolAdapter) -> RedTeamFaultInjector:
        """Wraps an adapter with fault injection capability."""
        injector = RedTeamFaultInjector(adapter)
        self._adapters[system] = injector
        return injector

    def get_injector(self, system: str) -> RedTeamFaultInjector | None:
        return self._adapters.get(system)

    def run_scenario(
        self,
        scenario: RedTeamScenario,
        target_system: str,
        target_tool: str,
        test_args: dict,
        task_id: str,
        execution_id: str,
        agent_id: str,
        expected_behavior: str = "should_detect_or_prevent",
    ) -> RedTeamFinding:
        """Runs a red team scenario against a tool."""
        run_id = str(uuid4())
        self._current_run_id = run_id
        self._running = True

        injector = self._adapters.get(target_system)
        if not injector:
            raise ValueError(f"System {target_system} not wrapped for red team")

        # Inject the fault
        injector.inject(scenario)

        finding = RedTeamFinding(
            id=str(uuid4()),
            scenario=scenario,
            severity=self._severity_for_scenario(scenario),
            status=FindingStatus.OPEN,
            title=f"Red Team: {scenario.value} on {target_system}.{target_tool}",
            description=self._describe_scenario(scenario),
            tool=target_tool,
            system=target_system,
            evidence={
                "run_id": run_id,
                "test_args": test_args,
                "task_id": task_id,
                "execution_id": execution_id,
                "expected_behavior": expected_behavior,
            },
            recommendation=self._recommendation_for_scenario(scenario),
        )

        try:
            # Run the tool under the adversarial scenario
            from vexinter.core.tools import OperationRequest
            req = OperationRequest(
                tool=target_tool,
                args=test_args,
                task_id=task_id,
                execution_id=execution_id,
                agent_id=agent_id,
            )
            result = injector.invoke(target_tool, req)

            # Evaluate the result
            finding = self._evaluate_result(finding, result, scenario)

        except Exception as e:  # noqa: BLE001 - error boundary: converted to result/log
            finding = self._evaluate_exception(finding, e, scenario)
        finally:
            # Snapshot before clearing: if evaluation raised, the finding
            # must still record which fault was active.
            try:
                finding.evidence["injector_state"] = injector.snapshot()
            except Exception as e:  # noqa: BLE001 - diagnostics must not mask
                _log.debug("could not snapshot injector state: %s", e)
            injector.clear()
            self._running = False

        # Record the finding
        self._findings.append(finding)
        self._emit_finding_event(finding, task_id, execution_id, agent_id)

        return finding

    def run_suite(
        self,
        scenarios: list[RedTeamScenario],
        target_system: str,
        target_tool: str,
        test_args: dict,
        task_id: str,
        execution_id: str,
        agent_id: str,
    ) -> list[RedTeamFinding]:
        """Runs a full suite of scenarios."""
        findings = []
        for scenario in scenarios:
            finding = self.run_scenario(
                scenario, target_system, target_tool, test_args,
                task_id, execution_id, agent_id
            )
            findings.append(finding)
            # Short pause between scenarios
            time.sleep(0.01)
        return findings

    def _severity_for_scenario(self, scenario: RedTeamScenario) -> FindingSeverity:
        critical = {
            RedTeamScenario.FALSE_SUCCESS,
            RedTeamScenario.FILTERED_EFFECT,
            RedTeamScenario.LOST_RESPONSE,
        }
        high = {
            RedTeamScenario.NETWORK_BEFORE,
            RedTeamScenario.BLIND_FALSE_SUCCESS,
            RedTeamScenario.AMBIGUOUS_RESPONSE,
        }
        if scenario in critical:
            return FindingSeverity.CRITICAL
        if scenario in high:
            return FindingSeverity.HIGH
        return FindingSeverity.MEDIUM

    def _describe_scenario(self, scenario: RedTeamScenario) -> str:
        descriptions = {
            RedTeamScenario.FALSE_SUCCESS:
                "The system reports success but does not persist the effect. Vexinter must detect the discrepancy.",
            RedTeamScenario.FILTERED_EFFECT:
                "The system reports an error but the effect is persisted. Vexinter must detect and compensate.",
            RedTeamScenario.LOST_RESPONSE:
                "The effect happens but the response is lost on the network. Vexinter must escalate without duplicating.",
            RedTeamScenario.NETWORK_BEFORE:
                "Network failure before execution. Vexinter must mark it as an unknown outcome.",
            RedTeamScenario.STALE_REPLICA:
                "Verification reads from a stale replica. Vexinter must mark it as 'inconclusive'.",
            RedTeamScenario.HONEST_BLIND:
                "The write works but cannot be verified independently. Vexinter must escalate.",
            RedTeamScenario.BLIND_FALSE_SUCCESS:
                "It cannot be verified and the system lies consistently. Vexinter must detect and escalate.",
            RedTeamScenario.AMBIGUOUS_RESPONSE:
                "Ambiguous 'pending' response. Vexinter must treat it as an unknown outcome.",
        }
        return descriptions.get(scenario, "Adversarial test scenario.")

    def _recommendation_for_scenario(self, scenario: RedTeamScenario) -> str:
        recommendations = {
            RedTeamScenario.FALSE_SUCCESS:
                "Configure effect probing (ToolProbe) and enable retry_approval='human' for irreversible writes.",
            RedTeamScenario.FILTERED_EFFECT:
                "Declare compensation (ToolCompensation) and use independent post-execution verification.",
            RedTeamScenario.LOST_RESPONSE:
                "Use idempotency keys per logical operation; do not retry without verification.",
            RedTeamScenario.NETWORK_BEFORE:
                "Treat IndeterminateOutcome as a first-class outcome; escalate to human decision.",
            RedTeamScenario.STALE_REPLICA:
                "Accept 'inconclusive' verdicts and schedule a later review (within_window).",
            RedTeamScenario.HONEST_BLIND:
                "Require independent observability; if not possible, require human approval.",
            RedTeamScenario.BLIND_FALSE_SUCCESS:
                "Mark the system as untrustworthy; require external verification or reject operations.",
            RedTeamScenario.AMBIGUOUS_RESPONSE:
                "Treat ambiguous responses as IndeterminateOutcome; assume neither success nor failure.",
        }
        return recommendations.get(scenario, "Review adapter configuration and recovery policies.")

    def _evaluate_result(self, finding: RedTeamFinding, result: OperationResult, scenario: RedTeamScenario) -> RedTeamFinding:
        """Evaluates whether the system behaved correctly under the scenario."""
        # By default, if the tool responded ok when it should not have, it is a confirmed finding
        if scenario in {RedTeamScenario.FALSE_SUCCESS, RedTeamScenario.BLIND_FALSE_SUCCESS}:
            if result.ok:
                finding.status = FindingStatus.CONFIRMED
                finding.severity = FindingSeverity.CRITICAL
            else:
                finding.status = FindingStatus.MITIGATED
        elif scenario == RedTeamScenario.FILTERED_EFFECT:
            if not result.ok and result.data:
                finding.status = FindingStatus.CONFIRMED
            else:
                finding.status = FindingStatus.MITIGATED
        elif scenario in {RedTeamScenario.LOST_RESPONSE, RedTeamScenario.NETWORK_BEFORE}:
            # These raise exceptions, they do not reach here
            finding.status = FindingStatus.INVESTIGATING
        else:
            finding.status = FindingStatus.INVESTIGATING

        finding.evidence["actual_result"] = {"ok": result.ok, "data": result.data, "error": result.error}
        return finding

    def _evaluate_exception(self, finding: RedTeamFinding, exc: Exception, scenario: RedTeamScenario) -> RedTeamFinding:
        finding.evidence["exception"] = {"type": type(exc).__name__, "message": str(exc)}

        if scenario in {RedTeamScenario.LOST_RESPONSE, RedTeamScenario.NETWORK_BEFORE}:
            from vexinter.core.tools import IndeterminateOutcome
            if isinstance(exc, IndeterminateOutcome):
                finding.status = FindingStatus.CONFIRMED
            else:
                finding.status = FindingStatus.INVESTIGATING
        else:
            finding.status = FindingStatus.INVESTIGATING

        return finding

    def _emit_finding_event(self, finding: RedTeamFinding, task_id: str, execution_id: str, agent_id: str):
        self._store.append(EventDraft(
            type=REDTEAM_FINDING,
            task_id=task_id,
            execution_id=execution_id,
            agent_id=agent_id,
            payload={
                "finding_id": finding.id,
                "scenario": finding.scenario.value,
                "severity": finding.severity.value,
                "status": finding.status.value,
                "title": finding.title,
                "description": finding.description,
                "tool": finding.tool,
                "system": finding.system,
                "evidence": finding.evidence,
                "recommendation": finding.recommendation,
            }
        ), stream=task_id)

    def list_findings(self, status: FindingStatus | None = None) -> list[RedTeamFinding]:
        if status:
            return [f for f in self._findings if f.status == status]
        return list(self._findings)

    def get_finding(self, finding_id: str) -> RedTeamFinding | None:
        for f in self._findings:
            if f.id == finding_id:
                return f
        return None

    def update_finding_status(self, finding_id: str, status: FindingStatus):
        finding = self.get_finding(finding_id)
        if finding:
            finding.status = status
            if status == FindingStatus.MITIGATED:
                finding.mitigated_at = datetime.now()

    def generate_report(self) -> dict:
        """Generates a report of the red team campaign."""
        total = len(self._findings)
        by_status: dict[str, int] = {}
        by_severity: dict[str, int] = {}
        for f in self._findings:
            by_status[f.status.value] = by_status.get(f.status.value, 0) + 1
            by_severity[f.severity.value] = by_severity.get(f.severity.value, 0) + 1

        critical_unmitigated = sum(1 for f in self._findings
                                   if f.severity == FindingSeverity.CRITICAL
                                   and f.status not in {FindingStatus.MITIGATED, FindingStatus.FALSE_POSITIVE})

        return {
            "run_id": self._current_run_id,
            "total_findings": total,
            "by_status": by_status,
            "by_severity": by_severity,
            "critical_unmitigated": critical_unmitigated,
            "findings": [
                {
                    "id": f.id,
                    "scenario": f.scenario.value,
                    "severity": f.severity.value,
                    "status": f.status.value,
                    "title": f.title,
                    "tool": f.tool,
                    "system": f.system,
                }
                for f in self._findings
            ],
        }


# Predefined suite of standard scenarios
STANDARD_REDTEAM_SUITE = [
    RedTeamScenario.FALSE_SUCCESS,
    RedTeamScenario.FILTERED_EFFECT,
    RedTeamScenario.LOST_RESPONSE,
    RedTeamScenario.NETWORK_BEFORE,
    RedTeamScenario.STALE_REPLICA,
    RedTeamScenario.HONEST_BLIND,
    RedTeamScenario.BLIND_FALSE_SUCCESS,
    RedTeamScenario.AMBIGUOUS_RESPONSE,
]

# Critical suite (only those that can cause data/money loss)
CRITICAL_REDTEAM_SUITE = [
    RedTeamScenario.FALSE_SUCCESS,
    RedTeamScenario.FILTERED_EFFECT,
    RedTeamScenario.LOST_RESPONSE,
    RedTeamScenario.BLIND_FALSE_SUCCESS,
]