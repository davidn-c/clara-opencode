#!/usr/bin/env python3
"""Minimal standalone MCP client harness for testing clara_mcp server.

Usage:
    python clara_mcp_harness.py

Keeps stdout clean (reserved for MCP protocol traffic).
Prints test results and diagnostics to stderr.
"""

import asyncio
import os
import sys
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

_SCRIPT_DIR = Path(os.path.dirname(os.path.abspath(__file__)))
_VENV_PYTHON = _SCRIPT_DIR.parent / "clara_venv" / "bin" / "python"
_SERVER_SCRIPT = _SCRIPT_DIR / "server.py"


async def run_harness():
    server_params = StdioServerParameters(
        command=str(_VENV_PYTHON),
        args=["-u", str(_SERVER_SCRIPT)],
        env={**os.environ, "PYTHONPATH": str(_SCRIPT_DIR)},
    )

    print("Harness: launching server...", file=sys.stderr)

    async with stdio_client(server_params, errlog=sys.stderr) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            print("Harness: initializing session...", file=sys.stderr)
            init_result = await session.initialize()
            print(
                f"Harness: initialized with server '{init_result.server_info.name}' "
                f"v{init_result.server_info.version}",
                file=sys.stderr,
            )

            # List tools
            print("Harness: calling tools/list...", file=sys.stderr)
            tools_result = await session.list_tools()
            tools = tools_result.tools
            print(f"Harness: found {len(tools)} tool(s):", file=sys.stderr)
            for tool in tools:
                print(f"  - {tool.name}: {tool.description}", file=sys.stderr)

            if not tools:
                print("FAIL: no tools found", file=sys.stderr)
                sys.exit(1)

            # Invoke first tool
            first_tool = tools[0]
            print(f"Harness: calling tool '{first_tool.name}'...", file=sys.stderr)
            call_result = await session.call_tool(first_tool.name)
            text_parts = [c.text for c in call_result.content if c.type == "text"]
            result_text = " ".join(text_parts) if text_parts else str(call_result)
            print(f"Harness: tool result: {result_text}", file=sys.stderr)

            # Validate
            if first_tool.name == "get_clara_version" and result_text == "Clara MCP Server v0.1.0":
                print("PASS: get_clara_version returned expected value", file=sys.stderr)
            else:
                print(
                    f"FAIL: unexpected result for {first_tool.name}: {result_text}",
                    file=sys.stderr,
                )
                sys.exit(1)

    print("Harness: server shut down cleanly", file=sys.stderr)
    print("PASS: all checks passed", file=sys.stderr)


def main():
    try:
        asyncio.run(run_harness())
    except Exception as e:
        print(f"Harness: unexpected error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
