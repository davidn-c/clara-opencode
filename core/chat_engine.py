"""
core/chat_engine.py — process_message(): the full conversation pipeline.

Extracted from clara2.py to decouple the chat orchestration logic from the
monolithic script.  This is the most complex module — it handles:
  - Pre-emptive memory recall
  - LLM chat turns with OpenAI-style tool calling
  - RECALL: / CORRECT: tool-token extraction & execution
  - Conversation-history trimming
  - Stall-phrase detection & forced recall
  - Fact correction with deactivation of old facts

All external dependencies (LLM, TTS, search, memory, prompt building,
analysis) are injected via parameters or imported from sibling modules so
this file stays focused on *flow control* only.
"""

import asyncio
import json
import re
import select
import time
import threading
import subprocess
import concurrent.futures

import httpx

from core.services import TTS_HOST, TTS_PORT
from core.llm_client import llm_client, llama_chat, _strip_reasoning, _collapse_repeated_words, warmup_llm
from core.memory_client import MemoryClient
from core.mcp_client import McpClient
from core.memory_recall import recall_memory
from core.tts import strip_markdown as _strip_markdown_tts
from core.analyzer import analyze_exchange


# ── Tool-token helpers ──────────────────────────────────────
MAX_TOOL_HOPS = 2  # hard cap so a misbehaving model can't loop forever
MAX_TOOL_CALL_LOOPS = 3  # max LLM tool-call iterations



def _extract_tool_line(text: str, token: str) -> str | None:
    """Extract the argument after *token* if a line actually STARTS with it
    (after stripping leading whitespace / markdown bullets)."""
    for line in text.strip().split("\n"):
        stripped = line.strip().lstrip("-*  ").strip()
        if stripped.startswith(token):
            return stripped[len(token):].strip()
    return None


def _has_any_tool_token(reply: str) -> bool:
    """True when *reply* contains at least one recognised tool token at the
    start of a line."""
    return any(
        any(line.strip().lstrip("-*  ").strip().startswith(tok) for line in reply.strip().split("\n"))
        for tok in ("RECALL:", "CORRECT:")
    )


def _strip_tool_tokens(reply: str, token: str) -> str:
    """Remove every line that starts with *token* and return the remainder."""
    return "\n".join(
        line for line in reply.strip().split("\n")
        if not line.strip().lstrip("-*  ").strip().startswith(token)
    ).strip()


# ── Markdown / TTS helpers ──────────────────────────────────
# Re-export strip_markdown so chat_engine's public API includes it.
strip_markdown = _strip_markdown_tts


def get_semantic_chunks(text: str, max_chars: int = 400) -> list[str]:
    """Split *text* into semantic chunks respecting linguistic boundaries.

    ``strip_markdown`` already collapses newlines to spaces, so the
    structural split happens in two stages:
      Stage 1: split on terminal punctuation (``.``, ``!``, `?``).
      Stage 2 (size constraint): if a chunk exceeds *max_chars*, split
      it at the nearest structural boundary going backward.  Never
      split mid-word — fall back to the last whitespace before the
      limit.
    """
    # ── Stage 1: split on terminal punctuation ────────────────
    chunks: list[str] = [
        s.strip() for s in re.split(r'(?<=[.!?])\s+', text) if s.strip()
    ]

    if not chunks:
        return []

    # ── Stage 2: size constraint ──────────────────────────────
    result: list[str] = []
    for chunk in chunks:
        if len(chunk) <= max_chars:
            result.append(chunk)
            continue
        # Oversized — split at the nearest boundary going backward
        while len(chunk) > max_chars:
            # Look backward from max_chars for a split point
            window = chunk[:max_chars]
            split_at = None

            # Priority 1: semicolon
            idx = window.rfind(';')
            if idx != -1:
                split_at = idx + 1
            # Priority 2: terminal punctuation
            if split_at is None:
                for punct in '.!?':
                    idx = window.rfind(punct)
                    if idx != -1:
                        split_at = idx + 1
                        break
            # Priority 3: comma
            if split_at is None:
                idx = window.rfind(',')
                if idx != -1:
                    split_at = idx + 1
            # Priority 4: last whitespace before limit (never mid-word)
            if split_at is None:
                idx = window.rfind(' ')
                if idx != -1:
                    split_at = idx + 1

            if split_at is None:
                # Absolute last resort — split at the limit anyway
                split_at = max_chars

            # Safety: ensure progress (never 0, never infinite loop)
            if split_at == 0:
                split_at = max_chars

            result.append(chunk[:split_at].strip())
            chunk = chunk[split_at:]

        if chunk.strip():
            result.append(chunk.strip())

    return result


def speak(text: str, interrupt_event=None):
    """Synthesize and speak *text* (with markdown stripping)."""
    text = strip_markdown(text)
    if not text or not text.strip():
        return

    if interrupt_event is None:
        interrupt_event = threading.Event()

    sentences = [s.strip() for s in re.split(r'(?<=[.!?])\s+', text) if s.strip()]
    clean_sentences = [s for s in sentences if len(s.strip()) >= 2]
    if not clean_sentences:
        return

    clean_sentences = get_semantic_chunks(text)

    player_proc = None
    player_rate = None

    def ensure_player(rate: int):
        nonlocal player_proc, player_rate
        if player_proc is not None and player_rate == rate and player_proc.poll() is not None:
            player_proc = None
        if player_proc is not None and player_rate == rate:
            return player_proc
        if player_proc is not None:
            try:
                player_proc.stdin.close()
            except Exception:
                pass
            try:
                player_proc.wait(timeout=1.0)
            except Exception:
                pass
        player_proc = subprocess.Popen(
            ["pw-play", "--format=s16", f"--rate={rate}", "--channels=1", "-"],
            stdin=subprocess.PIPE,
        )
        player_rate = rate
        return player_proc

    def make_tts_request(sentence):
        resp = httpx.post(
            f"http://{TTS_HOST}:{TTS_PORT}/tts",
            json={"text": sentence}, timeout=30.0
        )
        resp.raise_for_status()
        return resp.content, int(resp.headers.get("X-Sample-Rate", "24000"))

    # ── Pipe-drain safety cap ─────────────────────────────────
    MAX_PIPE_DRAIN_RETRIES = 50  # 50 × 10ms = 500ms max wait

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(make_tts_request, clean_sentences[0])

        for i, sentence in enumerate(clean_sentences):
            if interrupt_event.is_set():
                print("[tts] interrupted, killing playback and clearing queue")
                if player_proc is not None:
                    try:
                        player_proc.kill()
                    except Exception:
                        pass
                    try:
                        player_proc.wait(timeout=2.0)
                    except Exception:
                        pass
                    player_proc = None
                break

            print(f"[tts] chunk {i} ({len(sentence)} chars): {sentence!r}")

            try:
                audio_bytes, rate = future.result()

                if i < len(clean_sentences) - 1:
                    future = executor.submit(make_tts_request, clean_sentences[i + 1])

                proc = ensure_player(rate)

                try:
                    proc.stdin.write(audio_bytes)
                    proc.stdin.flush()
                except (BrokenPipeError, OSError):
                    print(f"[tts] pipe broken during sentence {i}, stopping playback")
                    if player_proc is not None:
                        try:
                            player_proc.kill()
                        except Exception:
                            pass
                        try:
                            player_proc.wait(timeout=2.0)
                        except Exception:
                            pass
                        player_proc = None
                    break

                # ── Cooperative yield to the consumer ───────────
                # POSIX pipes block writes when the buffer is full,
                # so write() + flush() already provides backpressure.
                # A tiny yield (10ms) after each write gives the
                # consumer a chance to drain before the next chunk.
                # This is not a fixed delay — it is a cooperative
                # yield that only adds latency when the consumer is
                # slow.  Safety cap prevents infinite loop if pw-play
                # hangs or becomes a zombie.
                drain_retries = 0
                while drain_retries < MAX_PIPE_DRAIN_RETRIES:
                    try:
                        # Check if proc.stdin is ready for WRITING
                        # (i.e., the pipe buffer has space).
                        # select.select([], [], [fd]) checks writability.
                        _, _, errored = select.select([], [proc.stdin], [], 0.01)
                        if not errored:
                            break  # pipe can accept more data
                        drain_retries += 1
                        time.sleep(0.01)
                    except Exception:
                        # If we can't probe, skip — write() blocks
                        # anyway when the pipe is full (POSIX semantics)
                        break
                if drain_retries >= MAX_PIPE_DRAIN_RETRIES:
                    print("[tts] WARNING: pipe buffer not draining after 500ms, proceeding anyway")

            except Exception as e:
                print(f"\n[Queue Error] Failed streaming sentence {i}: {e}")
                if player_proc is not None and player_proc.poll() is not None:
                    player_proc = None
                break

        # ── Graceful shutdown of the player ────────────────────
        if player_proc is not None:
            try:
                player_proc.stdin.close()
            except Exception:
                pass
            try:
                player_proc.wait(timeout=5.0)
            except Exception:
                # Player didn't exit cleanly — force kill
                try:
                    player_proc.kill()
                except Exception:
                    pass
                try:
                    player_proc.wait(timeout=2.0)
                except Exception:
                    pass
                player_proc = None


# ── Correction hop ──────────────────────────────────────────
def _apply_correction(
    reply: str,
    conversation_history: list,
    memory: MemoryClient,
) -> tuple[str, bool, str | None]:
    """Process a single CORRECT: hop.  Returns (updated_reply,
    correction_applied, next_correction_text)."""
    correction_text = _extract_tool_line(reply, "CORRECT:")
    if correction_text is None:
        return reply, False, None

    hops = 0
    correction_applied = False
    final_reply = reply

    while correction_text is not None and hops < MAX_TOOL_HOPS:
        hops += 1
        t0 = time.time()
        print(f"[correct] fallback correction ({hops}/{MAX_TOOL_HOPS}): {correction_text}")

        scope = list(memory.shown_fact_ids) or None
        best_match = memory.find_best_fact_match(correction_text, fact_ids=scope)
        old_matches = [best_match] if best_match else []
        print(f"[correct] scope size={len(scope) if scope else 0} -> "
              f"{'matched id=' + str(best_match['id']) if best_match else 'no match'}")

        new_fact_id = memory.write_fact("inferred", correction_text, confidence=0.9,
                                         implicit=True, pinned=False, source="corrected")
        stored = new_fact_id is not None
        print(f"[correct] new fact {'stored id=' + str(new_fact_id) if stored else 'FAILED TO STORE'}: {correction_text[:60]}")
        if stored:
            correction_applied = True

        deactivated_facts = []
        failed_deactivations = []
        if old_matches:
            for match in old_matches:
                if stored and match["id"] == new_fact_id:
                    continue
                ok = memory.deactivate_fact(
                    match["id"],
                    reason=f"superseded by: {correction_text[:100]}",
                    superseded_by=new_fact_id if stored else None
                )
                if ok:
                    deactivated_facts.append(match)
                    print(f"[correct] deactivated old fact id={match['id']}: \"{match['fact'][:60]}\"")
                else:
                    failed_deactivations.append(match)
                    print(f"[correct] FAILED TO DEACTIVATE old fact id={match['id']}: \"{match['fact'][:60]}\"")
        else:
            print("[correct] no confident match for an existing fact — storing as new fact only")

        if deactivated_facts and stored:
            if len(deactivated_facts) == 1:
                status_msg = f"Correction applied. The old fact (\"{deactivated_facts[0]['fact']}\") has been retired and replaced with: \"{correction_text}\"."
            else:
                old_list = "; ".join(f"\"{m['fact']}\"" for m in deactivated_facts)
                status_msg = f"Correction applied. {len(deactivated_facts)} old facts ({old_list}) have been retired and replaced with: \"{correction_text}\"."
        elif stored:
            status_msg = f"Stored as a new fact (no clearly matching old fact was found to retire): \"{correction_text}\"."
        else:
            status_msg = "The correction could not be saved due to a memory system error. Tell Dave the correction didn't save and he should check the logs."

        if failed_deactivations:
            status_msg += f" Note: {len(failed_deactivations)} matching old fact(s) could not be retired — worth checking the logs."

        # Unified pattern: update existing assistant message instead of appending
        last_assistant_idx = None
        for i in range(len(conversation_history) - 1, -1, -1):
            if conversation_history[i]["role"] == "assistant":
                last_assistant_idx = i
                break
        if last_assistant_idx is not None:
            conversation_history[last_assistant_idx]["content"] = "Let me update that."

        conversation_history.append({
            "role": "user",
            "content": f"{status_msg}\n\nAcknowledge this briefly and naturally to Dave. Confirm the specific corrected value so Dave knows the exact update has been processed. Do not output another CORRECT: or RECALL: token. Do not repeat the correction text or any tool tokens in your response."
        })
        final_reply, _ = llama_chat(conversation_history)
        print(f"[timer] fallback correction: {time.time()-t0:.2f}s")
        correction_text = _extract_tool_line(final_reply, "CORRECT:")

    if correction_text is not None:
        print(f"[correct] WARNING: model still emitting CORRECT: after {MAX_TOOL_HOPS} hops, stripping token")
        final_reply = _strip_tool_tokens(final_reply, "CORRECT:") or "I tried to update that but ran into trouble — worth checking the logs."

    return final_reply, correction_applied, None


# ── Recall hop ──────────────────────────────────────────────
def _apply_recall(
    reply: str,
    conversation_history: list,
    memory: MemoryClient,
) -> tuple[str, str | None]:
    """Process a single RECALL: hop.  Returns (updated_reply, next_recall_query)."""
    recall_query = _extract_tool_line(reply, "RECALL:")
    if recall_query is None:
        return reply, None

    hops = 0
    final_reply = reply

    while recall_query is not None and hops < MAX_TOOL_HOPS:
        t0 = time.time()
        hops += 1
        print(f"[recall] fallback recall ({hops}/{MAX_TOOL_HOPS}): {recall_query}")
        memory_text = recall_memory(memory, recall_query)
        print(f"[recall] result: {memory_text[:300]}")

        # Unified pattern: update existing assistant message instead of appending
        last_assistant_idx = None
        for i in range(len(conversation_history) - 1, -1, -1):
            if conversation_history[i]["role"] == "assistant":
                last_assistant_idx = i
                break
        if last_assistant_idx is not None:
            conversation_history[last_assistant_idx]["content"] = "Let me check my memory."

        if not memory_text or memory_text == "No matching memories found.":
            conversation_history.append({
                "role": "user",
                "content": "Memory recall returned no results — this is normal, not an error. Just answer Dave's question using your general knowledge as you normally would. No need to mention memory or apologize."
            })
        else:
            conversation_history.append({
                "role": "user",
                "content": f"Memory recall results:\n{memory_text}\n\nThe facts above are your current reality. Treat them as your own memory — do not say you don't have information or that you cannot access something. Use the specific facts provided to answer directly. Extract and state the specific facts found. If the memory contains a specific detail requested by Dave (a time, number, name, date, or value), prioritize that detail over a general summary of the topic. Speak naturally as if you remembered this yourself. Only state facts that are explicitly in the results above. Do not invent or infer additional details. Do not output another RECALL: or CORRECT: token — you already have the information, just answer. Do not repeat tool tokens or the recall query in your response."
            })
        final_reply, _ = llama_chat(conversation_history)
        print(f"[timer] fallback recall: {time.time()-t0:.2f}s")
        recall_query = _extract_tool_line(final_reply, "RECALL:")

    if recall_query is not None:
        print(f"[recall] WARNING: model still emitting RECALL: after {MAX_TOOL_HOPS} hops, stripping token")
        final_reply = _strip_tool_tokens(final_reply, "RECALL:") or "I'm having trouble accessing that memory right now."

    return final_reply, None



# ── Main entry point ────────────────────────────────────────
def process_message(
    user_input: str,
    conversation_history: list,
    mcp_client: McpClient,
    memory: MemoryClient,
) -> tuple[str, bool]:
    """Process a single user message through the full Klara pipeline.

    Parameters
    ----------
    user_input : str
        The raw text the user typed / spoke.
    conversation_history : list[dict]
        OpenAI-style message list (mutable — appended to in-place).
    mcp_client : McpClient
        Active MCP client instance with available tools.
    memory : MemoryClient
        Active memory client instance (must have a live session).

    Returns
    -------
    tuple[str, bool]
        (final_reply, correction_applied)
    """
    # Trim conversation history if it grows too long
    if len(conversation_history) > 26:
        conversation_history[3:] = conversation_history[-24:]

    # Pre-emptive memory recall (for longer messages)
    if memory.session_id and len(user_input.split()) >= 8:
        t0 = time.time()
        memory_text = recall_memory(memory, user_input, facts_only=True)
        print(f"[timer] pre-emptive recall: {time.time()-t0:.2f}s")
        if memory_text and memory_text != "No matching memories found.":
            conversation_history.append({
                "role": "user",
                "content": f"Before responding, here is relevant memory context:\n{memory_text}"
            })
            conversation_history.append({
                "role": "assistant",
                "content": "Understood, I have that context."
            })

    # First LLM turn
    conversation_history.append({"role": "user", "content": user_input})

    # Get OpenAI-format tools from MCP server
    try:
        tools = _sync_list_tools(mcp_client)
    except Exception as e:
        print(f"[tool-call] Failed to load tools: {e}")
        tools = None

    # Unified tool-call loop
    t0 = time.time()
    reply, correction_applied = _tool_call_loop(
        conversation_history, mcp_client, memory, tools
    )
    print(f"[timer] total pipeline: {time.time()-t0:.2f}s")

    return reply, correction_applied


def _sync_list_tools(mcp_client: McpClient) -> list[dict] | None:
    """List tools from MCP client synchronously using run_coroutine_threadsafe."""
    if mcp_client is None or not mcp_client.is_connected:
        return None
    if mcp_client._loop is None:
        return None

    future = asyncio.run_coroutine_threadsafe(
        mcp_client.list_tools_openai(),
        mcp_client._loop,
    )
    try:
        return future.result(timeout=10.0)
    except Exception as e:
        print(f"[tool-call] Failed to list tools: {e}")
        return None


def _sync_execute_tool(mcp_client: McpClient, tool_name: str, arguments: dict) -> dict:
    """Execute a tool via MCP client synchronously using run_coroutine_threadsafe."""
    if mcp_client is None or not mcp_client.is_connected:
        return {"name": tool_name, "content": "MCP client not connected", "is_error": True}
    if mcp_client._loop is None:
        return {"name": tool_name, "content": "MCP event loop not available", "is_error": True}

    future = asyncio.run_coroutine_threadsafe(
        mcp_client.call_tool_by_id(tool_name, arguments),
        mcp_client._loop,
    )
    try:
        return future.result(timeout=30.0)
    except Exception as e:
        print(f"[tool-call] Tool '{tool_name}' failed: {e}")
        return {"name": tool_name, "content": f"Tool execution failed: {e}", "is_error": True}


def _tool_call_loop(
    conversation_history: list,
    mcp_client: McpClient,
    memory: MemoryClient,
    tools: list[dict] | None,
) -> tuple[str, bool]:
    """Execute the unified tool-call loop with the LLM.

    Returns:
        (final_reply, correction_applied)
    """
    loop_count = 0
    correction_applied = False

    while loop_count < MAX_TOOL_CALL_LOOPS:
        loop_count += 1

        # Call LLM with tools
        t0 = time.time()
        content, tool_calls = llama_chat(conversation_history, tools)
        print(f"[timer] llama_chat (loop {loop_count}): {time.time()-t0:.2f}s")

        # Handle tool calls
        if tool_calls:
            print(f"[tool-call] Model requested {len(tool_calls)} tool(s)")

            # Parse arguments from string to dict for API compatibility
            parsed_tool_calls = []
            for tc in tool_calls:
                args = tc['arguments']
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except (json.JSONDecodeError, TypeError):
                        args = {}
                parsed_tool_calls.append({
                    "id": tc['id'],
                    "type": "function",
                    "function": {
                        "name": tc['name'],
                        "arguments": args
                    }
                })

            # Add assistant message with tool_calls (and any accompanying content) to history
            assistant_msg = {"role": "assistant", "tool_calls": parsed_tool_calls}
            if content:
                assistant_msg["content"] = content
            conversation_history.append(assistant_msg)

            # Execute each tool
            for tc in tool_calls:
                print(f"[tool-call] Executing: {tc['name']}({tc['arguments']})")
                args = next(p["function"]["arguments"] for p in parsed_tool_calls if p["id"] == tc['id'])
                result = _sync_execute_tool(mcp_client, tc['name'], args)

                # Add tool result to history
                conversation_history.append({
                    "role": "tool",
                    "tool_call_id": tc['id'],
                    "content": result['content']
                })

                # Check for CORRECT: in tool results
                if result['name'] == 'web_search' and 'CORRECT:' in result['content']:
                    correction_text = _extract_tool_line(result['content'], "CORRECT:")
                    if correction_text:
                        correction_applied = _handle_correction_via_tool(
                            correction_text, conversation_history, memory
                        )

            continue

        # No tool calls — check for RECALL: token (remains text-based)
        recall_query = _extract_tool_line(content, "RECALL:")
        if recall_query:
            print(f"[recall] Model requested memory recall: {recall_query}")
            content, _ = _apply_recall_from_tool_call(
                content, conversation_history, memory, recall_query
            )
            continue

        # Clean up tool tokens and return
        for token in ("RECALL:", "CORRECT:"):
            content = _strip_tool_tokens(content, token)

        return content, correction_applied

    # Max loops reached — strip any remaining tool tokens
    print(f"[tool-call] WARNING: reached max loops ({MAX_TOOL_CALL_LOOPS}), returning last content")
    for token in ("RECALL:", "CORRECT:"):
        content = _strip_tool_tokens(content, token)
    return content, correction_applied


def _handle_correction_via_tool(
    correction_text: str,
    conversation_history: list,
    memory: MemoryClient,
) -> bool:
    """Handle CORRECT: token found in tool result content.

    Returns True if correction was applied.
    """
    print(f"[correct] correction found in tool result: {correction_text}")
    scope = list(memory.shown_fact_ids) or None
    best_match = memory.find_best_fact_match(correction_text, fact_ids=scope)
    old_matches = [best_match] if best_match else []

    new_fact_id = memory.write_fact("inferred", correction_text, confidence=0.9,
                                     implicit=True, pinned=False, source="corrected")
    stored = new_fact_id is not None
    correction_applied = False

    if stored:
        correction_applied = True

    if old_matches:
        for match in old_matches:
            if stored and match["id"] == new_fact_id:
                continue
            memory.deactivate_fact(
                match["id"],
                reason=f"superseded by: {correction_text[:100]}",
                superseded_by=new_fact_id if stored else None
            )

    return correction_applied


def _apply_recall_from_tool_call(
    content: str,
    conversation_history: list,
    memory: MemoryClient,
    recall_query: str,
) -> tuple[str, str]:
    """Process a RECALL: request from tool-call loop.

    Returns (updated_content, next_recall_query).
    """
    print(f"[recall] processing recall: {recall_query}")
    memory_text = recall_memory(memory, recall_query)
    print(f"[recall] result: {memory_text[:300]}")

    # Add assistant message with original content
    conversation_history.append({"role": "assistant", "content": content})

    if not memory_text or memory_text == "No matching memories found.":
        conversation_history.append({
            "role": "user",
            "content": "Memory recall returned no results — this is normal, not an error. Just answer Dave's question using your general knowledge as you normally would. No need to mention memory or apologize."
        })
    else:
        conversation_history.append({
            "role": "user",
            "content": f"Memory recall results:\n{memory_text}\n\nThe facts above are your current reality. Treat them as your own memory — do not say you don't have information or that you cannot access something. Use the specific facts provided to answer directly. Extract and state the specific facts found. If the memory contains a specific detail requested by Dave (a time, number, name, date, or value), prioritize that detail over a general summary of the topic. Speak naturally as if you remembered this yourself. Only state facts that are explicitly in the results above. Do not invent or infer additional details. Do not output another RECALL: or CORRECT: token — you already have the information, just answer. Do not repeat tool tokens or the recall query in your response."
        })

    return content, None


# ── chat_with_search (legacy wrapper) ─────────────────────────
# This was the original top-level function in clara2.py.  It is kept
# here so that core.backend can continue to call it via core.chat_with_search.

def chat_with_search(
    user_input: str,
    conversation_history: list,
    search: McpClient,
    memory: MemoryClient,
) -> tuple[str, bool]:
    """Legacy wrapper that mirrors the old clara2.py chat_with_search API.

    Delegates to :func:`process_message` internally but keeps the same
    function signature so existing callers don't break.
    """
    return process_message(user_input, conversation_history, search, memory)


# ── write_episode_async ──────────────────────────────────────

def write_episode_async(
    memory: MemoryClient,
    user_input: str,
    clara_reply: str,
    skip_facts: bool = False,
) -> None:
    """Run fact extraction / episode storage in a background thread.

    Wraps :func:`analyze_exchange` so the GUI never blocks on the LLM
    during post-processing.
    """
    analyze_exchange(memory, user_input, clara_reply, skip_facts=skip_facts)


# ── Warmup helpers ──────────────────────────────────────────

def warmup_tts() -> None:
    """Send a short test utterance to the TTS server to prime the pipeline.

    Called once during startup so the first real response has no cold
    start latency.
    """
    try:
        with httpx.stream(
            "POST",
            f"http://{TTS_HOST}:{TTS_PORT}/tts",
            json={"text": "Hello."},
            timeout=15.0,
        ) as resp:
            resp.raise_for_status()
            print("[warmup] TTS primed")
    except Exception as e:
        print(f"[warmup] TTS warmup skipped (non-fatal): {e}")


def warmup_llm_wrapper(conversation_history: list) -> None:
    """Alias so core/__init__.py can export ``warmup_llm`` without
    conflicting with the imported symbol from llm_client."""
    warmup_llm(conversation_history)
