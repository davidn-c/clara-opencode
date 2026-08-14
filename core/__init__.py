"""
core/__init__.py — Re-export everything that was previously in clara2.py
so that existing import paths continue to work.

All functionality has been extracted into sibling modules; this package
initial simply re-exports the public API so that core.backend and any
other code that ``import clara2`` still works without changes.
"""

# ── Config constants (was clara2 top-level) ──────────────────
from core.services import (
    TTS_HOST, TTS_PORT,
    MEMORY_BASE, GEMMA_BASE_URL, GEMMA_API_KEY,
    CLARA_TEMPERATURE, USER_ID, INSTANCE,
    MAX_SEARCHES_PER_SESSION,
    PIN_PHRASES,
)

# ── LLM client ──────────────────────────────────────────────
from core.llm_client import (
    llm_client,
    llama_analyze,
    llama_chat,
    _strip_reasoning,
    _collapse_repeated_words,
)

# ── MCP client ──────────────────────────────────────────────
from core.mcp_client import McpClient

# ── Memory client ───────────────────────────────────────────
from core.memory_client import MemoryClient

# ── Prompt builder ──────────────────────────────────────────
from core.prompt_builder import build_system_prompt

# ── Analyzer ────────────────────────────────────────────────
from core.analyzer import analyze_exchange as analyze_exchange

# ── Memory recall ───────────────────────────────────────────
from core.memory_recall import recall_memory

# ── TTS ─────────────────────────────────────────────────────
from core.tts import strip_markdown
from core.chat_engine import speak

# ── Chat engine ─────────────────────────────────────────────
from core.chat_engine import (
    process_message,
    chat_with_search,
    write_episode_async,
    warmup_tts,
    warmup_llm_wrapper as warmup_llm,
)

__all__ = [
    # Config
    "TTS_HOST", "TTS_PORT", "MEMORY_BASE",
    "GEMMA_BASE_URL", "GEMMA_API_KEY", "CLARA_TEMPERATURE",
    "USER_ID", "INSTANCE", "MAX_SEARCHES_PER_SESSION", "PIN_PHRASES",
    # LLM
    "llm_client", "llama_analyze", "llama_chat",
    "_strip_reasoning", "_collapse_repeated_words",
    # MCP
    "McpClient",
    # Memory
    "MemoryClient",
    # Prompt
    "build_system_prompt",
    # Analyzer
    "analyze_exchange",
    # Recall
    "recall_memory",
    # TTS
    "speak", "strip_markdown",
    # Chat
    "process_message", "chat_with_search", "write_episode_async",
    "warmup_tts", "warmup_llm",
]
