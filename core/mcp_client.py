"""
core/mcp_client.py — Production MCP client for the Clara Desktop application.

Manages the MCP server subprocess lifecycle (spawn, monitor, shutdown,
auto-reconnect) and provides a clean async API for tool invocation with
timeout, retry for transport errors, and stderr redirection to GuiLogger.

Error taxonomy:
  TransportError  — broken pipe, connection refused, subprocess exit
                    -> retryable, triggers reconnect
  ProtocolError   — version mismatch, invalid JSON-RPC
                    -> fatal, requires restart
  LogicError      — tool not found, is_error=True in result
                    -> non-retryable, returned to caller
"""

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.shared.exceptions import MCPError as LibMCPError
from mcp.types import TextContent, Tool

from core.logger import gui_logger

# ── Path discovery ──────────────────────────────────────────────

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_MCP_SERVER_DIR = _PROJECT_ROOT / "Clara_MCP_Server"
_MCP_SERVER_SCRIPT = _MCP_SERVER_DIR / "server.py"
_VENV_PYTHON = _PROJECT_ROOT / "clara_venv" / "bin" / "python"

# ── Constants ───────────────────────────────────────────────────

DEFAULT_TIMEOUT = 15.0          # seconds per tool call
MAX_RETRIES = 3                 # transport error reconnect attempts
RETRY_BACKOFF = 1.0             # base seconds for exponential backoff
CONN_TIMEOUT = 10.0             # seconds for initial connection attempt


class McpError(Exception):
    """Base exception for MCP client errors."""
    pass


class TransportError(McpError):
    """Transport-level failure (connection/subprocess). Retryable."""
    pass


class ProtocolError(McpError):
    """Protocol-level failure (version mismatch, invalid response). Fatal."""
    pass


class LogicError(McpError):
    """Business-logic failure (tool not found, tool returned error). Non-retryable."""
    pass


class McpClient:
    """Async MCP client that manages the server subprocess lifecycle.

    Usage:
        client = McpClient()
        await client.connect()
        try:
            tools = await client.list_tools()
            result = await client.call_tool("get_clara_version")
        finally:
            await client.disconnect()
    """

    def __init__(
        self,
        server_script: Path | None = None,
        venv_python: Path | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = MAX_RETRIES,
    ):
        self._server_script = server_script or _MCP_SERVER_SCRIPT
        self._venv_python = venv_python or _VENV_PYTHON
        self._timeout = timeout
        self._max_retries = max_retries

        # Session state
        self._session: ClientSession | None = None
        self._stdio_cm = None
        self._streams = None
        self._connected = False
        self._semaphore = asyncio.Semaphore(1)  # serialize tool calls on stdio
        self._loop: asyncio.AbstractEventLoop | None = None  # set by backend for sync calls

    # ── Lifecycle ──────────────────────────────────────────────

    async def connect(self) -> None:
        """Spawn the MCP server and establish the stdio session.

        Raises TransportError on connection failure, ProtocolError on
        handshake failure.
        """
        if self._connected:
            return

        for attempt in range(1, self._max_retries + 1):
            try:
                await self._do_connect()
                gui_logger.log("[mcp] Connected to server")
                return
            except ProtocolError:
                # Don't retry protocol errors — they won't fix themselves
                raise
            except Exception as e:
                gui_logger.log(
                    f"[mcp] Connection attempt {attempt}/{self._max_retries} failed: {e}"
                )
                await self._cleanup()
                if attempt < self._max_retries:
                    backoff = RETRY_BACKOFF * (2 ** (attempt - 1))
                    await asyncio.sleep(backoff)

        raise TransportError(
            f"Failed to connect after {self._max_retries} attempts"
        )

    async def _do_connect(self) -> None:
        """Perform a single connection attempt."""
        server_params = StdioServerParameters(
            command=str(self._venv_python),
            args=["-u", str(self._server_script)],
            env={**os.environ, "PYTHONPATH": str(_MCP_SERVER_DIR)},
        )

        self._stdio_cm = stdio_client(server_params, errlog=sys.stderr)
        self._streams = await self._stdio_cm.__aenter__()
        read_stream, write_stream = self._streams

        self._session = ClientSession(read_stream, write_stream)
        await self._session.__aenter__()

        # Handshake
        try:
            init_result = await asyncio.wait_for(
                self._session.initialize(),
                timeout=CONN_TIMEOUT,
            )
        except asyncio.TimeoutError:
            await self._cleanup()
            raise TransportError("Connection timed out")
        except Exception as e:
            await self._cleanup()
            raise ProtocolError(f"Handshake failed: {e}")

        # Protocol version check — accept 2024.x and 2025.x
        version = init_result.protocol_version
        if not (version.startswith("2024-") or version.startswith("2025-")):
            await self._cleanup()
            raise ProtocolError(
                f"Unsupported protocol version: {version}"
            )

        self._connected = True

    async def disconnect(self) -> None:
        """Gracefully shut down the session and terminate the subprocess."""
        if not self._connected:
            return
        await self._cleanup()

    async def _cleanup(self) -> None:
        """Internal: tear down session and streams."""
        self._connected = False

        if self._session is not None:
            try:
                await self._session.__aexit__(None, None, None)
            except Exception:
                pass
            self._session = None

        if self._streams is not None:
            try:
                await self._stdio_cm.__aexit__(None, None, None)
            except Exception:
                pass
            self._streams = None
            self._stdio_cm = None

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *exc):
        await self.disconnect()

    # ── Public API ─────────────────────────────────────────────

    async def list_tools(self) -> list[Tool]:
        """Return the list of available tools from the server."""
        if not self._connected:
            raise TransportError("Not connected")
        async with self._semaphore:
            try:
                result = await self._session.list_tools()
                return list(result.tools)
            except LibMCPError as e:
                raise LogicError(str(e))
            except Exception as e:
                raise TransportError(f"list_tools failed: {e}")

    async def list_tools_openai(self) -> list[dict]:
        """Return tools in OpenAI function-calling format.

        Each tool becomes:
        {
            "type": "function",
            "function": {
                "name": str,
                "description": str,
                "parameters": dict  # JSON Schema object
            }
        }
        """
        if not self._connected:
            raise TransportError("Not connected")
        async with self._semaphore:
            try:
                result = await self._session.list_tools()
                tools = []
                for tool in result.tools:
                    tools.append({
                        "type": "function",
                        "function": {
                            "name": tool.name,
                            "description": tool.description or "",
                            "parameters": tool.input_schema if isinstance(tool.input_schema, dict) else {},
                        }
                    })
                return tools
            except LibMCPError as e:
                raise LogicError(str(e))
            except Exception as e:
                raise TransportError(f"list_tools_openai failed: {e}")

    async def call_tool(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> str:
        """Invoke a tool by name and return its text result.

        Raises:
            TransportError: connection lost (retryable via reconnect)
            ProtocolError: protocol violation
            LogicError: tool not found or tool returned an error
        """
        if not self._connected:
            raise TransportError("Not connected")

        arguments = arguments or {}

        async with self._semaphore:
            try:
                result = await asyncio.wait_for(
                    self._session.call_tool(name, arguments=arguments),
                    timeout=self._timeout,
                )
            except asyncio.TimeoutError:
                raise TransportError(
                    f"Tool '{name}' call timed out after {self._timeout}s"
                )
            except LibMCPError as e:
                # MCP error codes: -32601 = method not found (tool),
                # -32603 = internal error
                if "not found" in str(e).lower():
                    raise LogicError(f"Tool not found: {name}")
                raise LogicError(f"Tool error: {e}")
            except Exception as e:
                # Any other exception during transport is a transport error
                raise TransportError(f"Tool call failed: {e}")

        # Extract text from result
        text_parts = []
        for content in result.content:
            if isinstance(content, TextContent):
                text_parts.append(content.text)
            elif hasattr(content, "text"):
                text_parts.append(str(content.text))

        if result.is_error and not text_parts:
            raise LogicError(f"Tool returned error: {result}")

        return " ".join(text_parts) if text_parts else str(result)

    async def call_tool_by_id(
        self, tool_name: str, tool_arguments: dict[str, Any] | None = None
    ) -> dict:
        """Execute a tool and return structured result.

        Returns:
            {
                "name": str,
                "content": str,  # text content
                "is_error": bool
            }
        """
        if not self._connected:
            raise TransportError("Not connected")

        tool_arguments = tool_arguments or {}

        async with self._semaphore:
            try:
                result = await asyncio.wait_for(
                    self._session.call_tool(tool_name, arguments=tool_arguments),
                    timeout=self._timeout,
                )
            except asyncio.TimeoutError:
                raise TransportError(
                    f"Tool '{tool_name}' call timed out after {self._timeout}s"
                )
            except LibMCPError as e:
                if "not found" in str(e).lower():
                    raise LogicError(f"Tool not found: {tool_name}")
                raise LogicError(f"Tool error: {e}")
            except Exception as e:
                raise TransportError(f"Tool call failed: {e}")

        # Extract text content
        text_parts = []
        for content in result.content:
            if isinstance(content, TextContent):
                text_parts.append(content.text)
            elif hasattr(content, "text"):
                text_parts.append(str(content.text))

        return {
            "name": tool_name,
            "content": " ".join(text_parts) if text_parts else str(result),
            "is_error": result.is_error,
        }

    @property
    def is_connected(self) -> bool:
        return self._connected

    def call_tool_sync(self, name: str, arguments: dict[str, Any] | None = None) -> str:
        """Synchronous wrapper for call_tool.

        Uses asyncio.run_coroutine_threadsafe to bridge the sync/async
        gap. Must be called from the same thread that owns the event loop
        referenced by self._loop.
        """
        if not self._connected:
            gui_logger.log(f"[mcp] Not connected, tool call skipped: {name}")
            return ""
        if self._loop is None:
            gui_logger.log("[mcp] No event loop set, tool call skipped: {name}")
            return ""

        arguments = arguments or {}
        future = asyncio.run_coroutine_threadsafe(
            self.call_tool(name, arguments),
            self._loop,
        )
        try:
            return future.result(timeout=30.0)
        except asyncio.TimeoutError:
            gui_logger.log(f"[mcp] Tool '{name}' timed out after 30s")
            return ""
        except Exception as e:
            gui_logger.log(f"[mcp] Tool '{name}' failed: {e}")
            return ""

    def handle_crash(self) -> None:
        """Called when the subprocess exits unexpectedly.

        Resets internal state so the caller can retry connect().
        """
        gui_logger.log("[mcp] Server crashed — state reset for reconnect")
        self._connected = False
        self._session = None
        self._streams = None
        self._stdio_cm = None
