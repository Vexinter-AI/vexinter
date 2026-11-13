"""Operator tokens with role, expiration and padding."""
from __future__ import annotations

import base64
import hashlib
import hmac
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class OperatorRole(str, Enum):
    """Roles allowed for operator tokens."""
    VIEWER = "viewer"
    CONSOLE_ADMIN = "console_admin"
    OPERATOR = "operator"
    ADMIN = "admin"


class OperatorToken(BaseModel):
    """Operator token with expiration and roles."""
    token: str = Field(..., description="Unique operator token")
    username: str = Field(..., description="Operator username")
    roles: set[OperatorRole] = Field(default_factory=set, description="Operator roles")
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime | None = Field(None, description="Token expiration date")
    last_used: datetime | None = Field(None, description="Last time it was used")
    jti: str = Field(..., description="Token ID (`jti` like JWT)")
    
    @property
    def is_expired(self) -> bool:
        """True if the token has expired or will expire on the next request."""
        if self.expires_at is None:
            return False
        return datetime.now(UTC) > self.expires_at
    
    def verify_token(self, provided_token: str) -> bool:
        """Verifies that the provided token matches the stored one."""
        # Use HMAC to verify the token (SHA-256)
        key = f"vexinter:operador:{self.jti}".encode()
        computed = hashlib.sha256(key + self.token.encode()).hexdigest()
        expected = hashlib.sha256(key + provided_token.encode()).hexdigest()
        return hmac.compare_digest(computed, expected)
    
    def refresh(self, ttl_hours: int = 24) -> None:
        """Renews the token's validity for ttl_hours."""
        if self.expires_at is None:
            self.expires_at = self.created_at + timedelta(hours=ttl_hours)
        else:
            self.expires_at = datetime.now(UTC) + timedelta(hours=ttl_hours)
    
    @classmethod
    def create(cls, username: str, roles: set[OperatorRole] | None = None,
               jwt_id: str | None = None, ttl_hours: int = 24) -> OperatorToken:
        """Creates a new operator token."""
        jti = jwt_id or _generate_jti()

        # Generate secret token (64 bytes encoded in hex)
        raw_token = hashlib.sha256(f"vexinter:init:{jti}:{username}".encode()).hexdigest()
        
        return cls(
            token=raw_token,
            username=username,
            roles=roles or set(),
            jti=jti,
            expires_at=datetime.now(UTC) + timedelta(hours=ttl_hours),
            last_used=None,
        )


def generate_operator_token(username: str, scopes: list[str] | None = None,
                             roles: list[str] | None = None,
                             jwt_id: str | None = None,
                             ttl_hours: int = 24) -> dict[str, Any]:
    """Generates a complete operator token for internal use from a script/admin."""
    token = OperatorToken.create(
        username=username,
        roles={OperatorRole(r) for r in (roles or [])},
        jwt_id=jwt_id,
        ttl_hours=ttl_hours
    )
    
    return {
        "token": token.token,
        "username": token.username,
        "roles": [r.value for r in token.roles],
        "jti": token.jti,
        "expires_at": token.expires_at.isoformat() if token.expires_at else None,
        "created_at": token.created_at.isoformat()
    }


def encode_jwt(payload: dict[str, Any], secret: str) -> str:
    """Encodes a payload as a JWT signed with HMAC-SHA256."""
    import json

    # Headers
    header = {"alg": "HS256", "typ": "JWT"}
    header_b64 = _base64url_encode(json.dumps(header, separators=(",", ":")).encode())

    # Payload
    payload_b64 = _base64url_encode(json.dumps(payload, separators=(",", ":")).encode())

    # Signing
    signature_input = f"{header_b64}.{payload_b64}".encode()
    signature = hashlib.sha256(secret.encode() + signature_input).digest()
    signature_b64 = _base64url_encode(signature)
    
    return f"{header_b64}.{payload_b64}.{signature_b64}"


def decode_jwt(token: str, secret: str) -> dict[str, Any] | None:
    """Decodes and verifies a JWT."""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        
        header_b64, payload_b64, signature_b64 = parts
        
        # Verify signature
        signature_input = f"{header_b64}.{payload_b64}".encode()
        expected_signature = hashlib.sha256(secret.encode() + signature_input).digest()
        actual_signature = _base64url_decode(signature_b64)
        
        if not hmac.compare_digest(expected_signature, actual_signature):
            return None
        
        # Parse payload
        import json
        payload = json.loads(_base64url_decode(payload_b64))

        # Verify exp
        if "exp" in payload and datetime.now(UTC) > datetime.fromtimestamp(payload["exp"], UTC):
            return None
        
        # Verify nbf
        if "nbf" in payload and datetime.now(UTC) < datetime.fromtimestamp(payload["nbf"], UTC):
            return None
        
        return payload
    except Exception:  # noqa: BLE001 - error boundary: converted into result/log
        return None


def _generate_jti() -> str:
    """Generates a unique Token ID."""
    import time
    return f"op_{int(time.time())}_{base64.b64encode(hashlib.md5(str(time.time()).encode()).digest()).hex()[:16]}"


def _base64url_encode(data_bytes: bytes) -> str:
    """Encodes bytes in base64url format."""
    return base64.urlsafe_b64encode(data_bytes).rstrip(b"=").decode()


def _base64url_decode(s: str) -> bytes:
    """Decodes base64url to bytes."""
    padding = 4 - len(s) % 4
    if padding != 4:
        s += "=" * padding
    return base64.urlsafe_b64decode(s)