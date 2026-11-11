from __future__ import annotations

import logging
import os
import time
import uuid
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from functools import wraps
from typing import Any, ClassVar


def _package_version() -> str:
    """Installed distribution version (pyproject), never hardcoded."""
    try:
        from importlib.metadata import version
        return version("vexinter")
    except Exception:  # noqa: BLE001 - not installed as a dist: fall back
        return "unknown"

# Context variables for request tracing
correlation_id_var: ContextVar[str] = ContextVar("correlation_id", default="")
request_start_time_var: ContextVar[float] = ContextVar(
    "request_start_time", default=0.0
)


def _redis_available(redis_client: Any) -> bool:
    """Checks whether the Redis client is available and functional."""
    if redis_client is None:
        return False
    try:
        # Try ping to verify the connection
        redis_client.ping()
        return True
    except Exception:  # noqa: BLE001 - error boundary: converted to result/log
        return False


class HealthStatus(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


@dataclass
class HealthCheck:
    name: str
    status: HealthStatus
    message: str = ""
    latency_ms: float = 0.0
    details: dict = field(default_factory=dict)


@dataclass
class ServiceHealth:
    status: HealthStatus
    version: str
    timestamp: datetime
    checks: list[HealthCheck] = field(default_factory=list)
    uptime_seconds: float = 0.0


class StructuredFormatter(logging.Formatter):
    """JSON formatter with correlation ID support."""

    def format(self, record: logging.LogRecord) -> str:
        correlation_id = correlation_id_var.get()
        base = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "correlation_id": correlation_id,
        }
        if record.exc_info:
            base["exception"] = self.formatException(record.exc_info)
        # Extra fields passed via extra={"key": "value"} go directly into the record
        # We need to filter out standard logging attributes
        standard_attrs = {
            "name",
            "msg",
            "args",
            "levelname",
            "levelno",
            "pathname",
            "filename",
            "module",
            "lineno",
            "funcName",
            "created",
            "msecs",
            "relativeCreated",
            "thread",
            "threadName",
            "processName",
            "process",
            "message",
            "exc_info",
            "exc_text",
            "stack_info",
            "extra_fields",
        }
        for key, value in record.__dict__.items():
            if key not in standard_attrs:
                base[key] = value
        return json.dumps(base)


import json


def setup_structured_logging(level: str = "INFO", json_format: bool = True) -> None:
    """Configures structured logging for production."""
    log_level = getattr(logging, level.upper(), logging.INFO)

    handler = logging.StreamHandler()
    if json_format:
        handler.setFormatter(StructuredFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
        )

    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)
    root_logger.handlers = [handler]

    # Reduce library noise
    logging.getLogger("uvicorn").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def get_correlation_id() -> str:
    """Gets or generates a correlation ID for the current request."""
    cid = correlation_id_var.get()
    if not cid:
        cid = str(uuid.uuid4())[:8]
        correlation_id_var.set(cid)
    return cid


def set_correlation_id(cid: str) -> None:
    correlation_id_var.set(cid)


class RequestTracer:
    """Middleware for request tracing."""

    def __init__(self, app, excluded_paths: set[str] | None = None):
        self.app = app
        self.excluded_paths = excluded_paths or {
            "/health",
            "/ready",
            "/live",
            "/metrics",
        }

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if path in self.excluded_paths:
            await self.app(scope, receive, send)
            return

        correlation_id = scope.get("headers", {}).get(b"x-correlation-id", b"").decode()
        if not correlation_id:
            correlation_id = str(uuid.uuid4())[:8]

        set_correlation_id(correlation_id)
        request_start_time_var.set(time.time())

        logger = logging.getLogger("vexinter.request")
        logger.info(
            "Request started",
            extra={
                "extra_fields": {
                    "method": scope.get("method"),
                    "path": path,
                    "correlation_id": correlation_id,
                }
            },
        )

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                duration_ms = (time.time() - request_start_time_var.get()) * 1000
                logger.info(
                    "Request completed",
                    extra={
                        "extra_fields": {
                            "status_code": message.get("status"),
                            "duration_ms": round(duration_ms, 2),
                            "correlation_id": correlation_id,
                        }
                    },
                )
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception as e:
            duration_ms = (time.time() - request_start_time_var.get()) * 1000
            logger.error(
                "Request failed",
                extra={
                    "extra_fields": {
                        "error": str(e),
                        "duration_ms": round(duration_ms, 2),
                        "correlation_id": correlation_id,
                    }
                },
            )
            raise


class CircuitBreaker:
    """Circuit breaker for external calls, with optional distributed support.

    If a Redis client is provided, state information is synchronized
    across multiple instances. Otherwise it operates in local/singleton mode.
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        expected_exception: type[Exception] = Exception,
        redis_client: Any = None,
        service_name: str = "default",
    ):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.expected_exception = expected_exception
        self.failure_count = 0
        self.last_failure_time: float | None = None
        self.state = "closed"  # closed, open, half-open
        self.redis = redis_client
        self.service_name = service_name
        self._use_redis = _redis_available(redis_client)
        self._lock = {
            "lock": redis_client.lock(f"vexinter:cb:{service_name}:lock")
            if redis_client
            else None
        }

    def _get_state_key(self) -> tuple[str, str]:
        path = f"vexinter:cb:{self.service_name}"
        return f"{path}:state", f"{path}:stats"

    def _sync_state_to_redis(
        self, state: str, failure_count: int, last_failure: float | None
    ):
        """Synchronizes the current circuit breaker state with Redis."""
        if not self._use_redis or not self.redis:
            return

        try:
            state_key, _ = self._get_state_key()
            self.redis.hset(state_key, "state", state)
            self.redis.hset(state_key, "failure_count", str(failure_count))
            if last_failure is not None:
                self.redis.hset(state_key, "last_failure", str(last_failure))
            self.redis.expire(state_key, int(self.recovery_timeout) + 60)
        except Exception as e:  # noqa: BLE001 - error boundary: converted to result/log
            logging.getLogger(__name__).debug(
                "could not sync circuit breaker state with Redis: %s", e
            )

    def _load_state_from_redis(self):
        """Loads the circuit breaker state from Redis."""
        if not self._use_redis or not self.redis:
            return

        try:
            state_key, _ = self._get_state_key()
            data = self.redis.hgetall(state_key)
            if data:
                state = data.get(b"state", b"closed").decode()
                failure_count = int(data.get(b"failure_count", b"0"))
                last_failure = data.get(b"last_failure")
                if last_failure:
                    last_failure = float(last_failure.decode())

                self.state = state
                self.failure_count = failure_count
                self.last_failure_time = last_failure

                if self.state == "open":
                    now = time.time()
                    if last_failure and now - last_failure >= self.recovery_timeout:
                        self.state = "half-open"
        except Exception as e:  # noqa: BLE001 - error boundary: converted to result/log
            logging.getLogger(__name__).debug(
                "could not load circuit breaker state from Redis: %s", e
            )

    def allow(self, client_id: str, tokens: int = 1) -> tuple[bool, dict]:
        """Check rate limiter status."""
        if self.state == "open":
            last_fail = self.last_failure_time or 0.0
            if time.time() - last_fail >= self.recovery_timeout:
                self.state = "half-open"
            else:
                return False, {"status": "blocked", "reason": "circuit_breaker_open"}
        return True, {"status": "allowed"}

    def _state_lock(self):
        """Redis lock serializing state read-modify-write across
        instances; nullcontext when Redis is not in use."""
        from contextlib import nullcontext
        lock = self._lock.get("lock")
        return lock if lock is not None else nullcontext()

    def _check_open(self):
        if self.state == "open":
            last_fail = self.last_failure_time or 0.0
            if time.time() - last_fail >= self.recovery_timeout:
                self.state = "half-open"
            else:
                raise CircuitBreakerOpenError(
                    f"Circuit breaker for {self.service_name} is open"
                )

    def call(self, func: Callable, *args, **kwargs):
        # Load + check under the distributed lock: two instances must not
        # both read "closed" and double-count failures.
        if self._use_redis:
            with self._state_lock():
                self._load_state_from_redis()
                self._check_open()
        else:
            self._check_open()

        try:
            result = func(*args, **kwargs)
        except self.expected_exception:
            self.on_failure()
            raise
        self.on_success()
        return result

    def on_success(self):
        with self._state_lock():
            if self._use_redis:
                self._load_state_from_redis()
            self.failure_count = 0
            self.state = "closed"
            self._sync_state_to_redis(self.state, self.failure_count, None)

    def on_failure(self):
        with self._state_lock():
            if self._use_redis:
                self._load_state_from_redis()
            self.failure_count += 1
            self.last_failure_time = time.time()

            if self.failure_count >= self.failure_threshold:
                self.state = "open"
                self._sync_state_to_redis(
                    self.state, self.failure_count, self.last_failure_time
                )


class CircuitBreakerOpenError(Exception):
    pass


class RateLimiter:
    """Rate limiter per client (token bucket).

    Automatically selects Redis if configured, or falls back to in-memory.
    """

    def __init__(
        self,
        rate: float = 100,
        burst: int = 20,
        redis_client: Any = None,
        redis_prefix: str = "vexinter:rate_limit:",
    ):
        self.rate = rate  # requests per second
        self.burst = burst
        self.redis = redis_client
        self.redis_prefix = redis_prefix

        # In-memory fallback when there is no Redis (bounded: an
        # attacker spraying client_ids cannot grow it without limit)
        self._memory_bucket: dict[str, tuple[float, float]] = {}
        self._bucket_cap = 50_000
        self._use_memory = redis_client is None

    def allow(self, client_id: str, tokens: int = 1) -> tuple[bool, dict]:
        now = time.time()

        if self._use_memory:
            # In-memory fallback (single instance mode)
            bucket_tokens, last_update = self._memory_bucket.get(
                client_id, (float(self.burst), now)
            )

            # Replenish tokens
            elapsed = now - last_update
            bucket_tokens = min(self.burst, bucket_tokens + elapsed * self.rate)

            if bucket_tokens >= tokens:
                bucket_tokens -= tokens
                self._memory_bucket[client_id] = (bucket_tokens, now)
                return True, {
                    "remaining": int(max(0.0, bucket_tokens)),
                    "retry_after": 0,
                }
            else:
                if len(self._memory_bucket) >= self._bucket_cap:
                    # Evict the oldest-updated entries (dict order is
                    # insertion order — good enough for a cap).
                    for k in list(self._memory_bucket)[:self._bucket_cap // 4]:
                        del self._memory_bucket[k]
                self._memory_bucket[client_id] = (bucket_tokens, now)
                retry_after = max(0, (tokens - bucket_tokens) / self.rate)
                return False, {"remaining": 0, "retry_after": retry_after}
        else:
            # Distributed mode with Redis
            if not self.redis or not _redis_available(self.redis):
                # In-memory fallback if Redis is not available
                return self._fallback_allow_memory(client_id, tokens)

            key = f"{self.redis_prefix}{client_id}"

            # Use a Redis Lua script to guarantee atomicity
            lua_script = """
            local key = KEYS[1]
            local burst = tonumber(ARGV[1])
            local rate = tonumber(ARGV[2])
            local requested = tonumber(ARGV[3])
            local now = tonumber(ARGV[4])
            
            "gets or initializes the bucket"
            local tokens = tonumber(redis.call('HGET', key, 'tokens')) or burst
            local last_update = tonumber(redis.call('HGET', key, 'last_update')) or now
            
            -- Recalculate available tokens since the last update
            if tokens < burst then
                local elapsed = now - last_update
                tokens = math.min(burst, tokens + elapsed * rate)
            end
            
            -- Check whether there are enough tokens
            if tokens >= requested then
                tokens = tokens - requested
                
                -- Update in Redis
                redis.call('HSET', key, 'tokens', tokens, 'last_update', now)
                redis.call('EXPIRE', key, 3600)
                
                return 1 -- allow
            else
                -- Not enough tokens, we calculate retry_after
                local wait_time = (requested - tokens) / rate
                local hint = redis.call('SET', key .. ':hint', now + wait_time, 'EX', 300)
                
                return 0 -- deny
            end
            """

            try:
                result = self.redis.eval(
                    lua_script, 1, key, self.burst, self.rate, tokens, now
                )

                remaining_burst = int(result)
                retry_after = 0
                if result != 1:
                    # The Lua script stored now+wait_time in <key>:hint —
                    # use it so clients get the real wait, not a constant.
                    try:
                        hint = self.redis.get(f"{key}:hint")
                        retry_after = max(
                            0, int(float(hint) - now)) if hint else 0
                    except Exception:  # noqa: BLE001 - hint is advisory
                        retry_after = 0

                return result == 1, {
                    "remaining": remaining_burst,
                    "retry_after": retry_after,
                    "backend": "redis",
                }
            except Exception:  # noqa: BLE001 - error boundary: converted to result/log
                # Fall back to memory on Redis error
                return self._fallback_allow_memory(client_id, tokens)

    def _fallback_allow_memory(self, client_id: str, tokens: int) -> tuple[bool, dict]:
        """Private method for in-memory fallback in distributed mode."""
        now = time.time()
        bucket_tokens, last_update = self._memory_bucket.get(
            client_id, (float(self.burst), now)
        )

        elapsed = now - last_update
        bucket_tokens = min(self.burst, bucket_tokens + elapsed * self.rate)

        if bucket_tokens >= tokens:
            bucket_tokens -= tokens
            self._memory_bucket[client_id] = (bucket_tokens, now)
            return True, {"remaining": int(max(0.0, bucket_tokens)), "retry_after": 0}
        else:
            self._memory_bucket[client_id] = (bucket_tokens, now)
            retry_after = max(0, (tokens - bucket_tokens) / self.rate)
            return False, {"remaining": 0, "retry_after": retry_after}


class ConfigValidator:
    """Configuration validator at startup."""

    REQUIRED_ENV_VARS: ClassVar[list[str]] = [
        "VEXINTER_ADMIN_TOKEN",
    ]

    OPTIONAL_ENV_VARS: ClassVar[dict] = {
        "VEXINTER_PG_DSN": str,
        "VEXINTER_ADAPTERS": str,
        "VEXINTER_DEV_MODE": lambda x: x in ("1", "true", "True"),
        "VEXINTER_AUTO_MIGRATE": lambda x: x in ("1", "true", "True"),
        "VEXINTER_METRICS_TOKEN": str,
        "VEXINTER_LOG_LEVEL": str,
        "VEXINTER_LOG_JSON": lambda x: x in ("1", "true", "True"),
        "VEXINTER_RATE_LIMIT_RPS": float,
        "VEXINTER_RATE_LIMIT_BURST": int,
        "VEXINTER_REDIS_URL": str,
    }

    @classmethod
    def validate(cls) -> tuple[bool, list[str]]:
        """Validates configuration. Returns (is_valid, list_of_errors)."""
        errors = []

        for var in cls.REQUIRED_ENV_VARS:
            if not os.environ.get(var):
                errors.append(f"Required environment variable not defined: {var}")

        token = os.environ.get("VEXINTER_ADMIN_TOKEN")
        if token is not None and len(token) < 32:
            errors.append(
                "VEXINTER_ADMIN_TOKEN must be at least 32 characters")

        for var, parser in cls.OPTIONAL_ENV_VARS.items():
            value = os.environ.get(var)
            if value is not None:
                try:
                    parser(value)
                except Exception as e:  # noqa: BLE001 - error boundary: converted to result/log
                    errors.append(f"Invalid variable {var}: {e}")

        return len(errors) == 0, errors

    @classmethod
    def get_config(cls) -> dict:
        """Gets the parsed configuration."""
        config = {}
        for var, parser in cls.OPTIONAL_ENV_VARS.items():
            value = os.environ.get(var)
            if value is not None:
                try:
                    config[var] = parser(value)
                except Exception:  # noqa: BLE001 - error boundary: converted to result/log
                    config[var] = value
        return config


class GracefulShutdown:
    """Graceful shutdown handler."""

    def __init__(self):
        self.shutdown_handlers: list[Callable] = []
        self.is_shutting_down = False

    def register(self, handler: Callable) -> None:
        self.shutdown_handlers.append(handler)

    async def shutdown(self) -> None:
        self.is_shutting_down = True
        logger = logging.getLogger("vexinter.shutdown")
        logger.info("Starting graceful shutdown...")

        for handler in self.shutdown_handlers:
            try:
                if callable(handler):
                    result = handler()
                    if hasattr(result, "__await__"):
                        await result
            except Exception as e:  # noqa: BLE001 - error boundary: converted to result/log
                logger.error(f"Error in shutdown handler: {e}")

        logger.info("Graceful shutdown completed")


class HealthChecker:
    """Health checks for Kubernetes/load balancer."""

    def __init__(self, store, runtime_components: dict):
        self.store = store
        self.components = runtime_components
        self.start_time = time.time()

    def check_liveness(self) -> HealthCheck:
        """Liveness probe - process is alive."""
        return HealthCheck(
            name="liveness",
            status=HealthStatus.HEALTHY,
            message="Process is alive",
            latency_ms=0.1,
        )

    def check_readiness(self) -> HealthCheck:
        """Readiness probe - ready to receive traffic."""
        try:
            # Check the store
            self.store.query(None)
            # Check critical components
            for name, component in self.components.items():
                if hasattr(component, "health_check"):
                    hc = component.health_check()
                    if hc.status != HealthStatus.HEALTHY:
                        return HealthCheck(
                            name="readiness",
                            status=HealthStatus.DEGRADED,
                            message=f"Component {name} degraded: {hc.message}",
                            latency_ms=1.0,
                        )
            return HealthCheck(
                name="readiness",
                status=HealthStatus.HEALTHY,
                message="All components ready",
                latency_ms=1.0,
            )
        except Exception as e:  # noqa: BLE001 - error boundary: converted to result/log
            return HealthCheck(
                name="readiness",
                status=HealthStatus.UNHEALTHY,
                message=f"Readiness check failed: {e}",
                latency_ms=1.0,
            )

    def check_startup(self) -> HealthCheck:
        """Startup probe - initialization completed."""
        return HealthCheck(
            name="startup",
            status=HealthStatus.HEALTHY,
            message="Startup completed",
            latency_ms=0.1,
        )

    def full_health(self) -> ServiceHealth:
        """Full health check."""
        checks = [
            self.check_liveness(),
            self.check_readiness(),
            self.check_startup(),
        ]

        overall = HealthStatus.HEALTHY
        for check in checks:
            if check.status == HealthStatus.UNHEALTHY:
                overall = HealthStatus.UNHEALTHY
                break
            elif check.status == HealthStatus.DEGRADED:
                overall = HealthStatus.DEGRADED

        return ServiceHealth(
            status=overall,
            version=_package_version(),
            timestamp=datetime.now(UTC),
            checks=checks,
            uptime_seconds=time.time() - self.start_time,
        )

    def uptime_seconds(self) -> float:
        return time.time() - self.start_time


def log_audit_event(
    event_type: str,
    actor: str,
    resource: str,
    action: str,
    outcome: str,
    details: dict | None = None,
    correlation_id: str | None = None,
) -> None:
    """Structured audit log."""
    logger = logging.getLogger("vexinter.audit")
    logger.info(
        "Audit event",
        extra={
            "event_type": event_type,
            "actor": actor,
            "resource": resource,
            "action": action,
            "outcome": outcome,
            "details": details or {},
            "correlation_id": correlation_id or get_correlation_id(),
        },
    )


# Decorator for retry functions
def with_retry(
    max_attempts: int = 3,
    base_delay: float = 0.1,
    max_delay: float = 2.0,
    exponential_base: float = 2.0,
    exceptions: tuple = (Exception,),
):
    def decorator(func: Callable):
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_exception = None
            for attempt in range(max_attempts):
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    last_exception = e
                    if attempt < max_attempts - 1:
                        delay = min(base_delay * (exponential_base**attempt), max_delay)
                        time.sleep(delay)
            raise last_exception

        return wrapper

    return decorator


# Prometheus-style metrics
class PrometheusMetrics:
    """Metrics exporter in Prometheus format."""

    def __init__(self):
        self.counters: dict[str, float] = {}
        self.gauges: dict[str, float] = {}
        self.histograms: dict[str, list[float]] = {}

    def inc_counter(self, name: str, value: float = 1.0, labels: dict | None = None):
        key = self._make_key(name, labels)
        self.counters[key] = self.counters.get(key, 0) + value

    def set_gauge(self, name: str, value: float, labels: dict | None = None):
        key = self._make_key(name, labels)
        self.gauges[key] = value

    def observe_histogram(self, name: str, value: float, labels: dict | None = None):
        key = self._make_key(name, labels)
        if key not in self.histograms:
            self.histograms[key] = []
        self.histograms[key].append(value)

    def _make_key(self, name: str, labels: dict | None = None) -> str:
        if not labels:
            return name
        label_str = ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
        return f"{name}{{{label_str}}}"

    def generate(self) -> str:
        lines = []
        for name, value in self.counters.items():
            lines.append(f"# TYPE {name} counter")
            lines.append(f"{name} {value}")
        for name, value in self.gauges.items():
            lines.append(f"# TYPE {name} gauge")
            lines.append(f"{name} {value}")
        for name, values in self.histograms.items():
            if values:
                lines.append(f"# TYPE {name} histogram")
                count = len(values)
                total = sum(values)
                lines.append(f"{name}_count {count}")
                lines.append(f"{name}_sum {total}")
                # Approximate quantiles
                sorted_vals = sorted(values)
                lines.append(
                    f'{name}_bucket{{le="0.5"}} {sum(1 for v in sorted_vals if v <= 0.5)}'
                )
                lines.append(
                    f'{name}_bucket{{le="1.0"}} {sum(1 for v in sorted_vals if v <= 1.0)}'
                )
                lines.append(
                    f'{name}_bucket{{le="2.0"}} {sum(1 for v in sorted_vals if v <= 2.0)}'
                )
                lines.append(
                    f'{name}_bucket{{le="5.0"}} {sum(1 for v in sorted_vals if v <= 5.0)}'
                )
                lines.append(f'{name}_bucket{{le="+Inf"}} {count}')
        return "\n".join(lines) + "\n"


# Global metrics instance
prometheus_metrics = PrometheusMetrics()
