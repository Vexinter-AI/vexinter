"""pytest configuration: ephemeral PostgreSQL for integration tests."""
import importlib
import logging
import os

_container = None



_log = logging.getLogger(__name__)

# TestClient runs over http://testserver: without TLS Secure cookies are
# not sent back, so the suite uses the explicit development override.
os.environ.setdefault("VEXINTER_COOKIE_SECURE", "0")

def _postgres_class():
    """PostgreSQL container class from testcontainers (new path first)."""
    for module, name in (
            ("testcontainers.community.postgres", "PostgresContainer"),
            ("testcontainers.community.postgres", "PostgresCommunityContainer"),
            ("testcontainers.postgres", "PostgresContainer")):
        try:
            return getattr(importlib.import_module(module), name)
        except (ImportError, AttributeError):
            continue
    return None


def pytest_sessionstart(session):
    """If a PostgreSQL is already configured (VEXINTER_TEST_PG_DSN: the CI
    service or your own container), that one is used. Otherwise an ephemeral
    one is started; without Docker, PostgreSQL tests skip on their own."""
    global _container
    if os.environ.get("VEXINTER_TEST_PG_DSN"):
        return
    cls = _postgres_class()
    if cls is None:
        print("\n(testcontainers is not installed: PostgreSQL tests "
              "will be skipped)")
        return
    try:
        _container = cls("postgres:16-alpine")
        _container.start()
        dsn = _container.get_connection_url()
        for prefix in ("postgresql+psycopg2://", "postgresql+psycopg://",
                       "postgres://"):
            if dsn.startswith(prefix):
                dsn = "postgresql://" + dsn[len(prefix):]
                break
        os.environ["VEXINTER_TEST_PG_DSN"] = dsn
        print("\nEphemeral PostgreSQL (testcontainers) started")
    except Exception as e:
        _container = None
        print(f"\nCould not start testcontainers ({type(e).__name__}): "
              "PostgreSQL tests will be skipped. Is Docker running?")


def pytest_sessionfinish(session, exitstatus):
    global _container
    if _container is not None:
        try:
            _container.stop()
        except Exception as e:
            _log.debug("could not stop the test container: %s", e)
        _container = None
