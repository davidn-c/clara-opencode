"""
core/llm_client.py — LLM client functions for Gemma/Qwen inference.

Extracted from clara2.py to decouple LLM communication from the
monolithic script. Provides llama_chat(), llama_analyze(), warmup_llm(),
and helper functions for reasoning-stripping and word-collapse.
"""

import re
import httpx

from core.services import GEMMA_BASE_URL, GEMMA_API_KEY, CLARA_TEMPERATURE

# Shared httpx client for all LLM requests
llm_client = httpx.Client(timeout=120.0)


def _collapse_repeated_words(text: str) -> str:
    """Collapses immediate word-repetition artifacts ("their their own" ->
    "their own"). Small local models occasionally stutter a token like this;
    this is a text-level safety net independent of whatever's causing it
    upstream. Case-insensitive so "The The" also collapses; keeps the first
    occurrence's casing."""
    return re.sub(r'\b(\w+)(?:\s+\1\b)+', r'\1', text, flags=re.IGNORECASE)


def _strip_reasoning(message: dict) -> str:
    """Strip reasoning content from an LLM message response.

    Gemma 4 wraps its reasoning in <|channel>thought ... <channel|>, not
    <think>...</think> — and per Google's own docs the 26B-A4B variant
    keeps emitting that channel structure even when thinking is disabled.
    Some llama.cpp builds instead split reasoning into a separate
    reasoning_content field on the message rather than inlining it in
    content at all. This checks all three shapes so a leaked reasoning
    pass never reaches speak() and gets read aloud.
    """
    content = message.get("content") or ""

    if message.get("reasoning_content"):
        print(f"[thinking] reasoning_content field present ({len(message['reasoning_content'])} chars) — dropped")

    before = content
    content = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL)
    content = re.sub(r'<\|channel>thought.*?<channel\|>', '', content, flags=re.DOTALL)
    if content != before:
        print(f"[thinking] inline reasoning tags found and stripped ({len(before) - len(content)} chars removed)")

    return content.strip()


def llama_analyze(prompt: str) -> str:
    """Send a prompt to the LLM for JSON-only analysis (importance/fact extraction)."""
    resp = llm_client.post(
        f"{GEMMA_BASE_URL}/chat/completions",
        headers={"Authorization": f"Bearer {GEMMA_API_KEY}"},
        json={
            "model": "qwen3.6",
            "messages": [
                {"role": "system", "content": "You are a JSON-only response bot. You must respond with valid JSON and nothing else. No markdown, no explanation, no preamble."},
                {"role": "user", "content": prompt}
            ],
            "max_tokens": 4096,
            "temperature": 0.1,
            "chat_template_kwargs": {"enable_thinking": False}
        }
    )
    resp.raise_for_status()
    message = resp.json()["choices"][0]["message"]
    content = _strip_reasoning(message)
    return content


def llama_chat(messages: list, tools: list[dict] | None = None) -> tuple[str, list[dict] | None]:
    """Send a conversation to the LLM and return the assistant's reply.

    Returns:
        (content, tool_calls) where tool_calls is a list of dicts like:
        [{"id": str, "name": str, "arguments": dict}, ...]
        or None if the model responded with text.
    """
    payload = {
        "model": "qwen3.6",
        "messages": messages,
        "max_tokens": 4096,
        "temperature": CLARA_TEMPERATURE,
        "frequency_penalty": 0.2,
        "chat_template_kwargs": {"enable_thinking": False}
    }
    if tools:
        payload["tools"] = tools

    resp = llm_client.post(
        f"{GEMMA_BASE_URL}/chat/completions",
        headers={"Authorization": f"Bearer {GEMMA_API_KEY}"},
        json=payload
    )
    resp.raise_for_status()
    message = resp.json()["choices"][0]["message"]

    # Handle tool_calls if present
    tool_calls = message.get("tool_calls")
    content = _strip_reasoning(message)
    content = _collapse_repeated_words(content)
    if tool_calls:
        parsed_tool_calls = []
        for tc in tool_calls:
            func = tc.get("function", {})
            parsed_tool_calls.append({
                "id": tc.get("id", ""),
                "name": func.get("name", ""),
                "arguments": func.get("arguments", "{}"),
            })
        return content, parsed_tool_calls

    return content, None


def warmup_llm(conversation_history: list):
    """Fire a throwaway completion to prime the LLM's KV cache.

    llama-server caches KV per prefix match across requests, so paying
    the cold prefill cost here — during startup — means the user's
    actual first message only has to process the small delta (their new
    turn) instead of the whole system prompt from scratch.
    """
    try:
        import time
        t0 = time.time()
        llm_client.post(
            f"{GEMMA_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {GEMMA_API_KEY}"},
            json={
                "model": "qwen3.6",
                "messages": conversation_history,
                "max_tokens": 1,
                "temperature": CLARA_TEMPERATURE
            }
        )
        print(f"[warmup] LLM primed in {time.time()-t0:.2f}s")
    except Exception as e:
        print(f"[warmup] LLM warmup failed (non-fatal, first real message will just be slower): {e}")