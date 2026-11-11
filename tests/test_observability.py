"""Tests for Phase 8 (Production Hardening / Observability)."""
import json
import os
import time
from unittest.mock import Mock, patch

from vexinter.core.observability import (
    CircuitBreaker,
    CircuitBreakerOpenError,
    ConfigValidator,
    GracefulShutdown,
    HealthChecker,
    HealthStatus,
    PrometheusMetrics,
    RateLimiter,
    StructuredFormatter,
    get_correlation_id,
    log_audit_event,
    set_correlation_id,
)
from vexinter.core.runtime import LocalRuntime
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


def test_config_validator():
    """Tests the config validator."""
    # Test con variables requeridas
    with patch.dict(os.environ, {"VEXINTER_ADMIN_TOKEN": "test-token-12345-67890abcdef0123456789"}):
        valid, errors = ConfigValidator.validate()
        assert valid
        assert len(errors) == 0
    
    # Test sin variables requeridas
    with patch.dict(os.environ, {}, clear=True):
        valid, errors = ConfigValidator.validate()
        assert not valid
        assert any("VEXINTER_ADMIN_TOKEN" in e for e in errors)
    
    # Test get_config
    with patch.dict(os.environ, {"VEXINTER_LOG_LEVEL": "DEBUG", "VEXINTER_RATE_LIMIT_RPS": "50"}):
        config = ConfigValidator.get_config()
        assert config.get("VEXINTER_LOG_LEVEL") == "DEBUG"
        assert config.get("VEXINTER_RATE_LIMIT_RPS") == 50.0
    
    print("ConfigValidator test passed!")


def test_graceful_shutdown():
    """Tests graceful shutdown."""
    shutdown = GracefulShutdown()
    
    called = []
    def handler():
        called.append("sync")
    
    async def async_handler():
        called.append("async")
    
    shutdown.register(handler)
    shutdown.register(async_handler)
    
    import asyncio
    asyncio.run(shutdown.shutdown())
    
    assert "sync" in called
    assert "async" in called
    assert shutdown.is_shutting_down
    
    print("GracefulShutdown test passed!")


def test_health_checker():
    """Tests the health checker."""
    store = InMemoryEventStore()
    
    # Mock components with health_check method
    mock_tracker = Mock()
    mock_tracker.health_check.return_value = Mock(status=HealthStatus.HEALTHY, message="OK")
    mock_verifier = Mock()
    mock_verifier.health_check.return_value = Mock(status=HealthStatus.HEALTHY, message="OK")
    mock_intermediary = Mock()
    mock_intermediary.health_check.return_value = Mock(status=HealthStatus.HEALTHY, message="OK")
    
    checker = HealthChecker(store, {
        "store": store,
        "tracker": mock_tracker,
        "verifier": mock_verifier,
        "intermediary": mock_intermediary,
    })
    
    # Liveness
    liveness = checker.check_liveness()
    assert liveness.status == HealthStatus.HEALTHY
    assert liveness.name == "liveness"
    
    # Startup
    startup = checker.check_startup()
    assert startup.status == HealthStatus.HEALTHY
    assert startup.name == "startup"
    
    # Readiness
    readiness = checker.check_readiness()
    assert readiness.status == HealthStatus.HEALTHY
    assert readiness.name == "readiness"
    
    # Full health
    full = checker.full_health()
    assert full.status == HealthStatus.HEALTHY
    assert len(full.checks) == 3
    assert full.uptime_seconds >= 0
    
    # Uptime method
    uptime = checker.uptime_seconds()
    assert uptime >= 0
    
    print("HealthChecker test passed!")


def test_prometheus_metrics():
    """Tests Prometheus metrics."""
    metrics = PrometheusMetrics()
    
    # Counter - without labels
    metrics.inc_counter("test_counter", 1)
    metrics.inc_counter("test_counter", 2)
    assert metrics.counters["test_counter"] == 3
    
    # Counter with labels - separate key
    metrics.inc_counter("test_counter", 2, {"label": "value"})
    assert metrics.counters['test_counter{label="value"}'] == 2
    
    # Gauge
    metrics.set_gauge("test_gauge", 42)
    metrics.set_gauge("test_gauge", 100, {"label": "value"})
    assert metrics.gauges["test_gauge"] == 42
    assert metrics.gauges['test_gauge{label="value"}'] == 100
    
    # Histogram
    metrics.observe_histogram("test_histogram", 0.5)
    metrics.observe_histogram("test_histogram", 1.5)
    metrics.observe_histogram("test_histogram", 0.2, {"label": "value"})
    assert len(metrics.histograms["test_histogram"]) == 2
    assert len(metrics.histograms['test_histogram{label="value"}']) == 1
    
    # Generate output
    output = metrics.generate()
    assert "test_counter" in output
    assert "test_gauge" in output
    assert "test_histogram" in output
    
    print("PrometheusMetrics test passed!")


def test_rate_limiter():
    """Tests the rate limiter."""
    limiter = RateLimiter(rate=10, burst=5)  # 10 req/s, burst 5
    
    # Primeras 5 requests permitidas (burst)
    for i in range(5):
        allowed, info = limiter.allow("client1")
        assert allowed
        assert info["remaining"] == 4 - i
    
    # 6ta request denegada
    allowed, info = limiter.allow("client1")
    assert not allowed
    assert info["remaining"] == 0
    assert info["retry_after"] > 0
    
    # Cliente diferente tiene su propio bucket
    allowed, info = limiter.allow("client2")
    assert allowed
    assert info["remaining"] == 4
    
    print("RateLimiter test passed!")


def test_circuit_breaker():
    """Tests the circuit breaker."""
    cb = CircuitBreaker(failure_threshold=3, recovery_timeout=0.1)
    
    # A function that fails
    def failing_func():
        raise ValueError("fail")
    
    # Primeros 2 fallos - circuit cerrado
    for _ in range(2):
        try:
            cb.call(failing_func)
        except ValueError:
            pass
    assert cb.state == "closed"
    assert cb.failure_count == 2
    
    # 3er fallo - circuit abierto
    try:
        cb.call(failing_func)
    except ValueError:
        pass
    assert cb.state == "open"
    
    # Llamadas siguientes rechazadas
    try:
        cb.call(failing_func)
        assert False, "Should raise CircuitBreakerOpenError"
    except CircuitBreakerOpenError:
        pass
    
    # Esperar recovery timeout
    time.sleep(0.15)
    
    # Ahora en half-open, si falla vuelve a open
    try:
        cb.call(failing_func)
    except ValueError:
        pass
    assert cb.state == "open"
    
    print("CircuitBreaker test passed!")


def test_correlation_id():
    """Tests correlation ID."""
    # Limpiar
    set_correlation_id("")
    
    # Generar nuevo
    cid1 = get_correlation_id()
    assert cid1
    assert len(cid1) == 8
    
    # El mismo en contexto
    cid2 = get_correlation_id()
    assert cid1 == cid2
    
    # Set manual
    set_correlation_id("custom-123")
    assert get_correlation_id() == "custom-123"
    
    print("Correlation ID test passed!")


def test_structured_logging():
    """Tests structured logging."""
    import io
    import logging
    
    # Capturar logs
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(StructuredFormatter())
    
    logger = logging.getLogger("test_structured")
    logger.setLevel(logging.INFO)
    logger.handlers = [handler]
    logger.propagate = False
    
    set_correlation_id("test-correlation")
    logger.info("Test message", extra={"key": "value"})
    
    output = stream.getvalue()
    log_entry = json.loads(output.strip())
    
    assert log_entry["message"] == "Test message"
    assert log_entry["correlation_id"] == "test-correlation"
    assert log_entry["key"] == "value"
    assert log_entry["level"] == "INFO"
    
    print("StructuredLogging test passed!")


def test_log_audit_event():
    """Tests audit logging."""
    import io
    import logging
    
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(StructuredFormatter())
    
    logger = logging.getLogger("vexinter.audit")
    logger.setLevel(logging.INFO)
    logger.handlers = [handler]
    logger.propagate = False
    
    set_correlation_id("audit-test")
    log_audit_event(
        event_type="test_event",
        actor="test_actor",
        resource="test_resource",
        action="test_action",
        outcome="success",
        details={"detail": "value"}
    )
    
    output = stream.getvalue()
    log_entry = json.loads(output.strip())
    
    assert log_entry["event_type"] == "test_event"
    assert log_entry["actor"] == "test_actor"
    assert log_entry["resource"] == "test_resource"
    assert log_entry["action"] == "test_action"
    assert log_entry["outcome"] == "success"
    assert log_entry["details"]["detail"] == "value"
    assert log_entry["correlation_id"] == "audit-test"
    
    print("LogAuditEvent test passed!")


def test_runtime_observability_integration():
    """Tests observability integration in LocalRuntime."""
    # Configurar env vars requeridas
    os.environ["VEXINTER_ADMIN_TOKEN"] = "test-admin-token-12345"
    os.environ["VEXINTER_DEV_MODE"] = "1"
    
    adapter = DummyAdapter("sys")
    rt = LocalRuntime(adapters=[adapter], authenticated=False)
    
    # Verificar componentes de observabilidad
    assert hasattr(rt, "health_checker")
    assert hasattr(rt, "shutdown")
    assert hasattr(rt, "rate_limiter")
    assert hasattr(rt, "metrics")
    assert hasattr(rt, "circuit_breakers")
    
    # Health checks
    liveness = rt.health_checker.check_liveness()
    assert liveness.status == HealthStatus.HEALTHY
    
    readiness = rt.health_checker.check_readiness()
    assert readiness.status == HealthStatus.HEALTHY
    
    full = rt.health_checker.full_health()
    assert full.status == HealthStatus.HEALTHY
    
    # Rate limiter
    allowed, _info = rt.rate_limiter.allow("test-client")
    assert allowed
    
    # Metrics
    rt.metrics.inc_counter("test_metric")
    output = rt.metrics.generate()
    assert "test_metric" in output
    
    # Correlation ID
    cid = get_correlation_id()
    assert cid
    
    print("Runtime Observability Integration test passed!")


if __name__ == "__main__":
    test_config_validator()
    test_graceful_shutdown()
    test_health_checker()
    test_prometheus_metrics()
    test_rate_limiter()
    test_circuit_breaker()
    test_correlation_id()
    test_structured_logging()
    test_log_audit_event()
    test_runtime_observability_integration()
    print("\nAll Production Hardening tests passed successfully!")