"""Deep memory: persistent indexes, similarity retrieval, validity with re-verification (Item 14).

Extends the memory system (Phase 6) with:
- Persistent vector indexes (SQLite + sqlite-vec / optional FAISS)
- Semantic search via embeddings
- Validity with periodic re-verification
- Configurable temporal decay
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any

import numpy as np

from .events import EventDraft, EventType, new_id
from .store import InMemoryEventStore as EventStore


class MemoryType(str, Enum):
    """Memory types (extended from Phase 6)."""
    EPISODIC = "episodic"      # Concrete events with timestamp
    SEMANTIC = "semantic"      # General knowledge, facts
    WORKING = "working"        # Active task context
    PROCEDURAL = "procedural"  # Skills, action patterns
    REFLECTIVE = "reflective"  # Learned lessons, meta-cognition


class MemoryStatus(str, Enum):
    """States of a memory."""
    ACTIVE = "active"
    CONSOLIDATED = "consolidated"
    ARCHIVED = "archived"
    EXPIRED = "expired"
    REVERIFIED = "reverified"


@dataclass
class MemoryVector:
    """Embedding vector for semantic search."""
    memory_id: str
    vector: np.ndarray  # Embedding (normalizado)
    metadata: dict = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    last_accessed: datetime = field(default_factory=lambda: datetime.now(UTC))
    access_count: int = 0
    decay_factor: float = 1.0  # Decay factor [0,1]


@dataclass
class DeepMemoryEntry:
    """Deep memory entry with persistence and similarity."""
    memory_id: str
    memory_type: MemoryType
    content: str  # Original text
    summary: str  # Compressed summary
    embedding: np.ndarray | None = None
    tags: set[str] = field(default_factory=set)
    source_execution_id: str | None = None
    source_task_id: str | None = None
    agent_id: str | None = None
    importance: float = 0.5  # [0,1]
    status: MemoryStatus = MemoryStatus.ACTIVE
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime | None = None
    last_reverified: datetime | None = None
    verification_count: int = 0
    metadata: dict = field(default_factory=dict)
    
    def is_expired(self) -> bool:
        if self.expires_at is None:
            return False
        return datetime.now(UTC) > self.expires_at
    
    def needs_reverification(self, interval_days: int = 30) -> bool:
        """Checks whether periodic re-verification is needed."""
        if self.last_reverified is None:
            return True
        return (datetime.now(UTC) - self.last_reverified).days >= interval_days


class VectorIndex(ABC):
    """Abstract interface for vector indexes."""
    
    @abstractmethod
    def add(self, vectors: list[MemoryVector]) -> None:
        pass
    
    @abstractmethod
    def search(self, query_vector: np.ndarray, k: int = 10, 
               filter_fn=None) -> list[tuple[MemoryVector, float]]:
        """Searches for the k most similar vectors. Returns (vector, score)."""
    
    @abstractmethod
    def remove(self, memory_ids: list[str]) -> None:
        pass
    
    @abstractmethod
    def persist(self, path: Path) -> None:
        pass
    
    @abstractmethod
    def load(self, path: Path) -> None:
        pass


class SimpleVectorIndex(VectorIndex):
    """Simple in-memory vector index with disk persistence (numpy).

    For production, use FAISS, sqlite-vec, or pgvector.
    """
    
    def __init__(self, dimension: int = 384):
        self.dimension = dimension
        self._vectors: dict[str, MemoryVector] = {}
        self._matrix: np.ndarray | None = None
        self._ids: list[str] = []
    
    def add(self, vectors: list[MemoryVector]) -> None:
        for v in vectors:
            if v.vector.shape != (self.dimension,):
                raise ValueError(f"Expected dimension {self.dimension}, got {v.vector.shape}")
            # Normalize
            norm = np.linalg.norm(v.vector)
            if norm > 0:
                v.vector = v.vector / norm
            self._vectors[v.memory_id] = v
        self._rebuild_matrix()
    
    def _rebuild_matrix(self) -> None:
        self._ids = list(self._vectors.keys())
        if self._ids:
            self._matrix = np.stack([self._vectors[id].vector for id in self._ids])
        else:
            self._matrix = None
    
    def search(self, query_vector: np.ndarray, k: int = 10, 
               filter_fn=None) -> list[tuple[MemoryVector, float]]:
        if self._matrix is None or len(self._ids) == 0:
            return []
        
        # Normalize query
        norm = np.linalg.norm(query_vector)
        if norm > 0:
            query_vector = query_vector / norm
        
        # Cosine similarity
        scores = self._matrix @ query_vector
        
        # Filter if there is a filter_fn
        if filter_fn:
            valid_indices = [i for i, id in enumerate(self._ids) if filter_fn(self._vectors[id])]
            if not valid_indices:
                return []
            scores = scores[valid_indices]
            valid_ids = [self._ids[i] for i in valid_indices]
        else:
            valid_ids = self._ids
        
        # Top-k
        top_k = min(k, len(valid_ids))
        top_indices = np.argpartition(scores, -top_k)[-top_k:]
        top_indices = top_indices[np.argsort(scores[top_indices])][::-1]
        
        results = []
        for idx in top_indices:
            memory_id = valid_ids[idx]
            results.append((self._vectors[memory_id], float(scores[idx])))
        
        return results
    
    def remove(self, memory_ids: list[str]) -> None:
        for mid in memory_ids:
            self._vectors.pop(mid, None)
        self._rebuild_matrix()
    
    def persist(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        data: dict[str, Any] = {
            "dimension": self.dimension,
            "vectors": {k: {**v.__dict__, "vector": v.vector.tolist()} 
                       for k, v in self._vectors.items()}
        }
        # Convert numpy arrays to lists for JSON
        for v_data in data["vectors"].values():
            v_data["vector"] = v_data["vector"]  # Already a list
            v_data["created_at"] = v_data["created_at"].isoformat()
            v_data["last_accessed"] = v_data["last_accessed"].isoformat()
        with open(path, "w") as f:
            json.dump(data, f)
    
    def load(self, path: Path) -> None:
        with open(path, "r") as f:
            data = json.load(f)
        self.dimension = data["dimension"]
        self._vectors = {}
        for k, v_data in data["vectors"].items():
            v_data["vector"] = np.array(v_data["vector"])
            v_data["created_at"] = datetime.fromisoformat(v_data["created_at"])
            v_data["last_accessed"] = datetime.fromisoformat(v_data["last_accessed"])
            self._vectors[k] = MemoryVector(**v_data)
        self._rebuild_matrix()


class SQLiteVecIndex(VectorIndex):
    """Vector index using sqlite-vec (SQLite extension for vectors).

    Requires: pip install sqlite-vec
    """
    
    def __init__(self, db_path: Path, dimension: int = 384):
        self.db_path = Path(db_path)
        self.dimension = dimension
        self._conn: sqlite3.Connection | None = None
        self._init_db()
    
    def _init_db(self) -> None:
        self._conn = sqlite3.connect(self.db_path)
        self._conn.enable_load_extension(True)
        try:
            import sqlite_vec  # type: ignore[import-not-found]
            sqlite_vec.load(self._conn)
            self._has_vec = True
        except Exception:  # noqa: BLE001 - error boundary: converted into result/log
            self._has_vec = False
            # Without the extension, search() returns no results at all —
            # make the degradation loud instead of silently returning [].
            import logging
            logging.getLogger("vexinter.deep_memory").warning(
                "sqlite-vec not available: vector search on %s is DISABLED "
                "(install sqlite-vec or use SimpleVectorIndex)", self.db_path)
        
        if self._has_vec:
            self._conn.execute(f"""
                CREATE VIRTUAL TABLE IF NOT EXISTS vectors USING vec0(
                    memory_id TEXT PRIMARY KEY,
                    embedding FLOAT[{self.dimension}],
                    metadata TEXT,
                    created_at TEXT,
                    last_accessed TEXT,
                    access_count INTEGER,
                    decay_factor REAL
                )
            """)
        else:
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS vectors (
                    memory_id TEXT PRIMARY KEY,
                    embedding BLOB,
                    metadata TEXT,
                    created_at TEXT,
                    last_accessed TEXT,
                    access_count INTEGER,
                    decay_factor REAL
                )
            """)
        self._conn.commit()
    
    def add(self, vectors: list[MemoryVector]) -> None:
        if not self._conn:
            return
        for v in vectors:
            norm = np.linalg.norm(v.vector)
            vec = (v.vector / norm).astype(np.float32) if norm > 0 else v.vector.astype(np.float32)
            
            if self._has_vec:
                self._conn.execute("""
                    INSERT OR REPLACE INTO vectors 
                    (memory_id, embedding, metadata, created_at, last_accessed, access_count, decay_factor)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    v.memory_id, vec.tobytes(), json.dumps(v.metadata),
                    v.created_at.isoformat(), v.last_accessed.isoformat(),
                    v.access_count, v.decay_factor
                ))
            else:
                self._conn.execute("""
                    INSERT OR REPLACE INTO vectors 
                    (memory_id, embedding, metadata, created_at, last_accessed, access_count, decay_factor)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    v.memory_id, vec.tobytes(), json.dumps(v.metadata),
                    v.created_at.isoformat(), v.last_accessed.isoformat(),
                    v.access_count, v.decay_factor
                ))
        self._conn.commit()
    
    def search(self, query_vector: np.ndarray, k: int = 10, 
               filter_fn=None) -> list[tuple[MemoryVector, float]]:
        if not self._conn or not self._has_vec:
            return []
        
        norm = np.linalg.norm(query_vector)
        query_vec = (query_vector / norm).astype(np.float32) if norm > 0 else query_vector.astype(np.float32)
        
        # Use vec0 for cosine similarity search
        cursor = self._conn.execute("""
            SELECT memory_id, embedding, metadata, created_at, last_accessed, access_count, decay_factor,
                   vec_distance_cosine(embedding, ?) as distance
            FROM vectors
            ORDER BY distance ASC
            LIMIT ?
        """, (query_vec.tobytes(), k))
        
        results = []
        for row in cursor.fetchall():
            memory_id, embedding, metadata, created_at, last_accessed, access_count, decay_factor, distance = row
            score = 1.0 - distance  # Convert cosine distance to similarity
            vec = MemoryVector(
                memory_id=memory_id,
                vector=np.frombuffer(embedding, dtype=np.float32),
                metadata=json.loads(metadata),
                created_at=datetime.fromisoformat(created_at),
                last_accessed=datetime.fromisoformat(last_accessed),
                access_count=access_count,
                decay_factor=decay_factor,
            )
            if filter_fn and not filter_fn(vec):
                continue
            results.append((vec, score))
        
        return results
    
    def remove(self, memory_ids: list[str]) -> None:
        if not self._conn:
            return
        placeholders = ",".join("?" * len(memory_ids))
        self._conn.execute(f"DELETE FROM vectors WHERE memory_id IN ({placeholders})", memory_ids)
        self._conn.commit()
    
    def persist(self, path: Path) -> None:
        # SQLite already persists automatically
        pass
    
    def load(self, path: Path) -> None:
        # Already loaded on initialization
        pass


class DeepMemoryService:
    """Deep memory service with persistent indexes and similarity."""
    
    def __init__(
        self,
        store: EventStore | None = None,
        vector_index: VectorIndex | None = None,
        db_path: Path | None = None,
        embedding_dim: int = 384,
        default_ttl_days: int = 365,
        reverify_interval_days: int = 30,
    ):
        self._store = store
        self._embedding_dim = embedding_dim
        self._default_ttl = timedelta(days=default_ttl_days)
        self._reverify_interval = timedelta(days=reverify_interval_days)
        
        # Vector index
        if vector_index:
            self._vector_index = vector_index
        elif db_path:
            self._vector_index = SQLiteVecIndex(db_path, embedding_dim)
        else:
            self._vector_index = SimpleVectorIndex(embedding_dim)
        
        # Store of complete entries
        self._entries: dict[str, DeepMemoryEntry] = {}
        self._embedding_fn: Callable | None = None
    
    def set_embedding_function(self, fn: Callable) -> None:
        """Sets the function to generate embeddings (e.g.: sentence-transformers)."""
        self._embedding_fn = fn
    
    def _generate_embedding(self, text: str) -> np.ndarray:
        """Generates an embedding for a text."""
        if self._embedding_fn:
            return self._embedding_fn(text)
        # Fallback: deterministic hash (not truly semantic)
        hash_bytes = hashlib.sha256(text.encode()).digest()
        # Expand to required dimension
        vec = np.frombuffer(hash_bytes, dtype=np.uint8).astype(np.float32)
        if len(vec) < self._embedding_dim:
            vec = np.tile(vec, (self._embedding_dim // len(vec) + 1))[:self._embedding_dim]
        else:
            vec = vec[:self._embedding_dim]
        # Normalize
        norm = np.linalg.norm(vec)
        return vec / norm if norm > 0 else vec
    
    def store_memory(
        self,
        content: str,
        memory_type: MemoryType = MemoryType.EPISODIC,
        summary: str | None = None,
        tags: set[str] | None = None,
        source_execution_id: str | None = None,
        source_task_id: str | None = None,
        agent_id: str | None = None,
        importance: float = 0.5,
        ttl_days: int | None = None,
        metadata: dict | None = None,
    ) -> DeepMemoryEntry:
        """Stores a new memory with embedding."""
        memory_id = new_id("mem")
        
        # Generate embedding
        embedding = self._generate_embedding(content)
        
        # Create entry
        expires_at = None
        if ttl_days is not None:
            expires_at = datetime.now(UTC) + timedelta(days=ttl_days)
        elif self._default_ttl:
            expires_at = datetime.now(UTC) + self._default_ttl
        
        entry = DeepMemoryEntry(
            memory_id=memory_id,
            memory_type=memory_type,
            content=content,
            summary=summary or content[:200],
            embedding=embedding,
            tags=tags or set(),
            source_execution_id=source_execution_id,
            source_task_id=source_task_id,
            agent_id=agent_id,
            importance=importance,
            expires_at=expires_at,
            metadata=metadata or {},
        )
        
        # Save to vector index
        vec = MemoryVector(
            memory_id=memory_id,
            vector=embedding,
            metadata={
                "type": memory_type.value,
                "summary": entry.summary,
                "tags": list(entry.tags),
                "importance": importance,
            },
        )
        self._vector_index.add([vec])
        self._entries[memory_id] = entry
        
        # Log to store
        if self._store:
            self._store.append(EventDraft(
                type=EventType.SYSTEM_CHECKPOINT,
                payload={
                    "event": "memory_stored",
                    "memory_id": memory_id,
                    "type": memory_type.value,
                    "summary": entry.summary,
                    "tags": list(tags) if tags else [],
                }
            ), stream="memory")
        
        return entry
    
    def search_similar(
        self,
        query: str,
        k: int = 10,
        memory_types: list[MemoryType] | None = None,
        min_importance: float = 0.0,
        agent_id: str | None = None,
    ) -> list[tuple[DeepMemoryEntry, float]]:
        """Searches for semantically similar memories."""
        query_vector = self._generate_embedding(query)
        
        def filter_fn(vec: MemoryVector) -> bool:
            meta = vec.metadata
            if memory_types and meta.get("type") not in [t.value for t in memory_types]:
                return False
            if meta.get("importance", 0) < min_importance:
                return False
            if agent_id:
                entry = self._entries.get(vec.memory_id)
                if entry and entry.agent_id != agent_id:
                    return False
            return True
        
        results = self._vector_index.search(query_vector, k=k, filter_fn=filter_fn)
        
        # Convertir a entradas completas y actualizar acceso
        final_results = []
        for vec, score in results:
            entry = self._entries.get(vec.memory_id)
            if entry:
                vec.last_accessed = datetime.now(UTC)
                vec.access_count += 1
                final_results.append((entry, score))
        
        return final_results
    
    def get_memory(self, memory_id: str) -> DeepMemoryEntry | None:
        return self._entries.get(memory_id)
    
    def update_memory(self, memory_id: str, **kwargs) -> DeepMemoryEntry | None:
        entry = self._entries.get(memory_id)
        if not entry:
            return None
        
        for key, value in kwargs.items():
            if hasattr(entry, key):
                setattr(entry, key, value)
        
        entry.updated_at = datetime.now(UTC)
        
        # Re-index if content changed
        if "content" in kwargs:
            entry.embedding = self._generate_embedding(entry.content)
            vec = MemoryVector(memory_id=memory_id, vector=entry.embedding)
            self._vector_index.add([vec])
        
        return entry
    
    def reverify_memory(self, memory_id: str, verified: bool = True) -> DeepMemoryEntry | None:
        """Marks a memory as re-verified."""
        entry = self._entries.get(memory_id)
        if not entry:
            return None
        
        entry.last_reverified = datetime.now(UTC)
        entry.verification_count += 1
        entry.status = MemoryStatus.REVERIFIED if verified else MemoryStatus.ACTIVE
        
        # Extend validity if verified
        if verified and entry.expires_at:
            entry.expires_at = datetime.now(UTC) + self._default_ttl
        
        return entry
    
    def consolidate_memories(self, older_than_days: int = 30) -> int:
        """Consolidates old memories: compresses, reduces importance."""
        cutoff = datetime.now(UTC) - timedelta(days=older_than_days)
        consolidated = 0
        
        for entry in self._entries.values():
            if (entry.status == MemoryStatus.ACTIVE and 
                entry.created_at < cutoff and
                entry.importance < 0.7):
                entry.status = MemoryStatus.CONSOLIDATED
                entry.importance *= 0.8  # Reduce importance
                consolidated += 1
        
        return consolidated
    
    def cleanup_expired(self) -> int:
        """Removes expired memories."""
        expired_ids = [
            mid for mid, entry in self._entries.items()
            if entry.is_expired()
        ]
        
        if expired_ids:
            self._vector_index.remove(expired_ids)
            for mid in expired_ids:
                del self._entries[mid]
        
        return len(expired_ids)
    
    def get_stats(self) -> dict:
        stats = {t.value: 0 for t in MemoryType}
        for entry in self._entries.values():
            stats[entry.memory_type.value] += 1
        
        return {
            "total": len(self._entries),
            "by_type": stats,
            "by_status": {s.value: sum(1 for e in self._entries.values() if e.status == s) 
                         for s in MemoryStatus},
            "expired": sum(1 for e in self._entries.values() if e.is_expired()),
            "needs_reverification": sum(1 for e in self._entries.values() 
                                       if e.needs_reverification(self._reverify_interval.days)),
        }
    
    def persist(self, path: Path) -> None:
        """Persists the vector index and the entries."""
        self._vector_index.persist(path / "vector_index.json")
        
        # Persist complete entries
        entries_data = {}
        for mid, entry in self._entries.items():
            d = entry.__dict__.copy()
            d["memory_type"] = entry.memory_type.value
            d["status"] = entry.status.value
            d["tags"] = list(entry.tags)
            d["created_at"] = entry.created_at.isoformat()
            d["updated_at"] = entry.updated_at.isoformat()
            d["expires_at"] = entry.expires_at.isoformat() if entry.expires_at else None
            d["last_reverified"] = entry.last_reverified.isoformat() if entry.last_reverified else None
            if entry.embedding is not None:
                d["embedding"] = entry.embedding.tolist()
            entries_data[mid] = d
        
        with open(path / "entries.json", "w") as f:
            json.dump(entries_data, f, default=str)
    
    def load(self, path: Path) -> None:
        """Loads the index and entries from disk."""
        self._vector_index.load(path / "vector_index.json")
        
        with open(path / "entries.json", "r") as f:
            entries_data = json.load(f)
        
        for mid, d in entries_data.items():
            d["memory_type"] = MemoryType(d["memory_type"])
            d["status"] = MemoryStatus(d["status"])
            d["tags"] = set(d["tags"])
            d["created_at"] = datetime.fromisoformat(d["created_at"])
            d["updated_at"] = datetime.fromisoformat(d["updated_at"])
            d["expires_at"] = datetime.fromisoformat(d["expires_at"]) if d["expires_at"] else None
            d["last_reverified"] = datetime.fromisoformat(d["last_reverified"]) if d["last_reverified"] else None
            if d.get("embedding"):
                d["embedding"] = np.array(d["embedding"])
            self._entries[mid] = DeepMemoryEntry(**d)


# === Factory to create service from environment ===

def create_deep_memory_from_env(store: EventStore | None = None) -> DeepMemoryService:
    """Creates a DeepMemoryService from environment variables.

    Variables:
    - VEXINTER_MEMORY_DB_PATH: path to SQLite (uses sqlite-vec if available)
    - VEXINTER_MEMORY_EMBEDDING_DIM: embedding dimension (default 384)
    - VEXINTER_MEMORY_TTL_DAYS: default TTL (default 365)
    - VEXINTER_MEMORY_REVERIFY_DAYS: re-verification interval (default 30)
    """
    db_path_str = os.environ.get("VEXINTER_MEMORY_DB_PATH")
    db_path = Path(db_path_str) if db_path_str else None
    
    embedding_dim = int(os.environ.get("VEXINTER_MEMORY_EMBEDDING_DIM", "384"))
    ttl_days = int(os.environ.get("VEXINTER_MEMORY_TTL_DAYS", "365"))
    reverify_days = int(os.environ.get("VEXINTER_MEMORY_REVERIFY_DAYS", "30"))
    
    return DeepMemoryService(
        store=store,
        db_path=db_path,
        embedding_dim=embedding_dim,
        default_ttl_days=ttl_days,
        reverify_interval_days=reverify_days,
    )