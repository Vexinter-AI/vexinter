"""Credential directory anchoring with external HSM/KMS signature (Item 12).

Allows signing checkpoints and credential directories using external keys
(HSM, KMS, Cloud KMS, etc.) for server non-trust.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any, Protocol

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding

from .events import EventDraft, EventType, new_id
from .store import InMemoryEventStore as EventStore


class ExternalSignerType(str, Enum):
    """Types of supported external signers."""
    HSM_PKCS11 = "hsm_pkcs11"
    AWS_KMS = "aws_kms"
    GCP_KMS = "gcp_kms"
    AZURE_KEY_VAULT = "azure_key_vault"
    LOCAL_HSM = "local_hsm"  # For development/testing
    CUSTOM = "custom"


class SigningAlgorithm(str, Enum):
    """Supported signing algorithms."""
    ED25519 = "Ed25519"
    RSA_PSS_SHA256 = "RSA-PSS-SHA256"
    ECDSA_P256_SHA256 = "ECDSA-P256-SHA256"


@dataclass
class ExternalKeyRef:
    """Reference to an external key (does not contain secret material)."""
    key_id: str
    signer_type: ExternalSignerType
    algorithm: SigningAlgorithm
    public_key_b64: str  # Public key in base64 for verification
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime | None = None

    def verify_signature(self, data: bytes, signature_b64: str) -> bool:
        """Verifies a signature using the referenced public key."""
        try:
            sig = base64.b64decode(signature_b64)
            pub_key = self._load_public_key()
            
            if self.algorithm == SigningAlgorithm.ED25519:
                pub_key.verify(sig, data)
                return True
            elif self.algorithm == SigningAlgorithm.RSA_PSS_SHA256:
                pub_key.verify(
                    sig, data,
                    padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
                    hashes.SHA256()
                )
                return True
        except Exception:  # noqa: BLE001 - error boundary: converted into result/log
            return False
        return False

    def _load_public_key(self):
        der = base64.b64decode(self.public_key_b64)
        if self.algorithm == SigningAlgorithm.ED25519:
            return ed25519.Ed25519PublicKey.from_public_bytes(der)
        elif self.algorithm == SigningAlgorithm.RSA_PSS_SHA256:
            return serialization.load_der_public_key(der)
        raise ValueError(f"Unsupported algorithm: {self.algorithm}")


class ExternalSigner(Protocol):
    """Interface for external signers (HSM/KMS)."""
    
    def sign(self, data: bytes, key_ref: ExternalKeyRef) -> str:
        """Signs data and returns the signature in base64."""
        ...
    
    def get_public_key(self, key_id: str) -> ExternalKeyRef:
        """Gets the public key reference."""
        ...


class LocalHSMSigner:
    """Local signer for development/testing (simulates HSM)."""
    
    def __init__(self, master_key: bytes | None = None):
        self._keys: dict[str, ed25519.Ed25519PrivateKey] = {}
        self._master_key = master_key or os.urandom(32)
    
    def create_key(self, key_id: str) -> ExternalKeyRef:
        """Generates a new Ed25519 key."""
        priv_key = ed25519.Ed25519PrivateKey.generate()
        self._keys[key_id] = priv_key
        pub_key = priv_key.public_key()
        return ExternalKeyRef(
            key_id=key_id,
            signer_type=ExternalSignerType.LOCAL_HSM,
            algorithm=SigningAlgorithm.ED25519,
            public_key_b64=base64.b64encode(
                pub_key.public_bytes_raw()
            ).decode(),
        )
    
    def sign(self, data: bytes, key_ref: ExternalKeyRef) -> str:
        priv_key = self._keys.get(key_ref.key_id)
        if not priv_key:
            raise ValueError(f"Key not found: {key_ref.key_id}")
        signature = priv_key.sign(data)
        return base64.b64encode(signature).decode()
    
    def get_public_key(self, key_id: str) -> ExternalKeyRef:
        priv_key = self._keys.get(key_id)
        if not priv_key:
            raise ValueError(f"Key not found: {key_id}")
        pub_key = priv_key.public_key()
        return ExternalKeyRef(
            key_id=key_id,
            signer_type=ExternalSignerType.LOCAL_HSM,
            algorithm=SigningAlgorithm.ED25519,
            public_key_b64=base64.b64encode(
                pub_key.public_bytes_raw()
            ).decode(),
        )


class CheckpointAnchor:
    """Externally signed checkpoint anchoring.

    An anchored checkpoint contains:
    - Hash of the credential directory state
    - Timestamp
    - External signature (HSM/KMS)
    - Reference to the key used
    """
    
    def __init__(
        self,
        store: EventStore,
        signer: ExternalSigner,
        anchor_key_ref: ExternalKeyRef,
    ):
        self._store = store
        self._signer = signer
        self._anchor_key_ref = anchor_key_ref
    
    def create_anchor(self, directory_hash: str, metadata: dict | None = None) -> AnchoredCheckpoint:
        """Creates an anchored checkpoint with external signature."""
        anchor_id = new_id("anchor")
        timestamp = datetime.now(UTC)
        
        # Payload to sign
        payload = {
            "anchor_id": anchor_id,
            "directory_hash": directory_hash,
            "timestamp": timestamp.isoformat(),
            "key_id": self._anchor_key_ref.key_id,
            "metadata": metadata or {},
        }
        payload_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        
        # Sign with external HSM/KMS
        signature_b64 = self._signer.sign(payload_bytes, self._anchor_key_ref)
        
        anchored = AnchoredCheckpoint(
            anchor_id=anchor_id,
            directory_hash=directory_hash,
            timestamp=timestamp,
            key_ref=self._anchor_key_ref,
            signature_b64=signature_b64,
            payload=payload,
            metadata=metadata or {},
        )
        
        # Save to store
        self._store.append(EventDraft(
            type=EventType.SYSTEM_CHECKPOINT,
            payload={
                "anchored_checkpoint": anchored.to_dict(),
                "event": "credential_directory_anchored",
            }
        ), stream="anchor")
        
        return anchored
    
    def verify_anchor(self, anchor: AnchoredCheckpoint) -> bool:
        """Verifies the integrity of an anchored checkpoint."""
        # Rebuild payload
        payload = {
            "anchor_id": anchor.anchor_id,
            "directory_hash": anchor.directory_hash,
            "timestamp": anchor.timestamp.isoformat(),
            "key_id": anchor.key_ref.key_id,
            "metadata": anchor.metadata,
        }
        payload_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        
        # Verify signature
        return anchor.key_ref.verify_signature(payload_bytes, anchor.signature_b64)


@dataclass
class AnchoredCheckpoint:
    """Checkpoint with external anchoring."""
    anchor_id: str
    directory_hash: str
    timestamp: datetime
    key_ref: ExternalKeyRef
    signature_b64: str
    payload: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "anchor_id": self.anchor_id,
            "directory_hash": self.directory_hash,
            "timestamp": self.timestamp.isoformat(),
            "key_id": self.key_ref.key_id,
            "algorithm": self.key_ref.algorithm.value,
            "signature": self.signature_b64,
            "metadata": self.metadata,
        }


class CredentialDirectory:
    """Credential directory with periodic external anchoring.

    Maintains an index of active credentials and generates checkpoints
    signed by HSM/KMS for immutable auditing.
    """
    
    def __init__(
        self,
        store: EventStore,
        signer: ExternalSigner,
        anchor_key_ref: ExternalKeyRef,
        anchor_interval: int = 100,  # Anchor every N credentials
    ):
        self._store = store
        self._signer = signer
        self._anchor_key_ref = anchor_key_ref
        self._anchor_interval = anchor_interval
        self._credentials: dict[str, dict] = {}
        self._anchor_service = CheckpointAnchor(store, signer, anchor_key_ref)
        self._since_last_anchor = 0
    
    def register_credential(self, credential_id: str, agent_id: str, public_keys: dict) -> None:
        """Registers a credential in the directory."""
        self._credentials[credential_id] = {
            "credential_id": credential_id,
            "agent_id": agent_id,
            "public_keys": public_keys,
            "registered_at": datetime.now(UTC).isoformat(),
            "status": "active",
        }
        self._since_last_anchor += 1
        
        if self._since_last_anchor >= self._anchor_interval:
            self.anchor()
    
    def revoke_credential(self, credential_id: str, reason: str) -> bool:
        """Revokes a credential in the directory."""
        if credential_id in self._credentials:
            self._credentials[credential_id]["status"] = "revoked"
            self._credentials[credential_id]["revoked_at"] = datetime.now(UTC).isoformat()
            self._credentials[credential_id]["revoke_reason"] = reason
            self._since_last_anchor += 1
            
            if self._since_last_anchor >= self._anchor_interval:
                self.anchor()
            return True
        return False
    
    def get_directory_hash(self) -> str:
        """Computes a deterministic hash of the current directory."""
        # ALL credentials, sorted by ID — including revoked ones with
        # their status: a replica that omits revocation events must not
        # reproduce a previously anchored directory hash.
        active = sorted(
            self._credentials.values(),
            key=lambda x: x["credential_id"]
        )
        content = json.dumps(active, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(content).hexdigest()
    
    def anchor(self) -> AnchoredCheckpoint:
        """Generates an externally signed anchored checkpoint."""
        directory_hash = self.get_directory_hash()
        anchor = self._anchor_service.create_anchor(directory_hash, {
            "credential_count": len([c for c in self._credentials.values() if c["status"] == "active"]),
            "total_count": len(self._credentials),
        })
        self._since_last_anchor = 0
        return anchor
    
    def get_latest_anchor(self) -> AnchoredCheckpoint | None:
        """Gets the latest anchored checkpoint from the store."""
        # Search recent events
        from .store import EventQuery
        events = self._store.query(EventQuery(types={EventType.SYSTEM_CHECKPOINT}, limit=10))
        for ev in reversed(events):
            payload = ev.payload
            if isinstance(payload, dict) and payload.get("event") == "credential_directory_anchored":
                ac_data = payload.get("anchored_checkpoint")
                if ac_data:
                    return AnchoredCheckpoint(
                        anchor_id=ac_data["anchor_id"],
                        directory_hash=ac_data["directory_hash"],
                        timestamp=datetime.fromisoformat(ac_data["timestamp"]),
                        key_ref=ExternalKeyRef(
                            key_id=ac_data["key_id"],
                            signer_type=ExternalSignerType.LOCAL_HSM,
                            algorithm=SigningAlgorithm(ac_data["algorithm"]),
                            public_key_b64="",  # Would be loaded from registry
                        ),
                        signature_b64=ac_data["signature"],
                        metadata=ac_data.get("metadata", {}),
                    )
        return None


# === Factory to create signers from configuration ===

def create_signer_from_env() -> ExternalSigner:
    """Creates an external signer based on environment variables.

    Variables:
    - VEXINTER_HSM_TYPE: local|aws_kms|gcp_kms|azure
    - VEXINTER_HSM_KEY_ID: key ID in the HSM/KMS
    - VEXINTER_HSM_CREDENTIALS: path to credentials (JSON) if applicable
    """
    hsm_type = os.environ.get("VEXINTER_HSM_TYPE", "local").lower()
    
    if hsm_type == "local":
        return LocalHSMSigner()
    
    # For AWS KMS, GCP KMS, Azure Key Vault specific libraries would be
    # required along with credential configuration. Left as a stub here.
    raise NotImplementedError(
        f"HSM type '{hsm_type}' is not implemented. "
        f"Use 'local' for development or implement the corresponding connector."
    )


def create_credential_directory_from_env(store: EventStore) -> CredentialDirectory:
    """Creates a CredentialDirectory with signer and anchor key from environment."""
    signer = create_signer_from_env()
    
    # Create or get anchor key
    if isinstance(signer, LocalHSMSigner):
        anchor_key_ref = signer.create_key("credential-directory-anchor")
    else:
        # For real HSMs, the key would already exist
        key_id = os.environ.get("VEXINTER_ANCHOR_KEY_ID", "credential-directory-anchor")
        anchor_key_ref = signer.get_public_key(key_id)
    
    return CredentialDirectory(store, signer, anchor_key_ref)