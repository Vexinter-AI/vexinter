"""Agent key persistence in encrypted SQLite (Item 9).

Stores AgentKeyMaterial (Ed25519/X25519 keys) persistently so they
survive restarts. Uses SQLite with optional encryption via SQLCipher
or application-level encryption with Fernet.
"""
from __future__ import annotations

import base64
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from .envelope import AgentKeyMaterial

SCHEMA_VERSION = 2

SCHEMA_SQL = f"""
PRAGMA user_version = {SCHEMA_VERSION};
CREATE TABLE IF NOT EXISTS agent_keys (
    agent_id TEXT PRIMARY KEY,
    signing_priv_b64 TEXT NOT NULL,
    encryption_priv_b64 TEXT NOT NULL,
    signing_pub_b64 TEXT NOT NULL,
    encryption_pub_b64 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    metadata_json TEXT DEFAULT '{{}}',
    previous_keys_enc TEXT
);
CREATE INDEX IF NOT EXISTS idx_agent_keys_updated ON agent_keys(updated_at);
"""

def _migrate_v2(conn) -> None:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(agent_keys)")}
    if "previous_keys_enc" not in cols:
        conn.execute(
            "ALTER TABLE agent_keys ADD COLUMN previous_keys_enc TEXT")


MIGRATIONS: dict[int, Any] = {2: _migrate_v2}


class EncryptedStorage:
    """Wrapper for application-level encryption (Fernet).

    If no master key is provided, stores in plaintext
    (development/testing only).
    """
    
    def __init__(self, master_key: bytes | None = None):
        self._fernet = Fernet(master_key) if master_key else None
    
    @staticmethod
    def derive_key(password: str, salt: bytes | None = None) -> tuple[bytes, bytes]:
        """Derives an encryption key from a password."""
        if salt is None:
            salt = os.urandom(16)
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=100_000,
        )
        key = base64.urlsafe_b64encode(kdf.derive(password.encode()))
        return key, salt
    
    def encrypt(self, data: str) -> str:
        if self._fernet is None:
            return data
        return self._fernet.encrypt(data.encode()).decode()
    
    def decrypt(self, data: str) -> str:
        if self._fernet is None:
            return data
        return self._fernet.decrypt(data.encode()).decode()


class AgentKeyStore:
    """Persistent agent key store with optional encryption."""
    
    def __init__(
        self,
        db_path: str | Path = "vexinter_agent_keys.db",
        master_key: bytes | None = None,
        salt: bytes | None = None,
    ):
        self.db_path = Path(db_path)
        self._storage = EncryptedStorage(master_key)
        self._salt = salt
        self._init_db()
        # WAL allows concurrent readers while a writer holds the DB;
        # without it multi-threaded save/load hits "database is locked".
        with self._conn() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
    
    def _init_db(self) -> None:
        """Initializes the database and applies migrations."""
        with self._conn() as conn:
            conn.executescript(SCHEMA_SQL)

            # Check version and apply migrations
            cursor = conn.execute("PRAGMA user_version")
            current_version = cursor.fetchone()[0]
            
            for version in range(current_version + 1, SCHEMA_VERSION + 1):
                if version in MIGRATIONS:
                    MIGRATIONS[version](conn)
                    conn.execute(f"PRAGMA user_version = {version}")
    
    @contextmanager
    def _conn(self):
        """Context manager for the SQLite connection."""
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.execute("PRAGMA busy_timeout = 10000")
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    
    def save_keys(self, agent_id: str, keys: AgentKeyMaterial, 
                  metadata: dict[str, Any] | None = None) -> None:
        """Saves or updates an agent's keys."""
        now = datetime.now(UTC).isoformat()
        signing_priv_enc = self._storage.encrypt(keys.signing_priv_b64)
        encryption_priv_enc = self._storage.encrypt(keys.encryption_priv_b64)
        previous_enc = self._storage.encrypt(
            json.dumps(keys.previous_keys_serialized()))
        
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO agent_keys 
                (agent_id, signing_priv_b64, encryption_priv_b64,
                 signing_pub_b64, encryption_pub_b64, created_at, updated_at,
                 metadata_json, previous_keys_enc)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(agent_id) DO UPDATE SET
                    signing_priv_b64 = excluded.signing_priv_b64,
                    encryption_priv_b64 = excluded.encryption_priv_b64,
                    signing_pub_b64 = excluded.signing_pub_b64,
                    encryption_pub_b64 = excluded.encryption_pub_b64,
                    updated_at = excluded.updated_at,
                    metadata_json = excluded.metadata_json,
                    previous_keys_enc = excluded.previous_keys_enc
            """, (
                agent_id,
                signing_priv_enc,
                encryption_priv_enc,
                keys.signing_pub_b64,
                keys.encryption_pub_b64,
                now,
                now,
                json.dumps(metadata or {}),
                previous_enc,
            ))
    
    def load_keys(self, agent_id: str) -> AgentKeyMaterial | None:
        """Loads an agent's keys."""
        with self._conn() as conn:
            cursor = conn.execute(
                "SELECT * FROM agent_keys WHERE agent_id = ?", (agent_id,)
            )
            row = cursor.fetchone()
            
            if row is None:
                return None
            
            previous = []
            try:
                raw_prev = row["previous_keys_enc"]
            except IndexError:      # column absent in pre-v2 databases
                raw_prev = None
            if raw_prev:
                try:
                    previous = json.loads(self._storage.decrypt(raw_prev))
                except Exception:  # noqa: BLE001 - corrupt blob: start clean
                    previous = []
            return AgentKeyMaterial.from_private_b64(
                self._storage.decrypt(row["signing_priv_b64"]),
                self._storage.decrypt(row["encryption_priv_b64"]),
                previous=previous,
            )
    
    def delete_keys(self, agent_id: str) -> bool:
        """Deletes an agent's keys. Returns True if they existed."""
        with self._conn() as conn:
            cursor = conn.execute(
                "DELETE FROM agent_keys WHERE agent_id = ?", (agent_id,)
            )
            return cursor.rowcount > 0
    
    def list_agents(self) -> list[str]:
        """Lists all stored agent_ids."""
        with self._conn() as conn:
            cursor = conn.execute("SELECT agent_id FROM agent_keys ORDER BY agent_id")
            return [row["agent_id"] for row in cursor.fetchall()]
    
    def get_metadata(self, agent_id: str) -> dict[str, Any] | None:
        """Gets the agent's metadata."""
        with self._conn() as conn:
            cursor = conn.execute(
                "SELECT metadata_json FROM agent_keys WHERE agent_id = ?", (agent_id,)
            )
            row = cursor.fetchone()
            if row:
                return json.loads(row["metadata_json"])
            return None
    
    @classmethod
    def create_from_password(
        cls,
        db_path: str | Path,
        password: str,
        salt: bytes | None = None,
    ) -> AgentKeyStore:
        """Creates a store deriving the key from a password."""
        key, derived_salt = EncryptedStorage.derive_key(password, salt)
        return cls(db_path=db_path, master_key=key, salt=derived_salt)
    
    def export_salt(self) -> bytes | None:
        """Exports the salt used for key derivation."""
        return self._salt


def create_agent_key_store_from_env() -> AgentKeyStore:
    """Creates AgentKeyStore from environment variables:

    - VEXINTER_KEYS_DB_PATH: path to the SQLite file (default: ./vexinter_agent_keys.db)
    - VEXINTER_KEYS_PASSWORD: encryption password (optional)
    - VEXINTER_KEYS_SALT: hex salt for derivation (optional, generated if missing)
    """
    db_path = os.environ.get("VEXINTER_KEYS_DB_PATH", "vexinter_agent_keys.db")
    password = os.environ.get("VEXINTER_KEYS_PASSWORD")
    salt_hex = os.environ.get("VEXINTER_KEYS_SALT")
    
    salt = bytes.fromhex(salt_hex) if salt_hex else None
    
    if password:
        return AgentKeyStore.create_from_password(db_path, password, salt)
    else:
        return AgentKeyStore(db_path=db_path)