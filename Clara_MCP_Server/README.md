# Clara MCP Server

A Model Context Protocol (MCP) implementation for the Clara Desktop ecosystem. This project provides a standardized way for the Clara backend to interact with external tools and services via the MCP protocol.

## Current Status: Phase 3 Complete
The migration of the first core capability (Web Search) from the monolithic backend to the MCP server has been successfully completed and verified.

## Architecture

### Server (`server.py`)
A minimal MCP server providing tools via `stdio` transport. It is designed to be extensible by adding new tool definitions to the `tools/` directory.

### Client (`core/mcp_client.py`)
A production-grade asynchronous client integrated into `ClaraBackend`.
- **Lifecycle**: Manages the MCP server subprocess, including auto-reconnect on crash and graceful shutdown. The shutdown is triggered by closing the `stdio` transport, which signals the subprocess to exit.
- **Communication**: Uses `asyncio` and wraps the MCP `stdio_client` to provide non-blocking tool calls and `stderr` redirection to the `GuiLogger`.
- **Resilience**: Implements a semaphore for backpressure and distinguishes between Transport, Protocol, and Logic errors to drive appropriate retry behaviors.
- **Threading**: Supports synchronous access via `call_tool_sync()`, which uses an injected event loop reference to bridge the async protocol with the synchronous `ClaraBackend` and `ChatEngine`.

### Process Management (ClaraBackend Shutdown)
The `ClaraBackend` manages multiple external processes with specific termination strategies:
- **MCP Server**: Graceful exit via `stdio` transport closure.
- **ComfyUI**: Managed via process groups (`os.killpg()`) with a 5-second `SIGTERM` to `SIGKILL` escalation.
- **llama-server**: Terminated via `pkill -f`.

## Directory Structure
```text
Clara_MCP_Server/
├── server.py              # MCP Server entry point
├── tools/                 # Tool definitions
│   ├── __init__.py
│   └── search.py          # Migrated Web Search tool
├── clara_mcp_harness.py   # Standalone MCP test client (for development)
├── PHASE_2_PLAN.md        # Documentation of the Phase 2 implementation
├── PHASE_3_PLAN.md        # Documentation of the Search Migration plan
└── README.md              # This file
```

## Testing
Tests are run using a dedicated test suite that validates:
- Direct `McpClient` connectivity (connect, list, call, disconnect).
- Async context manager lifecycle.
- `ClaraBackend` integration.
- Error handling for disconnected states.
- Independent client isolation.
- Successful tool execution (e.g., `get_clara_version` and `web_search`).

To run tests, use the project's test command (e.g., `pytest` or the specific test script provided in the environment).
