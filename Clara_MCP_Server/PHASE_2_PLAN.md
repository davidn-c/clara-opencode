# Phase 2: Production MCP Client Harness Development Plan

## Objective
Transition from the standalone `clara_mcp_harness.py` to a production-grade MCP client integration within the main Clara Desktop application. This phase focuses on building a robust, asynchronous client capable of managing the MCP server lifecycle and providing a stable interface for the `ClaraBackend`.

## Goals
1.  **Lifecycle Management**: Implement a mechanism to start, monitor, and gracefully shut down the MCP server process as part of the Clara application lifecycle, including a force-kill fallback for unresponsive processes and an auto-reconnect strategy for unexpected crashes.
2.  **Asynchronous Communication**: Ensure the client uses non-blocking I/O to prevent the GUI from freezing. Use a semaphore to manage backpressure and queue tool requests, acknowledging that `stdio` transport is inherently serialized.
3.  **Error Resilience**: Implement robust error handling by distinguishing between:
    - **Transport Errors**: (e.g., broken pipe, connection refused) -> Trigger retries/reconnects.
    - **Protocol Errors**: (e.g., version mismatch, invalid JSON-RPC) -> Fatal/Requires restart.
    - **Logic Errors**: (e.g., tool not found, `is_error=True` in result) -> Non-retryable, return to caller.
4.  **Standardized Interface**: Create a clean internal API that abstracts the MCP protocol details away from the rest of the Clara backend.
5.  **Observability**: Wrap the MCP library's `stdio_client` to capture and redirect the subprocess `stderr` to the `GuiLogger` for visibility in the Clara GUI.

## Proposed Architecture (Internal to Clara)

### 1. `core/mcp_client.py` (The Manager)
This will be the primary entry point for the rest of the application.
- **Responsibility**: Manages the `subprocess` for the MCP server.
- **Capabilities**: 
    - `connect()`: Spawns the server (using discoverable paths for `server.py` and `venv`) and establishes the `stdio` session.
    - `disconnect()`: Ensures the subprocess is terminated cleanly.
    - `call_tool(name, arguments)`: A high-level async method to invoke tools.
    - `list_tools()`: Retrieves available capabilities.
    - `handle_crash()`: Implements the auto-reconnect logic.

### 2. `core/backend.py` (The Integration)
- **Responsibility**: Integrates the `mcp_client` into the existing `ClaraBackend`.
- **Logic**: Decides *when* to call the MCP server based on incoming user prompts or system needs.

## Implementation Roadmap

### Step 1: The Connection Engine
- Implement the `stdio` transport by wrapping the MCP library's `stdio_client` to allow for `stderr` redirection.
- Ensure the subprocess environment and paths (server/venv) are discoverable or configurable.
- Implement the MCP handshake and session initialization, including a basic protocol version check.
- **Validation**: Ensure the client can successfully perform a `list_tools` call and receive the `get_clara_version` response.

### Step 2: Robust Tool Invocation
- Implement a standardized way to pass arguments to tools.
- Implement a timeout mechanism for every tool call to prevent the backend from hanging on a stalled MCP server.
- Implement retry logic specifically for **Transport Errors** (e.g., connection refused).

### Step 3: Lifecycle Integration
- Integrate the connection logic into `ClaraBackend` initialization.
- Ensure that when `ClaraGUI` closes, the MCP server process is explicitly killed, with a force-kill fallback for unresponsive processes.
- Implement an auto-reconnect strategy for unexpected server crashes.

## Success Criteria
- The MCP server starts automatically when Clara launches.
- `ClaraBackend` can successfully retrieve the version via the MCP client without blocking the main thread.
- The system handles a "killed" MCP server gracefully (e.g., logging the error and allowing the rest of the app to function).
- No "zombie" MCP processes remain after the application exits.
- **Testing**: The `clara_mcp_harness.py` is used as a test fixture to validate the `mcp_client` implementation before full integration.
