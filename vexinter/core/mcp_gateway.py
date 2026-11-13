"""MCP Gateway proxy adapter (Fase 3)."""
from __future__ import annotations

import logging
from typing import Any

from .events import MCP_PROXIED, EventDraft, now
from .tools import Observation, OperationRequest, OperationResult, ToolAdapter, ToolSpec

_log = logging.getLogger(__name__)

class MCPAdapter(ToolAdapter):
    """Proxy adapter for the Model Context Protocol (MCP) Gateway (§3)."""

    def __init__(self, system_name: str, mcp_client: Any, store: Any = None):
        self.system = system_name
        self._client = mcp_client
        self._store = store
        self._tools_cache: dict[str, ToolSpec] = {}
        self._refresh_tools()

    def _refresh_tools(self):
        try:
            tools_list = self._client.list_tools() if hasattr(self._client, "list_tools") else []
            for t in tools_list:
                name = t.get("name") if isinstance(t, dict) else getattr(t, "name", str(t))
                desc = t.get("description", "") if isinstance(t, dict) else getattr(t, "description", "")
                schema = t.get("inputSchema", {}) if isinstance(t, dict) else getattr(t, "input_schema", {})
                self._tools_cache[name] = ToolSpec(
                    system=self.system,
                    name=name,
                    description=desc,
                    input_schema=schema,
                )
        except Exception as e:  # noqa: BLE001 - error boundary: converted into result/log
            _log.debug("could not refresh MCP tool catalog for %s: %s",
                       self.system, e)

    def tools(self) -> dict[str, ToolSpec]:
        self._refresh_tools()
        return self._tools_cache

    def invoke(self, tool: str, req: OperationRequest) -> OperationResult:
        try:
            # Invoke through the proxied MCP client
            res = self._client.call_tool(tool, req.args)
            data = res.get("content") if isinstance(res, dict) else res
            # Record MCP_PROXIED event
            if self._store is not None and req.task_id and req.execution_id:
                self._store.append(EventDraft(
                    type=MCP_PROXIED,
                    task_id=req.task_id,
                    execution_id=req.execution_id,
                    agent_id=req.agent_id,
                    payload={"system": self.system, "tool": tool, "args": req.args}
                ))

            # MCP signals tool-execution failures with isError=True in the
            # result envelope — that is NOT a successful call (the proxied
            # call still happened, hence the audit event above).
            if isinstance(res, dict) and res.get("isError"):
                return OperationResult(
                    ok=False, data=data,
                    error="MCP tool reported execution error")
            return OperationResult(ok=True, data=data)
        except Exception as e:  # noqa: BLE001 - error boundary: converted into result/log
            return OperationResult(ok=False, error=str(e))

    def observe(self, query: dict) -> Observation:
        try:
            res = self._client.read_resource(query.get("uri", "")) if hasattr(self._client, "read_resource") else None
            return Observation(data=res, observed_at=now(), source=f"{self.system}/mcp", consistency="strong")
        except Exception as e:  # noqa: BLE001 - error boundary: converted into result/log
            return Observation(data={"error": str(e)}, observed_at=now(), source=f"{self.system}/mcp", consistency="unknown")
