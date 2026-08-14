from mcp.server.runner import ServerRequestContext
from mcp.types import (
    Tool,
    TextContent,
    CallToolResult,
    ListToolsResult,
    PaginatedRequestParams,
    CallToolRequestParams,
)


async def list_tools(
    ctx: ServerRequestContext, params: PaginatedRequestParams | None = None
) -> ListToolsResult:
    return ListToolsResult(tools=[
        Tool(
            name="get_clara_version",
            description="Returns the current version string of the Clara MCP Server.",
            inputSchema={"type": "object", "properties": {}},
        )
    ])


async def call_tool(
    ctx: ServerRequestContext, params: CallToolRequestParams
) -> CallToolResult:
    if params.name == "get_clara_version":
        return CallToolResult(content=[TextContent(type="text", text="Clara MCP Server v0.1.0")])
    raise ValueError(f"Unknown tool: {params.name}")
