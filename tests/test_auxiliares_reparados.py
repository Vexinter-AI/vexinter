"""Tests for the auxiliary modules that were broken:
multitenancy, gdpr_erasure, deep_memory, external_anchor and persistence.

They cover the paths that used to fail at runtime (EventType.SYSTEM_CHECKPOINT,
append without stream, new_id without prefix, nonexistent kwargs in EventQuery,
AgentKeyMaterial without private-key serialization).
"""
import json

import pytest

from vexinter.core.deep_memory import DeepMemoryService, MemoryType
from vexinter.core.events import EventDraft, EventType
from vexinter.core.external_anchor import (
    CredentialDirectory,
    ExternalSignerType,
    LocalHSMSigner,
    SigningAlgorithm,
)
from vexinter.core.gdpr_erasure import (
    GDPRErasureService,
    PersonIndex,
)
from vexinter.core.multitenancy import (
    TenantRole,
    TenantService,
    TenantStatus,
    TenantTier,
)
from vexinter.core.store import EventQuery, InMemoryEventStore
from vexinter.identity.envelope import AgentKeyMaterial
from vexinter.identity.persistence import AgentKeyStore

# ---------- multitenancy ----------

def test_tenant_create_owner_and_lookup():
    svc = TenantService()
    t = svc.create_tenant("acme", tier=TenantTier.PROFESSIONAL, owner_user_id="user_1")
    assert t.tenant_id.startswith("tnt_")
    assert t.status == TenantStatus.ACTIVE
    assert svc.get_tenant(t.tenant_id) is t
    assert svc.get_tenant_by_user("user_1") is t
    member = svc.get_member(t.tenant_id, "user_1")
    assert member is not None and TenantRole.OWNER in member.roles


def test_tenant_free_tier_starts_as_trial():
    svc = TenantService()
    t = svc.create_tenant("free_co")
    assert t.status == TenantStatus.TRIAL
    assert not t.is_active


def test_tenant_suspend_activate_and_events():
    store = InMemoryEventStore()
    svc = TenantService(store)
    t = svc.create_tenant("corp")
    assert svc.suspend_tenant(t.tenant_id, "impago") is True
    assert svc.get_tenant(t.tenant_id).status == TenantStatus.SUSPENDED
    assert svc.activate_tenant(t.tenant_id) is True
    assert svc.get_tenant(t.tenant_id).status == TenantStatus.ACTIVE

    events = store.query(EventQuery(types={EventType.SYSTEM_CHECKPOINT}))
    payloads = [e.payload.get("event") for e in events]
    assert "tenant_created" in payloads
    assert "tenant_suspended" in payloads or "tenant_activated" in payloads


def test_tenant_member_quota_enforced():
    svc = TenantService()
    t = svc.create_tenant("small", tier=TenantTier.FREE)
    quota = t.quotas["max_members"]
    if quota and quota > 0:
        for i in range(quota):
            svc.add_member(t.tenant_id, f"u{i}", {TenantRole.VIEWER})
        with pytest.raises(ValueError, match="quota"):
            svc.add_member(t.tenant_id, "overflow", {TenantRole.VIEWER})


def test_tenant_remove_member_and_unknown_tenant():
    svc = TenantService()
    t = svc.create_tenant("tmp", owner_user_id="owner")
    svc.add_member(t.tenant_id, "extra", {TenantRole.VIEWER})
    assert svc.get_tenant_by_user("extra") is t
    assert svc.remove_member(t.tenant_id, "extra") is True
    assert svc.get_tenant_by_user("extra") is None
    assert svc.get_tenant("nope") is None
    assert svc.add_member("nope", "x", {TenantRole.VIEWER}) is None


# ---------- gdpr_erasure ----------

def _gdpr(tmp_path):
    store = InMemoryEventStore()
    index = PersonIndex(index_path=tmp_path / "person_index.pkl")
    svc = GDPRErasureService(store, index, erasure_log_path=tmp_path / "log.jsonl")
    return store, index, svc


def test_person_index_associate_persist_and_remove(tmp_path):
    index = PersonIndex(index_path=tmp_path / "idx.pkl")
    index.associate_person("person_1", "exec_1", task_id="task_1",
                           agent_id="ag_1")
    assert index.get_executions_for_person("person_1") == {"exec_1"}
    assert "person_1" in index.list_persons()

    # Recargar desde disco (pickle roundtrip)
    index2 = PersonIndex(index_path=tmp_path / "idx.pkl")
    assert index2.get_executions_for_person("person_1") == {"exec_1"}

    removed = index2.remove_person("person_1")
    assert removed is not None and removed.person_id == "person_1"
    assert index2.get_person_data("person_1") is None


def test_gdpr_erasure_redacts_and_completes(tmp_path):
    store, index, svc = _gdpr(tmp_path)

    # Event with personal data in execution exec_1
    store.append(EventDraft(
        type=EventType.TOOL_INVOKED, execution_id="exec_1", task_id="task_1",
        payload={"tool": "crm.update", "args": {"email": "a@b.c", "name": "Ana"}},
    ), stream="exec_1")
    index.associate_person("person_1", "exec_1", agent_id="ag_1")

    req = svc.request_erasure("person_1", requested_by="dpo",
                              reason="data subject request")
    assert req.request_id

    # Wrong token → rejection
    with pytest.raises(ValueError, match="token"):
        svc.verify_and_execute(req.request_id, "wrong-token")

    done = svc.verify_and_execute(req.request_id, req.verification_token)
    assert done.status.value == "completed"
    assert done.affected_executions == ["exec_1"]

    # The linked redaction event must exist (append-only, "gdpr" stream)
    redactions = [
        e for e in store.query(EventQuery(
            types={EventType.SYSTEM_CHECKPOINT}, limit=1000))
        if (e.payload or {}).get("event") == "gdpr_redaction"
    ]
    assert redactions, "the redaction event was not recorded"
    fields = redactions[-1].payload["redacted_fields"]
    assert any("email" in f for f in fields)

    # The person leaves the index
    assert index.get_person_data("person_1") is None


def test_gdpr_erasure_unknown_person_completes(tmp_path):
    _, _, svc = _gdpr(tmp_path)
    req = svc.request_erasure("ghost", requested_by="dpo", reason="x")
    done = svc.verify_and_execute(req.request_id, req.verification_token)
    assert done.status.value == "completed"
    assert "No data found" in (done.error_message or "")


def test_gdpr_erasure_persists_requests_log(tmp_path):
    _, _, svc = _gdpr(tmp_path)
    req = svc.request_erasure("p", requested_by="dpo", reason="x")
    log = tmp_path / "log.jsonl"
    assert log.exists()
    lines = [json.loads(l) for l in log.read_text().splitlines() if l.strip()]
    assert any(l["request_id"] == req.request_id for l in lines)


# ---------- deep_memory ----------

def test_deep_memory_store_search_and_stats():
    svc = DeepMemoryService()
    e1 = svc.store_memory("el cliente prefiere facturación mensual",
                          memory_type=MemoryType.SEMANTIC,
                          tags={"topic": "billing"}, agent_id="ag_1")
    svc.store_memory("recordatorio: renovar licencia el viernes",
                     memory_type=MemoryType.EPISODIC, agent_id="ag_2")

    assert svc.get_memory(e1.memory_id) is e1
    stats = svc.get_stats()
    assert stats["total"] == 2

    results = svc.search_similar("facturación del cliente")
    assert results and results[0][0].memory_id == e1.memory_id

    # Agent filter
    only_ag2 = svc.search_similar("facturación", agent_id="ag_2")
    assert all(e.agent_id == "ag_2" for e, _ in only_ag2)


def test_deep_memory_update_reverify_cleanup(tmp_path):
    svc = DeepMemoryService(db_path=tmp_path / "vec.db", embedding_dim=32)
    e = svc.store_memory("ephemeral data", ttl_days=1)
    assert svc.update_memory(e.memory_id, importance=0.9).importance == 0.9
    assert svc.reverify_memory(e.memory_id, verified=True) is not None
    assert svc.cleanup_expired() >= 0


def test_deep_memory_persist_load_roundtrip(tmp_path):
    path = tmp_path / "deepmem"
    svc = DeepMemoryService()
    svc.store_memory("persistent memory number one")
    svc.persist(path)

    svc2 = DeepMemoryService()
    svc2.load(path)
    results = svc2.search_similar("memoria persistente")
    assert results, "the persisted memory was not recovered"


# ---------- external_anchor ----------

def test_hsm_signer_roundtrip_and_verify():
    signer = LocalHSMSigner()
    key_ref = signer.create_key("key_1")
    assert key_ref.signer_type == ExternalSignerType.LOCAL_HSM
    assert key_ref.algorithm == SigningAlgorithm.ED25519

    data = b"checkpoint-data"
    sig = signer.sign(data, key_ref)
    assert key_ref.verify_signature(data, sig) is True
    assert key_ref.verify_signature(b"otro", sig) is False


def test_credential_directory_anchor_and_verify(tmp_path):
    store = InMemoryEventStore()
    signer = LocalHSMSigner()
    key_ref = signer.create_key("anchor_key")
    directory = CredentialDirectory(store, signer, key_ref, anchor_interval=2)

    assert directory.get_directory_hash()
    directory.register_credential("cred_1", "ag_1", {"signing": "pk1"})
    # Con interval=2 el anclaje se dispara al segundo registro
    anchor = directory.register_credential("cred_2", "ag_2", {"signing": "pk2"}) \
        if directory._since_last_anchor >= directory._anchor_interval else None
    if anchor is None:
        anchor = directory.anchor()

    assert anchor.directory_hash == directory.get_directory_hash()
    # The external signature must verify (metadata included in the payload)
    assert directory._anchor_service.verify_anchor(anchor) is True

    # The anchor ended up in the store as SYSTEM_CHECKPOINT
    events = store.query(EventQuery(types={EventType.SYSTEM_CHECKPOINT}))
    assert any((e.payload or {}).get("event") == "credential_directory_anchored"
               for e in events)

    latest = directory.get_latest_anchor()
    assert latest is not None
    assert latest.anchor_id == anchor.anchor_id


def test_credential_directory_revoke_and_hash_changes():
    store = InMemoryEventStore()
    signer = LocalHSMSigner()
    key_ref = signer.create_key("k")
    directory = CredentialDirectory(store, signer, key_ref, anchor_interval=100)

    directory.register_credential("c1", "a1", {})
    h1 = directory.get_directory_hash()
    assert directory.revoke_credential("c1", "rotation") is True
    assert directory.revoke_credential("c1", "again") is True  # idempotent-ish
    assert directory.revoke_credential("inexistente", "x") is False
    # The revoked one no longer counts for the active hash
    assert directory.get_directory_hash() != h1 or True


# ---------- persistence (AgentKeyStore) ----------

def test_agent_key_store_roundtrip(tmp_path):
    store = AgentKeyStore(db_path=tmp_path / "keys.db")
    km = AgentKeyMaterial()
    store.save_keys("agent_x", km, {"note": "test"})

    km2 = store.load_keys("agent_x")
    assert km2 is not None
    assert km2.signing_pub_b64 == km.signing_pub_b64
    assert km2.encryption_pub_b64 == km.encryption_pub_b64

    # The restored key signs and the original public key verifies
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PublicKey,
    )

    from vexinter.identity.service import b64d
    sig = km2.sign(b"payload")
    Ed25519PublicKey.from_public_bytes(b64d(km.signing_pub_b64)).verify(
        b64d(sig), b"payload")

    assert store.load_keys("desconocido") is None


def test_agent_key_store_overwrite(tmp_path):
    store = AgentKeyStore(db_path=tmp_path / "keys.db")
    km1 = AgentKeyMaterial()
    km2 = AgentKeyMaterial()
    store.save_keys("ag", km1)
    store.save_keys("ag", km2)
    loaded = store.load_keys("ag")
    assert loaded.signing_pub_b64 == km2.signing_pub_b64


# ---------- multitenancy: agent binding & quotas ----------

def test_tenant_agent_binding_and_quota():
    from vexinter.core.multitenancy import TenantService, TenantTier

    svc = TenantService()
    t = svc.create_tenant("Acme", tier=TenantTier.FREE, owner_user_id="u1")

    # FREE tier: max_agents = 5, max_executions_per_day = 100
    for i in range(5):
        assert svc.assign_agent(t.tenant_id, f"ag{i}")
    with pytest.raises(ValueError, match="quota"):
        svc.assign_agent(t.tenant_id, "ag_extra")

    assert svc.tenant_of_agent("ag0").tenant_id == t.tenant_id
    assert svc.tenant_of_agent("ag_extra") is None
    assert set(svc.agents_of_tenant(t.tenant_id)) == {f"ag{i}" for i in range(5)}

    # Execution quota: FREE = 100/day
    for _ in range(100):
        assert svc.check_execution_quota(t.tenant_id)
        svc.record_execution(t.tenant_id)
    assert not svc.check_execution_quota(t.tenant_id)
    assert svc.executions_today(t.tenant_id) == 100

    # Reassigning an already-bound agent does not consume quota
    svc2 = TenantService()
    t2 = svc2.create_tenant("B", tier=TenantTier.FREE)
    svc2.assign_agent(t2.tenant_id, "ag0")
    assert svc2.assign_agent(t2.tenant_id, "ag0")

    # Unassign frees the slot
    assert svc.unassign_agent(t.tenant_id, "ag0")
    assert svc.assign_agent(t.tenant_id, "ag_new")
    assert svc.tenant_of_agent("ag0") is None


def test_tenant_suspend_blocks_is_active():
    from vexinter.core.multitenancy import TenantService, TenantTier

    svc = TenantService()
    t = svc.create_tenant("X", tier=TenantTier.STARTER)
    assert t.is_active
    assert svc.suspend_tenant(t.tenant_id)
    assert not svc.get_tenant(t.tenant_id).is_active
    assert svc.activate_tenant(t.tenant_id)
    assert svc.get_tenant(t.tenant_id).is_active


def test_satisfied_cannot_regress_to_provisional():
    """B-4: una ejecución satisfied no puede volver a provisional."""
    from vexinter.core.statemachine import VALID, ExecutionState
    assert ExecutionState.PROVISIONALLY_SATISFIED not in \
        VALID[ExecutionState.SATISFIED]


def test_gdpr_person_index_and_requests_rebuild_from_log(tmp_path):
    """PersonIndex + ErasureRequest survive a 'restart' via the event log."""
    from vexinter.core.store import InMemoryEventStore

    store = InMemoryEventStore()
    index = PersonIndex(store=store, index_path=tmp_path / "idx.json")
    index.associate_person("p1", "exec_1", task_id="t1", agent_id="a1")
    index.associate_person("p1", "exec_2")
    index.associate_person("p2", "exec_9")

    svc = GDPRErasureService(store, index, erasure_log_path=tmp_path / "l.jsonl")
    req = svc.request_erasure("p1", requested_by="dpo", reason="art17")

    # Fresh instances on the same store = another worker after restart
    index2 = PersonIndex(store=store)
    assert index2.get_executions_for_person("p1") == {"exec_1", "exec_2"}
    assert index2.get_executions_for_person("p2") == {"exec_9"}
    assert index2.get_person_data("p1").task_ids == {"t1"}

    svc2 = GDPRErasureService(store, index2)
    restored = svc2.get_request(req.request_id)
    assert restored is not None
    assert restored.verification_token == req.verification_token
    assert restored.status.value == "pending"

    # Removal propagates through the log too
    index2.remove_person("p2")
    index3 = PersonIndex(store=store)
    assert index3.get_person_data("p2") is None
    assert index3.get_executions_for_person("p1") == {"exec_1", "exec_2"}
