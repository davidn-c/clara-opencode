"""
core/backend.py — ClaraBackend: wraps clara2 AI services (LLM, memory, search, TTS).
"""

import asyncio
import os
import subprocess
import signal
import threading
import time

import httpx

import core
from core.logger import gui_logger
from config import GEMMA_LAUNCH_SCRIPT, GEMMA_LOG, COMFYUI_DIR, COMFYUI_LOG
from core.stt import STTClient
from core.wake_word import WakeWordListener
from core.mcp_client import McpClient


class CancelledError(Exception):
    """Raised when a chat request is cancelled by a new user message."""
    pass


# faster-whisper's own default (0) means "use every detected core" — capped
# lower since Whisper now shares the CPU with a continuous wake-word
# inference loop and with whatever's still settling from Gemma/ComfyUI's
# own startup loads. See STTClient docstring for the full reasoning.
STT_CPU_THREADS = 2

class ClaraBackend:
    """
    Thin adapter between the GUI and the clara2 library.
    Owns the conversation history and lifecycle of backend processes.
    """

    def __init__(self):
        self.memory = None
        self.search = None
        self.conversation_history: list = []
        self.running = False
        self.audio_interrupt = threading.Event()
        self._chat_generation = 0
        self._audio_lock = threading.Lock()
        self._audio_thread: threading.Thread | None = None
        self.stt = None
        self.wake_word = None
        self._comfyui_proc = None
        self._comfyui_running = False
        self._mcp_client: McpClient | None = None
        self._mcp_loop: asyncio.AbstractEventLoop | None = None
        self._mcp_task: threading.Thread | None = None

    # ── Startup ────────────────────────────────────────────────

    def start(self) -> None:
        """Initialise memory/search clients and build the opening prompt."""
        gui_logger.log("[system] Starting Klara")

        self.memory = core.MemoryClient(core.MEMORY_BASE)

        try:
            context = self.memory.start_session()
        except Exception as e:
            gui_logger.log(f"[memory] start_session failed: {e}")
            context = {}
        system_prompt = core.build_system_prompt(context)

        self.conversation_history = [
            {"role": "system",    "content": system_prompt},
            {"role": "user",      "content": "Do you have internet access?"},
            {"role": "assistant", "content": "Yes, I do."},
        ]
        self.running = True

        # Start MCP client in a background thread
        self._mcp_task = threading.Thread(
            target=self._start_mcp_async, daemon=True
        )
        self._mcp_task.start()

    # ── MCP Client ─────────────────────────────────────────────

    def _start_mcp_async(self) -> None:
        """Run MCP client connect in a dedicated event loop thread."""
        loop = asyncio.new_event_loop()
        self._mcp_loop = loop
        try:
            loop.run_until_complete(self._mcp_client_connect())
            # Keep the loop running for future coroutine calls
            # The loop will be stopped during shutdown
            loop.run_forever()
        except Exception as e:
            gui_logger.log(f"[mcp] Failed to start: {e}")
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:
                pass
            loop.close()

    async def _mcp_client_connect(self) -> None:
        """Async connection to MCP server."""
        self._mcp_client = McpClient()
        self._mcp_client._loop = asyncio.get_event_loop()
        await self._mcp_client.connect()
        gui_logger.log("[mcp] MCP client ready")

    def mcp_call_tool(self, name: str, arguments: dict | None = None) -> str:
        """Synchronous wrapper for calling an MCP tool.

        Runs the async call_tool in the dedicated MCP event loop.
        Blocks the caller until the tool completes or fails.
        """
        if self._mcp_client is None or not self._mcp_client.is_connected:
            gui_logger.log("[mcp] Not connected, tool call skipped")
            return ""
        if self._mcp_loop is None:
            gui_logger.log("[mcp] No event loop running")
            return ""

        future = asyncio.run_coroutine_threadsafe(
            self._mcp_client.call_tool(name, arguments or {}),
            self._mcp_loop,
        )
        try:
            return future.result(timeout=30.0)
        except asyncio.TimeoutError:
            gui_logger.log(f"[mcp] Tool '{name}' call timed out")
            return ""
        except Exception as e:
            gui_logger.log(f"[mcp] Tool '{name}' failed: {e}")
            return ""

    # ── Voice pipeline ───────────────────────────────────────────

    def start_voice_pipeline(self, on_wake, on_transcribed, on_empty=None,
                                  wake_word_enabled: bool = False) -> None:
            """Call once after self.start(). on_wake fires the instant the wake
            word triggers; on_transcribed fires with the final transcribed text
            once an utterance finishes recording. on_empty (optional) fires
            instead whenever transcription comes back blank — genuine silence,
            a mic hiccup, background noise triggering the wake word with no
            real speech after it, etc. — so the caller can reset any
            "listening"/"thinking" UI state that on_wake set, since without
            this hook nothing ever fires to undo it on the empty-result path.

            wake_word_enabled: forwarded to WakeWordListener. Defaults to
            False — the "Talk to Klara" button remains the only way to start
            a recording, and openWakeWord is never loaded at startup. Flip via
            set_wake_word_enabled() at runtime (e.g. a GUI checkbox) once
            ready to test wake-word detection.
            """
            self.stt = STTClient(model_size="small", device="cpu", compute_type="int8",
                                  cpu_threads=STT_CPU_THREADS)

            def _on_utterance(audio):
                text = self.stt.transcribe(audio)
                if text:
                    on_transcribed(text)
                else:
                    print("[voice] transcription empty, ignoring")
                    if on_empty is not None:
                        on_empty()

            self.wake_word = WakeWordListener(
                on_wake=on_wake,
                on_utterance=_on_utterance,
                model_name="hey_jarvis_v0.1",
                wake_word_enabled=wake_word_enabled,
            )
            self.wake_word.start()

    def set_wake_word_enabled(self, enabled: bool) -> None:
            """Toggle openWakeWord detection on the already-running listener
            (e.g. from a GUI checkbox), without tearing down/restarting the
            parecord capture loop — a restart would drop audio and reintroduce
            the backlog issues _drain_pending() exists to avoid."""
            if self.wake_word is not None:
                self.wake_word.set_enabled(enabled)

    def trigger_voice_now(self) -> None:
        """Toggle a manual recording — called from the GUI's 'Talk to
        Klara' button. If no recording is in progress this starts one
        immediately, bypassing wake-word detection. If one is already in
        progress (started by this button or by a real wake word), this
        ends it right now instead of waiting on the silence timer. The
        toggle logic itself lives entirely in WakeWordListener.trigger_now()
        — this is just a pass-through, so the backend doesn't need to know
        or care which mode it's currently in."""
        if self.wake_word is not None:
            self.wake_word.trigger_now()

    # ── Chat ───────────────────────────────────────────────────

    def chat(self, user_input: str) -> str:
        """Send a user message, get Klara's reply, write episode to memory."""
        my_generation = self._chat_generation
        reply, correction_applied = core.chat_with_search(
            user_input,
            self.conversation_history,
            self._mcp_client,
            self.memory,
        )

        if self._chat_generation != my_generation:
            gui_logger.log("[system] Request cancelled during processing")
            raise CancelledError("Request cancelled")

        self.memory.write_episode("clara", reply, 0.5)

        threading.Thread(
            target=core.write_episode_async,
            args=(self.memory, user_input, reply, correction_applied),
            daemon=True,
        ).start()

        return reply

    # ── TTS ────────────────────────────────────────────────────

    def speak(self, text: str, interrupt_event: threading.Event) -> None:
        """Speak text via Kokoro TTS (blocking — run via speak_async).

        interrupt_event is created and assigned to self.audio_interrupt by
        the caller (speak_async) BEFORE this runs, so _stop_audio() always
        has a valid, current Event to signal — no window where it could
        set a stale Event left over from the previous call.
        """
        core.speak(text, interrupt_event)

    def speak_async(self, text: str) -> None:
        with self._audio_lock:
            if self._audio_thread is not None and self._audio_thread.is_alive():
                self.audio_interrupt.set()
                self._audio_thread.join()

            # Create and publish the new interrupt Event synchronously,
            # under the lock, before the thread even starts — this is the
            # fix. Previously this Event was created inside speak() itself,
            # which only runs once the new thread gets scheduled; in the
            # gap between t.start() and that first line executing,
            # self.audio_interrupt still pointed at the OLD (already-used)
            # Event, so a stop-audio request in that window would signal
            # nothing meaningful.
            my_event = threading.Event()
            self.audio_interrupt = my_event

            def _run():
                if self.wake_word is not None:
                    self.wake_word.mute()
                try:
                    self.speak(text, my_event)
                finally:
                    if self.wake_word is not None:
                        self.wake_word.unmute()

            t = threading.Thread(target=_run, daemon=True)
            self._audio_thread = t
            t.start()

    # ── KV Cache Refresh ───────────────────────────────────────

    def _refresh_kv_cache(self) -> None:
        """Clear all llama.cpp slot KV caches via HTTP API.
        
        Sends an 'idle' action to each slot, which resets its KV cache
        and prepares it for a fresh context. Non-fatal if the API is
        unavailable — the conversation history consolidation still works.
        """
        try:
            base_url = core.GEMMA_BASE_URL  # e.g., http://0.0.0.0:8082
            # Query active slots
            resp = httpx.get(f"{base_url}/slots", timeout=5.0)
            if resp.status_code == 200:
                slots = resp.json()
                if isinstance(slots, dict):
                    slot_ids = list(slots.keys())
                elif isinstance(slots, list):
                    slot_ids = [s.get("id") or s for s in slots if isinstance(s, (dict, str))]
                else:
                    slot_ids = []

                for slot_id in slot_ids:
                    try:
                        httpx.post(
                            f"{base_url}/slots/{slot_id}",
                            json={"action": "idle"},
                            timeout=5.0,
                        )
                    except Exception:
                        pass  # Skip individual slot failures

                if slot_ids:
                    gui_logger.log(f"[system] KV cache refreshed for {len(slot_ids)} slot(s)")
        except Exception as e:
            gui_logger.log(f"[system] KV cache refresh skipped: {e}")

    # ── Context Consolidation ──────────────────────────────────

    def consolidate_context(self) -> None:
        """Summarize the current conversation and refresh the LLM context.
        
        Extracts the conversation history, sends it to the LLM for
        summarization, then replaces the history with a compact
        [system_prompt, summary, acknowledgment] structure.
        Also clears the llama.cpp KV cache slots.
        
        Must be called on a background thread (not the main GUI thread).
        """
        try:
            # Extract conversation turns (skip system prompt)
            turns = self.conversation_history[1:] if self.conversation_history else []
            
            if not turns:
                gui_logger.log("[system] No conversation to consolidate")
                self._refresh_kv_cache()
                return

            # Build a text summary of the conversation
            conversation_text = "\n".join(
                f"{m.get('role', 'unknown')}: {m.get('content', '')}"
                for m in turns
            )

            # Get the system prompt (first message)
            system_prompt = self.conversation_history[0]["content"] if self.conversation_history else "You are Klara."

            # Send consolidation prompt to LLM
            gui_logger.log("[system] Consolidating conversation context...")
            
            consolidation_prompt = (
                "The following is a conversation between the user and an AI assistant. "
                "Please provide a concise summary of this conversation, preserving only "
                "essential context, facts, and preferences that should be carried forward. "
                "Keep it brief but retain any important details, ongoing topics, or stated preferences.\n\n"
                f"Conversation:\n{conversation_text}\n\n"
                "Summary:"
            )

            summary_resp = httpx.post(
                f"{core.GEMMA_BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {core.GEMMA_API_KEY}"},
                json={
                    "model": "qwen",
                    "messages": [
                        {"role": "system", "content": "You are a conversation summarizer. Provide concise, context-preserving summaries."},
                        {"role": "user", "content": consolidation_prompt}
                    ],
                    "max_tokens": 1024,
                    "temperature": 0.3,
                },
                timeout=60.0,
            )
            summary_resp.raise_for_status()
            summary = summary_resp.json()["choices"][0]["message"].get("content", "").strip()

            if not summary:
                summary = "Previous conversation context has been summarized."

            # Replace conversation history with consolidated version
            self.conversation_history = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"[Context consolidated: {summary}]"},
                {"role": "assistant", "content": "Context has been refreshed. How can I help you?"},
            ]

            # Clear the KV cache
            self._refresh_kv_cache()

            gui_logger.log("[system] Context consolidated and refreshed")

        except Exception as e:
            gui_logger.log(f"[system] Context consolidation failed: {e}")
            # Still clear the KV cache even if summarization fails
            try:
                self._refresh_kv_cache()
            except Exception:
                pass

    # ── Shutdown ───────────────────────────────────────────────

    def shutdown(self) -> None:
        """Gracefully close all services and kill background processes."""
        try:
            gui_logger.log("[system] Closing memory")
            self.memory.end_session()
            self.memory.close()
            core.llm_client.close()
        except Exception:
            pass

        # Stop ComfyUI using tracked process (not pkill)
        self.stop_comfyui()

        # Kill other managed processes
        for pattern in ("llama-server",):
            try:
                subprocess.run(["pkill", "-f", pattern], check=False)
            except Exception:
                pass

        if self.wake_word is not None:
            self.wake_word.stop()

        # Release the faster-whisper model to prevent hangs on restart
        if self.stt is not None:
            try:
                gui_logger.log("[system] Releasing faster-whisper model")
                self.stt.shutdown()
            except Exception as e:
                gui_logger.log(f"[system] STT shutdown warning: {e}")

        # Disconnect MCP client and stop its event loop
        if self._mcp_client is not None and self._mcp_loop is not None:
            try:
                asyncio.run_coroutine_threadsafe(
                    self._mcp_client.disconnect(),
                    self._mcp_loop,
                ).result(timeout=5.0)
                gui_logger.log("[mcp] Client disconnected")
            except Exception as e:
                gui_logger.log(f"[mcp] Disconnect warning: {e}")
            # Stop the event loop thread
            try:
                self._mcp_loop.call_soon_threadsafe(self._mcp_loop.stop)
                if self._mcp_task is not None:
                    self._mcp_task.join(timeout=3.0)
            except Exception as e:
                gui_logger.log(f"[mcp] Loop stop warning: {e}")

        self.running = False

    # ── External process launchers ─────────────────────────────

    @staticmethod
    def launch_gemma() -> None:
        """Start the Gemma llama-server in the background."""
        try:
            subprocess.Popen(
                ["bash", "-c", f"bash {GEMMA_LAUNCH_SCRIPT} >> {GEMMA_LOG} 2>&1"],
                start_new_session=True,
            )
        except Exception as e:
            gui_logger.log(f"[system] Failed to start Gemma: {e}")

    @staticmethod
    def wait_for_gemma_ready(timeout: float = 90.0, poll_interval: float = 1.0) -> bool:
        """Blocks until Gemma's llama-server responds to a lightweight
        request, or timeout elapses. Call this between launch_gemma() and
        launch_comfyui()/start_voice_pipeline() so the heaviest startup
        load (a 26B MOE model) finishes before ComfyUI's checkpoint load
        and Whisper's model load pile on top of it — this staggering, plus
        the STT_CPU_THREADS cap, is the fix for the resource-contention
        lockup seen when everything loaded at once. Returns True once
        Gemma answers, or False on timeout — startup proceeds either way
        rather than hanging forever, so a slow/broken Gemma doesn't also
        block ComfyUI and voice input from ever starting."""
        gui_logger.log("[system] Waiting for Gemma to finish loading...")
        client = httpx.Client(timeout=3.0)
        deadline = time.time() + timeout
        try:
            while time.time() < deadline:
                try:
                    resp = client.get(
                        f"{core.GEMMA_BASE_URL}/models",
                        headers={"Authorization": f"Bearer {core.GEMMA_API_KEY}"},
                    )
                    if resp.status_code == 200:
                        gui_logger.log("[system] Gemma ready")
                        return True
                except Exception:
                    pass
                time.sleep(poll_interval)
        finally:
            client.close()
        gui_logger.log(f"[system] WARNING: Gemma not ready after {timeout:.0f}s — continuing startup anyway")
        return False

    @staticmethod
    def launch_comfyui() -> None:
        """Start ComfyUI in the background (legacy — use start_comfyui instead)."""
        try:
            subprocess.Popen(
                [
                    "bash", "-c",
                    f"cd {COMFYUI_DIR} && "
                    f"source venv/bin/activate && "
                    f"python main.py >> {COMFYUI_LOG} 2>&1",
                ],
                start_new_session=True,
            )
            gui_logger.log("[system] ComfyUI launching...")
        except Exception as e:
            gui_logger.log(f"[system] Failed to start ComfyUI: {e}")

    def start_comfyui(self) -> bool:
        """Start ComfyUI and track the process for later termination.
        Returns True if started successfully, False if already running."""
        if self._comfyui_running:
            gui_logger.log("[system] ComfyUI already running")
            return False
        try:
            self._comfyui_proc = subprocess.Popen(
                [
                    "bash", "-c",
                    f"cd {COMFYUI_DIR} && "
                    f"source venv/bin/activate && "
                    f"python main.py >> {COMFYUI_LOG} 2>&1",
                ],
                start_new_session=True,
            )
            self._comfyui_running = True
            gui_logger.log("[system] ComfyUI launching...")
            return True
        except Exception as e:
            gui_logger.log(f"[system] Failed to start ComfyUI: {e}")
            return False

    def stop_comfyui(self) -> bool:
        """Stop ComfyUI by killing the process group.
        Returns True if stopped, False if not running."""
        if not self._comfyui_running:
            gui_logger.log("[system] ComfyUI not running")
            return False
        try:
            if self._comfyui_proc is not None:
                try:
                    os.killpg(os.getpgid(self._comfyui_proc.pid), signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    pass
                try:
                    self._comfyui_proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(os.getpgid(self._comfyui_proc.pid), signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass
            self._comfyui_proc = None
            self._comfyui_running = False
            gui_logger.log("[system] ComfyUI stopped")
            return True
        except Exception as e:
            gui_logger.log(f"[system] Failed to stop ComfyUI: {e}")
            self._comfyui_running = False
            return False

    def is_comfyui_running(self) -> bool:
        """Check if ComfyUI process is still alive."""
        if not self._comfyui_running:
            return False
        if self._comfyui_proc is None:
            return False
        if self._comfyui_proc.poll() is not None:
            self._comfyui_running = False
            return False
        return True
