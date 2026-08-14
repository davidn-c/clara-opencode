import asyncio

from mcp.server import Server
from mcp.server.stdio import stdio_server

from tools.basic import list_tools as basic_list_tools, call_tool as basic_call_tool
from tools.search import list_tools as search_list_tools, call_tool as search_call_tool, _reset_session_counter


async def _combined_list_tools(ctx, params=None):
    """Merge tool lists from all registered tool modules."""
    basic_result = await basic_list_tools(ctx, params)
    search_result = await search_list_tools(ctx, params)
    all_tools = list(basic_result.tools) + list(search_result.tools)
    from mcp.types import ListToolsResult
    return ListToolsResult(tools=all_tools)


async def _combined_call_tool(ctx, params):
    """Route tool calls to the appropriate handler."""
    if params.name == "get_clara_version":
        return await basic_call_tool(ctx, params)
    elif params.name == "web_search":
        return await search_call_tool(ctx, params)
    raise ValueError(f"Unknown tool: {params.name}")


def make_server():
    return Server(
        "clara_mcp",
        on_list_tools=_combined_list_tools,
        on_call_tool=_combined_call_tool,
    )


async def main():
    server = make_server()

    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    asyncio.run(main())
