"""Quick test to verify MCP Gateway functionality."""
from unittest.mock import Mock

from vexinter import Vexinter
from vexinter.core.mcp_gateway import MCPAdapter


def test_mcp_adapter_basic():
    """Test that MCPAdapter can be created and used."""
    # Create a mock MCP client
    mock_client = Mock()
    mock_client.list_tools.return_value = [
        {"name": "search", "description": "Search the web", "inputSchema": {"type": "object"}}
    ]
    mock_client.call_tool.return_value = {"content": [{"type": "text", "text": "results"}]}
    
    # Create adapter
    adapter = MCPAdapter("web", mock_client)
    
    # Test tools listing
    tools = adapter.tools()
    assert "search" in tools
    assert tools["search"].system == "web"
    
    # Test invoke
    from vexinter.core.tools import OperationRequest
    req = OperationRequest(tool="search", args={"query": "test"}, task_id="t1", execution_id="e1", agent_id="a1")
    res = adapter.invoke("search", req)
    assert res.ok is True
    assert res.data == [{"type": "text", "text": "results"}]
    
    print("MCPAdapter basic test passed!")


def test_mcp_integration():
    """Test MCP integration with Vexinter."""
    # Mock MCP client
    mock_client = Mock()
    mock_client.list_tools.return_value = [
        {"name": "search", "description": "Search", "inputSchema": {"type": "object"}}
    ]
    mock_client.call_tool.return_value = {"content": [{"type": "text", "text": "ok"}]}
    
    # Create Vexinter with MCP adapter
    vex = Vexinter(embedded=True, adapters=[MCPAdapter("web", mock_client)])
    
    with vex.task(agent="a", objective="test", condition={"op": "exists", "system": "web", "query": {}}, input={}) as t:
        # List MCP tools
        mcp_tools = t.mcp_list_tools()
        assert "web.search" in mcp_tools["tools"]
        
        # Invoke MCP tool
        res = t.mcp_invoke("web", "search", {"query": "test"})
        assert res.ok is True
        assert res.data == [{"type": "text", "text": "ok"}]
    
    print("MCP integration test passed!")


if __name__ == "__main__":
    test_mcp_adapter_basic()
    test_mcp_integration()
    print("All MCP tests passed!")