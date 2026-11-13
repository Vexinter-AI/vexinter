"""Append-only store over PostgreSQL (§12.2) and sequence guard.

Same interface as InMemoryEventStore: the acceptance criterion is that
the core tests pass against either one unchanged.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from .core.crypto import GENESIS, canonical_json, hash_payload, sha256_hex
from .core.events import Event, EventDraft, EventType, new_id, now
from .core.store import ChainAudit, CheckpointSigner, EventQuery

_COLS = ("id, stream, seq, ts, type, task_id, execution_id, agent_id, "
         "operation_id, caused_by, depends_on, compensates, payload, "
         "payload_ref, payload_hash, prev_hash, this_hash, signature")

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "sql" / "schema.sql"
MIGRATIONS_DIR = SCHEMA_PATH.parent / "migrations"

# Version of sql/schema.sql applied by init_schema. Bump it whenever the
# schema changes in a way old code cannot tolerate; init_schema refuses to
# run older code against a newer database.
SCHEMA_VERSION = 5


def _row_to_event(row) -> Event:
    return Event(id=row[0], stream=row[1], seq=row[2], ts=row[3],
                 type=EventType(row[4]), task_id=row[5], execution_id=row[6],
                 agent_id=row[7], operation_id=row[8], caused_by=row[9],
                 depends_on=row[10] or [], compensates=row[11],
                 payload=row[12], payload_ref=row[13], payload_hash=row[14],
                 prev_hash=row[15], this_hash=row[16], signature=row[17])


class PostgresEventStore:
    """The store as the single persistent source of truth (§15)."""

    def __init__(self, dsn: str, min_size: int = 1, max_size: int = 8):
        from psycopg_pool import ConnectionPool
        self._pool = ConnectionPool(dsn, min_size=min_size, max_size=max_size,
                                   open=True)

    def close(self) -> None:
        self._pool.close()

    @staticmethod
    def _lock_stream(conn, stream: str) -> tuple[int, str]:
        conn.execute(
            "INSERT INTO vex_stream_head (stream, seq, head_hash) "
            "VALUES (%s, 0, %s) ON CONFLICT (stream) DO NOTHING",
            (stream, GENESIS))
        row = conn.execute(
            "SELECT seq, head_hash FROM vex_stream_head "
            "WHERE stream = %s FOR UPDATE", (stream,)).fetchone()
        return (row[0], row[1]) if row else (0, GENESIS)

    @staticmethod
    def _insert_event(conn, draft: EventDraft, stream: str,
                      seq: int, prev: str) -> Event:
        from psycopg.types.json import Jsonb
        ev = Event(id=new_id("evt"), stream=stream, seq=seq + 1,
                   payload_hash=hash_payload(draft.payload),
                   prev_hash=prev, this_hash="",
                   **draft.model_dump()).seal()
        conn.execute(
            f"INSERT INTO vex_event ({_COLS}) VALUES "
            "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (ev.id, ev.stream, ev.seq, ev.ts, ev.type.value, ev.task_id,
             ev.execution_id, ev.agent_id, ev.operation_id, ev.caused_by,
             Jsonb(ev.depends_on), ev.compensates,
             Jsonb(ev.payload) if ev.payload is not None else None,
             ev.payload_ref, ev.payload_hash, ev.prev_hash, ev.this_hash,
             ev.signature))
        conn.execute("UPDATE vex_stream_head SET seq = %s, head_hash = %s "
                     "WHERE stream = %s", (ev.seq, ev.this_hash, stream))
        return ev

    def append(self, draft: EventDraft, stream: str) -> Event:
        with self._pool.connection() as conn, conn.transaction():
            seq, prev = self._lock_stream(conn, stream)
            return self._insert_event(conn, draft, stream, seq, prev)

    def append_state_transition(self, draft: EventDraft, stream: str,
                                execution_id: str, target) -> Event:
        """Atomically validate-and-append EXECUTION_STATE_CHANGED.

        The stream head row is locked FOR UPDATE inside the same
        transaction that reads the execution's events and derives the
        current state — check-then-act cannot interleave with another
        writer to this stream (all state events of an execution share
        run.stream)."""
        from vexinter.core.statemachine import (
            VALID,
            ExecutionState,
            StateTransitionError,
            derive_execution_state,
        )
        tgt = target if isinstance(target, ExecutionState) \
            else ExecutionState(target)
        with self._pool.connection() as conn, conn.transaction():
            seq, prev = self._lock_stream(conn, stream)
            # Serialize validators per EXECUTION, not just per stream:
            # multi-agent flows can append state events for the same
            # execution on different streams. Transaction-scoped
            # advisory lock releases on commit/rollback.
            # Lock ordering is always stream-head THEN advisory — every
            # caller acquires in this order, so no deadlock cycle exists.
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (execution_id,))
            rows = conn.execute(
                f"SELECT {_COLS} FROM vex_event WHERE execution_id = %s "
                "ORDER BY ts, seq", (execution_id,)).fetchall()
            current = derive_execution_state(
                [_row_to_event(r) for r in rows])
            if tgt not in VALID[current]:
                raise StateTransitionError(current, tgt)
            draft.payload = {**(draft.payload or {}),
                             "from": current.value, "to": tgt.value}
            return self._insert_event(conn, draft, stream, seq, prev)

    def get(self, event_id: str) -> Event | None:
        with self._pool.connection() as conn:
            row = conn.execute(f"SELECT {_COLS} FROM vex_event WHERE id = %s",
                               (event_id,)).fetchone()
        return _row_to_event(row) if row else None

    def head(self, stream: str) -> tuple[int, str]:
        with self._pool.connection() as conn:
            row = conn.execute("SELECT seq, head_hash FROM vex_stream_head "
                               "WHERE stream = %s", (stream,)).fetchone()
        return (row[0], row[1]) if row else (0, GENESIS)

    def query(self, q: EventQuery | None = None) -> list[Event]:
        q = q or EventQuery()
        sql = f"SELECT {_COLS} FROM vex_event WHERE 1=1"
        args: list[Any] = []
        if q.task_id:      sql += " AND task_id = %s";       args.append(q.task_id)
        if q.execution_id: sql += " AND execution_id = %s";  args.append(q.execution_id)
        if q.agent_id:     sql += " AND agent_id = %s";      args.append(q.agent_id)
        if q.types:        sql += " AND type = ANY(%s)";      args.append([t.value for t in q.types])
        if q.since:        sql += " AND ts >= %s";           args.append(q.since)
        if q.until:        sql += " AND ts <= %s";           args.append(q.until)
        if q.payload_eq:
            for k, v in q.payload_eq.items():
                # payload->> yields JSON *text*: booleans come out as
                # "true"/"false" (lowercase) — str(True)="True" would
                # never match. None can't be expressed via ->> (missing
                # key and JSON null are indistinguishable), so it maps
                # to IS NULL which covers both.
                if v is None:
                    sql += " AND payload->>%s IS NULL"
                    args.append(k)
                elif isinstance(v, bool):
                    sql += " AND payload->>%s = %s"
                    args.extend([k, "true" if v else "false"])
                else:
                    sql += " AND payload->>%s = %s"
                    args.extend([k, str(v)])
        sql += " ORDER BY ts, seq LIMIT %s";                 args.append(q.limit)
        with self._pool.connection() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [_row_to_event(r) for r in rows]

    def verify_chain(self, stream: str | None = None) -> ChainAudit:
        with self._pool.connection() as conn:
            streams = ([stream] if stream else
                       [r[0] for r in conn.execute(
                           "SELECT DISTINCT stream FROM vex_event").fetchall()])
            problems, checked = [], 0
            for s in streams:
                prev = GENESIS
                for row in conn.execute(
                        f"SELECT {_COLS} FROM vex_event "
                        "WHERE stream = %s ORDER BY seq", (s,)).fetchall():
                    ev = _row_to_event(row)
                    checked += 1
                    if ev.prev_hash != prev:
                        problems.append(f"{ev.id}: prev_hash does not link")
                    if ev.payload_hash != hash_payload(ev.payload):
                        problems.append(f"{ev.id}: altered content")
                    if ev.this_hash != sha256_hex(canonical_json(ev.hash_material())):
                        problems.append(f"{ev.id}: invalid this_hash")
                    prev = ev.this_hash
        return ChainAudit(ok=not problems, events_checked=checked,
                           streams_checked=len(streams), problems=problems)

    def take_checkpoint(self, signer: CheckpointSigner | None = None) -> dict:
        with self._pool.connection() as conn:
            heads = {r[0]: {"seq": r[1], "hash": r[2]} for r in conn.execute(
                "SELECT stream, seq, head_hash FROM vex_stream_head").fetchall()}
        cp: dict[str, Any] = {"id": new_id("ckpt"), "ts": now(), "heads": heads,
              "head_hash": sha256_hex(canonical_json(heads))}
        if signer is not None:
            cp["signature"] = signer.sign(cp["head_hash"])
            cp["signer"] = signer.key_id
        from psycopg.types.json import Jsonb
        with self._pool.connection() as conn:
            conn.execute(
                "INSERT INTO vex_checkpoint (id, ts, heads, head_hash, "
                "signature, signer) VALUES (%s,%s,%s,%s,%s,%s)",
                (cp["id"], cp["ts"], Jsonb(heads), cp["head_hash"],
                 cp.get("signature"), cp.get("signer")))
        return cp


def init_schema(store: PostgresEventStore) -> None:
    # psycopg3 executes multi-statement scripts via the simple query
    # protocol, so the whole file goes in one execute() — no fragile
    # split on ";" that would break on PL/pgSQL bodies or strings.
    schema_sql = SCHEMA_PATH.read_text()
    with store._pool.connection() as conn:
        reg = conn.execute(
            "SELECT to_regclass('public.vex_schema_migrations')").fetchone()
        db_version = None
        if reg and reg[0]:
            row = conn.execute(
                "SELECT max(version) FROM vex_schema_migrations").fetchone()
            db_version = row[0] if row else None
        if db_version is not None and db_version > SCHEMA_VERSION:
            raise RuntimeError(
                f"database schema version {db_version} is newer than this "
                f"code supports ({SCHEMA_VERSION}); upgrade the application")
        conn.execute(schema_sql)
        # Incremental migrations for databases created by older code:
        # sql/migrations/<version>_<slug>.sql files run once, in order,
        # recorded in vex_schema_migrations. schema.sql remains the
        # idempotent baseline for fresh installs (version = SCHEMA_VERSION).
        if db_version is None:
            db_version = SCHEMA_VERSION
        # Serialize concurrent init_schema callers (multi-worker boot):
        # two processes must not run the same DDL migration at once.
        conn.execute("SELECT pg_advisory_xact_lock(727272)")
        if MIGRATIONS_DIR.is_dir():
            pending = []
            for f in sorted(MIGRATIONS_DIR.glob("*.sql")):
                try:
                    v = int(f.name.split("_", 1)[0])
                except ValueError:
                    continue
                # Only migrations this code knows about (v <= SCHEMA_VERSION):
                # a file from a newer release must not mark the DB ahead
                # of the code and make every later init_schema refuse.
                if db_version < v <= SCHEMA_VERSION:
                    pending.append((v, f))
            for v, f in pending:
                with conn.transaction():
                    conn.execute(f.read_text())
                    conn.execute(
                        "INSERT INTO vex_schema_migrations (version) "
                        "VALUES (%s) ON CONFLICT (version) DO NOTHING", (v,))
        conn.execute(
            "INSERT INTO vex_schema_migrations (version) VALUES (%s) "
            "ON CONFLICT (version) DO NOTHING", (SCHEMA_VERSION,))


class PostgresSequenceGuard:
    """Persistent API anti-replay (§9.4 applied to the transport).

    Accepts a (credential, sequence) pair only the first time it sees
    it; order is not required. The PRIMARY KEY makes the decision atomic
    across processes.
    """

    def __init__(self, store: PostgresEventStore, retention_s: float = 900.0):
        self._pool = store._pool
        self._retention_s = retention_s
        self._purge_every = min(60.0, retention_s)
        # 0.0 forces a purge on the first call after a restart, instead of
        # waiting _purge_every seconds with stale nonces accumulated.
        self._last_purge = 0.0

    def check_and_advance(self, key: str, seq: int) -> bool:
        with self._pool.connection() as conn:
            cur = conn.execute(
                "INSERT INTO vex_api_nonce (k, seq) VALUES (%s, %s) "
                "ON CONFLICT (k, seq) DO NOTHING", (key, seq))
            nuevo = cur.rowcount == 1
            t = time.monotonic()
            if t - self._last_purge >= self._purge_every:
                self._last_purge = t
                conn.execute(
                    "DELETE FROM vex_api_nonce "
                    "WHERE seen_at < now() - make_interval(secs => %s::float8)",
                    (self._retention_s,))
        return nuevo