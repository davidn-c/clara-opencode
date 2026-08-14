#!/usr/bin/env python3
"""QA verification tool for Phase 2 MCP integration.

Tests the mcp_client.py module and its integration with ClaraBackend.
Prints results to stdout. Errors go to stderr.

Usage:
    cd /home/dave/Clara_OpenCode
    ./clara_venv/bin/python mcp_qa_test.py
"""

import asyncio
import os
import sys
import time
from pathlib import Path

# Ensure the project root is on the path
_PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_PROJECT_ROOT))

from core.mcp_client import McpClient, TransportError, ProtocolError, LogicError


# ── Test helpers ──────────────────────────────────────────────

_tests_run = 0
_tests_passed = 0
_tests_failed = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global _tests_run, _tests_passed, _tests_failed
    _tests_run += 1
    if condition:
        _tests_passed += 1
        print(f"  PASS: {name}")
    else:
        _tests_failed += 1
        msg = f"  FAIL: {name}"
        if detail:
            msg += f" — {detail}"
        print(msg, file=sys.stderr)


# ── Test 1: Direct McpClient usage ───────────────────────────

async def test_direct_client() -> None:
    """Test McpClient without backend integration."""
    print("\n[Test 1] Direct McpClient usage")

    client = McpClient()
    check("is_connected starts False", not client.is_connected)

    try:
        await client.connect()
        check("connect succeeds", True)
    except Exception as e:
        check("connect succeeds", False, str(e))
        return

    try:
        # List tools
        tools = await client.list_tools()
        check("list_tools returns tools", len(tools) >= 1)
        check("get_clara_version in tools", any(t.name == "get_clara_version" for t in tools))

        # Call tool
        result = await client.call_tool("get_clara_version")
        check("call_tool returns version string", result == "Clara MCP Server v0.1.0", result)
        check("is_connected after calls", client.is_connected)

        # Unknown tool should raise LogicError
        try:
            await client.call_tool("nonexistent_tool")
            check("unknown tool raises LogicError", False, "no exception raised")
        except LogicError:
            check("unknown tool raises LogicError", True)
        except Exception as e:
            check("unknown tool raises LogicError", False, f"got {type(e).__name__}: {e}")

    finally:
        await client.disconnect()
        check("disconnect succeeds", not client.is_connected)


# ── Test 2: Async context manager ────────────────────────────

async def test_context_manager() -> None:
    """Test McpClient as async context manager."""
    print("\n[Test 2] Async context manager")

    async with McpClient() as client:
        check("connected inside context", client.is_connected)
        result = await client.call_tool("get_clara_version")
        check("call_tool inside context", result == "Clara MCP Server v0.1.0")

    check("disconnected outside context", not client.is_connected)


# ── Test 3: ClaraBackend integration ─────────────────────────

def test_backend_integration() -> None:
    """Test MCP client integrated into ClaraBackend."""
    print("\n[Test 3] ClaraBackend integration")

    from core.backend import ClaraBackend

    backend = ClaraBackend()
    backend.start()

    # Wait for MCP connection
    time.sleep(2.5)

    # Test tool call
    result = backend.mcp_call_tool("get_clara_version")
    check("backend mcp_call_tool returns version", result == "Clara MCP Server v0.1.0", result)

    # Test unknown tool returns empty string
    result = backend.mcp_call_tool("nonexistent_tool")
    check("unknown tool returns empty string", result == "")

    # Test call with no arguments
    result = backend.mcp_call_tool("get_clara_version", {})
    check("call_tool with empty args", result == "Clara MCP Server v0.1.0")

    backend.shutdown()
    check("shutdown completes", True)


# ── Test 4: Error handling ───────────────────────────────────

async def test_error_handling() -> None:
    """Test error handling and exception types."""
    print("\n[Test 4] Error handling")

    client = McpClient()

    # Not connected should raise TransportError
    try:
        await client.call_tool("get_clara_version")
        check("not connected raises TransportError", False, "no exception")
    except TransportError:
        check("not connected raises TransportError", True)

    await client.connect()
    await client.disconnect()

    # After disconnect, should raise TransportError
    try:
        await client.call_tool("get_clara_version")
        check("after disconnect raises TransportError", False, "no exception")
    except TransportError:
        check("after disconnect raises TransportError", True)


# ── Test 5: Multiple connections ─────────────────────────────

async def test_multiple_clients() -> None:
    """Test that multiple McpClient instances work independently."""
    print("\n[Test 5] Multiple clients")

    async with McpClient() as client1:
        result1 = await client1.call_tool("get_clara_version")
        check("client1 result", result1 == "Clara MCP Server v0.1.0")

    async with McpClient() as client2:
        result2 = await client2.call_tool("get_clara_version")
        check("client2 result", result2 == "Clara MCP Server v0.1.0")

    check("sequential clients work", True)


# ── Main ─────────────────────────────────────────────────────

async def main_async() -> None:
    await test_direct_client()
    await test_context_manager()
    await test_error_handling()
    await test_multiple_clients()


def main() -> None:
    print("=" * 60)
    print("Phase 2 MCP Integration — QA Verification")
    print("=" * 60)

    # Async tests
    asyncio.run(main_async())

    # Sync backend test
    test_backend_integration()

    # Summary
    print("\n" + "=" * 60)
    print(f"Results: {_tests_passed}/{_tests_run} passed, {_tests_failed} failed")
    print("=" * 60)

    if _tests_failed:
        print("\nSome tests FAILED. See details above.")
        sys.exit(1)
    else:
        print("\nAll tests PASSED.")
        sys.exit(0)


if __name__ == "__main__":
    main()
