"""Official SDK stdio fixture: nonce is created only inside an actual call."""
import secrets

try:
    from mcp.server.mcpserver import MCPServer
except ImportError:
    from mcp.server.fastmcp import FastMCP as MCPServer
from mcp.types import ToolAnnotations


server = MCPServer('synthetic-model-acceptance')


@server.tool(annotations=ToolAnnotations(readOnlyHint=True))
def nonce() -> str:
    """Return a fresh synthetic acceptance marker; no files or network."""
    return 'MCP_SYNTHETIC_' + secrets.token_hex(24)


if __name__ == '__main__':
    server.run(transport='stdio')
