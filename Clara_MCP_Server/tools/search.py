"""
Clara_MCP_Server/tools/search.py — web_search MCP tool.

Performs web searches via the local search proxy service. Enforces
MAX_SEARCHES_PER_SESSION using a module-level counter.
"""

from mcp.server.runner import ServerRequestContext
from mcp.types import (
    Tool,
    TextContent,
    CallToolResult,
    ListToolsResult,
    PaginatedRequestParams,
    CallToolRequestParams,
)

import httpx

SEARCH_BASE = "http://10.0.0.200:4000"
MAX_SEARCHES_PER_SESSION = 10

# Module-level session counter — the MCP server owns this state.
_searches_this_session = 0


def _reset_session_counter() -> None:
    """Reset the search counter (call once per server restart)."""
    global _searches_this_session
    _searches_this_session = 0


async def list_tools(
    ctx: ServerRequestContext, params: PaginatedRequestParams | None = None
) -> ListToolsResult:
    return ListToolsResult(tools=[
        Tool(
            name="web_search",
            description=(
                "Search the web for current information. Use this tool for prices, weather, news, sports, "
                "stock prices, product specs, or any factual query requiring up-to-date data. "
                "Provide a concise query string. Returns results with title, URL, and content snippets."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query string. Keep it concise and specific.",
                    },
                    "num_results": {
                        "type": "integer",
                        "description": "Number of results to return (default 5).",
                        "default": 5,
                    },
                },
                "required": ["query"],
            },
        )
    ])


async def call_tool(
    ctx: ServerRequestContext, params: CallToolRequestParams
) -> CallToolResult:
    global _searches_this_session

    if params.name == "web_search":
        arguments = params.arguments or {}
        query = arguments.get("query", "")
        num_results = arguments.get("num_results", 5)

        if not query:
            return CallToolResult(
                content=[TextContent(type="text", text="Error: query is required")],
                is_error=True,
            )

        if _searches_this_session >= MAX_SEARCHES_PER_SESSION:
            return CallToolResult(
                content=[TextContent(type="text", text=f"Session limit reached ({MAX_SEARCHES_PER_SESSION})")],
                is_error=True,
            )

        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.get(
                    f"{SEARCH_BASE}/search",
                    params={
                        "q": query,
                        "format": "json",
                        "safesearch": 0,
                    },
                )
                resp.raise_for_status()
                data = resp.json()

            _searches_this_session += 1
            results = data.get("results", [])[:num_results]
            formatted_results = [
                {
                    "title": r.get("title", ""),
                    "url": r.get("url", ""),
                    "content": r.get("content", ""),
                }
                for r in results
            ]
            text = f"[search] '{query}' -> {len(formatted_results)} results (session total: {_searches_this_session})\n"
            text += str(formatted_results)
            return CallToolResult(content=[TextContent(type="text", text=text)])

        except httpx.HTTPError as e:
            return CallToolResult(
                content=[TextContent(type="text", text=f"Search failed: {e}")],
                is_error=True,
            )
        except Exception as e:
            return CallToolResult(
                content=[TextContent(type="text", text=f"Search error: {e}")],
                is_error=True,
            )

    raise ValueError(f"Unknown tool: {params.name}")
