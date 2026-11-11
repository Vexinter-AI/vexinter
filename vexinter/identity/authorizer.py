from __future__ import annotations

from .service import IdentityError


class IdentityAwareAuthorizer:
    """§9.2 with fail-closed (§14): without a valid credential, no operation.

    Authentication (who?) is resolved by `assert_credential_valid`;
    authorization (may they?) is resolved by the logical agent's policy.
    Moreover, the credential must BELONG to whoever claims to act: the agent
    (and the instance, if declared) in the context must match those of the
    credential, so that an act is not attributed to another agent.
    """

    def __init__(self, identity_service):
        self._ids = identity_service

    def can_invoke(self, agent_id: str, tool_key: str,
                   instance_id: str | None = None,
                   credential_id: str | None = None) -> bool:
        if credential_id is None:
            return False
        try:
            cred = self._ids.assert_credential_valid(credential_id)
        except IdentityError:
            return False
        if cred.agent_id != agent_id:
            return False
        if instance_id is not None and cred.instance_id != instance_id:
            return False
        return self._ids.can_invoke_tool(credential_id, tool_key)