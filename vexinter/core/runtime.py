from __future__ import annotations

import logging
import os
import sys

from ..identity import IdentityAwareAuthorizer, IdentityService, MessageService
from .coordinated import CoordinatedRecovery
from .coordinator import Coordinator
from .followup import FollowupScheduler
from .memory import LessonExtractor, MemoryDecay, MemoryIndex, MemoryService
from .multiagent import AgentMessaging, MultiAgentOrchestrator, SubTaskManager
from .observability import (
    CircuitBreaker,
    ConfigValidator,
    GracefulShutdown,
    HealthChecker,
    HealthStatus,
    RateLimiter,
    prometheus_metrics,
    setup_structured_logging,
)
from .policy import PolicyEngine
from .recovery import HumanDecisionQueue, RecoveryEngine
from .redteam import RedTeamEngine
from .statemachine import ExecutionTracker
from .store import InMemoryEventStore
from .taskrun import TaskRun
from .tools import Intermediary, SourceRouter, StaticAuthorizer
from .verifier import Verifier


class LocalRuntime:
    """Assembles all components in a single process (modality §12.1).

    `authenticated=False` is equivalent to a permissive StaticAuthorizer —
    ONLY for development and testing (§14). With `authenticated=True`,
    every invocation and every message requires a valid credential
    (fail-closed).
    """

    def __init__(self, adapters, authorizer=None, identity_service=None,
                 authenticated=False, followup_delay_s=30.0, store=None):
        self.followup_delay_s = followup_delay_s
        self.store = store or InMemoryEventStore()
        self.tracker = ExecutionTracker(self.store)
        self.identity = identity_service or IdentityService(self.store)
        self.messaging = MessageService(self.identity, self.store)
        # Red Team Engine (Fase 5): wrap adapters BEFORE building the
        # router/verifier/intermediary so each is created exactly once.
        self.redteam = RedTeamEngine(self.store)
        wrapped_adapters = [
            self.redteam.wrap_adapter(a.system, a) for a in adapters]
        self.router = SourceRouter(wrapped_adapters)
        self.verifier = Verifier(self.store, self.router)
        if authorizer is None:
            authorizer = (IdentityAwareAuthorizer(self.identity)
                          if authenticated else StaticAuthorizer())
        # Policy Engine (Fase 4)
        policy_engine = PolicyEngine(self.store)
        self.intermediary = Intermediary(
            self.store, authorizer, wrapped_adapters,
            policy_engine=policy_engine)

        self.human_queue = HumanDecisionQueue(self.store, self.verifier,
                                               self.intermediary, self.tracker)
        self.recovery = RecoveryEngine(self.store, self.verifier,
                                        self.intermediary, self.human_queue,
                                        tracker=self.tracker)
        self.coordinated = CoordinatedRecovery(self.store, self.intermediary,
                                               self.verifier, self.router,
                                               self.human_queue, self.tracker)
        self.scheduler = FollowupScheduler(self.store, self.verifier,
                                            self.recovery, self.tracker)
        # Advanced Memory & Learning (Fase 6)
        lesson_extractor = LessonExtractor()
        memory_index = MemoryIndex()
        memory_decay = MemoryDecay()
        self.memory = MemoryService(self.store, lesson_extractor=lesson_extractor,
                                     memory_index=memory_index, memory_decay=memory_decay)
        # Multi-agent Orchestration (Fase 7)
        self.subtasks = SubTaskManager(self.store, self._create_task_run)
        self.agent_messaging = AgentMessaging(self.store)
        self.orchestrator = MultiAgentOrchestrator(self.subtasks, self.agent_messaging)
        self.coordinator = Coordinator(self.intermediary, self.verifier,
                                       self.router, self.coordinated,
                                       self.tracker)
        self.runs: dict[str, TaskRun] = {}

        # Production Hardening (Fase 8): Observability & Resilience
        # Validate configuration at startup (only if VEXINTER_ADMIN_TOKEN is defined, or not in non-strict test/dev mode)
        admin_token_env = os.environ.get("VEXINTER_ADMIN_TOKEN")
        dev_mode = os.environ.get("VEXINTER_DEV_MODE") == "1"
        if not admin_token_env:
            # The convenience default exists ONLY for tests and explicit
            # dev mode — never silently in production.
            if dev_mode or "pytest" in sys.modules:
                # Random per-process token: even if DEV_MODE leaks into a
                # shared environment, the admin credential is not a
                # publicly known constant from the source code.
                import secrets
                os.environ["VEXINTER_ADMIN_TOKEN"] = secrets.token_hex(24)
            else:
                raise RuntimeError(
                    "VEXINTER_ADMIN_TOKEN is not set. Configure it, or run "
                    "with VEXINTER_DEV_MODE=1 for local development only.")

        valid, errors = ConfigValidator.validate()
        if not valid and not dev_mode:
            raise RuntimeError(f"Invalid configuration: {errors}")

        # Configure structured logging
        log_level = os.environ.get("VEXINTER_LOG_LEVEL", "INFO")
        log_json = os.environ.get("VEXINTER_LOG_JSON", "1") in ("1", "true", "True")
        setup_structured_logging(log_level, log_json)

        # Health checker
        self.health_checker = HealthChecker(self.store, {
            "store": self.store,
            "tracker": self.tracker,
            "verifier": self.verifier,
            "intermediary": self.intermediary,
        })

        # Graceful shutdown
        self.shutdown = GracefulShutdown()
        self.shutdown.register(self._shutdown_handler)

        # Rate limiter - Distributed Redis support
        rate_limit_rps = float(os.environ.get("VEXINTER_RATE_LIMIT_RPS", "100"))
        rate_limit_burst = int(os.environ.get("VEXINTER_RATE_LIMIT_BURST", "20"))
        redis_url = os.environ.get("VEXINTER_REDIS_URL")
        redis_client = None
        if redis_url:
            try:
                import redis  # type: ignore[import-not-found]
                redis_client = redis.from_url(redis_url, decode_responses=False)
                # Check the connection
                redis_client.ping()
                logging.getLogger("vexinter.runtime").info(
                    "Redis connected for distributed rate limiting and circuit breaker",
                    extra={"extra_fields": {"redis_url": redis_url}}
                )
            except Exception as e:  # noqa: BLE001 - error boundary: converted to result/log
                logging.getLogger("vexinter.runtime").warning(
                    f"Could not connect to Redis ({redis_url}), using local mode: {e}"
                )
                redis_client = None
        
        self.rate_limiter = RateLimiter(
            rate_limit_rps, 
            rate_limit_burst,
            redis_client=redis_client
        )

        # Circuit breakers for external adapters - Redis support
        self.circuit_breakers: dict[str, CircuitBreaker] = {}
        self._redis_client = redis_client

        # Prometheus metrics
        self.metrics = prometheus_metrics

    def _shutdown_handler(self):
        """Graceful shutdown handler."""
        # Cancel in-flight tasks
        for run in self.runs.values():
            if not run.completed:
                run.completed = True
        # Close store connections if applicable
        if hasattr(self.store, "close"):
            self.store.close()

    def health_check(self) -> HealthStatus:
        """Component health check — delegates to the real probes
        (liveness/readiness/startup) instead of a hardcoded HEALTHY."""
        return self.health_checker.full_health().status

    def _create_task_run(self, task_id: str, execution_id: str, agent_id: str,
                          objective: str, condition: dict, input: dict,
                          instance_id: str | None = None,
                          credential_id: str | None = None) -> TaskRun:
        """Factory to create a TaskRun (used by SubTaskManager)."""
        from .taskrun import TaskRun
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