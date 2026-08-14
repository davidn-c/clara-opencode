"""
ui/main_window.py — ClaraGUI: the top-level customtkinter window and application controller.
"""

import sys
import os
from core.comfy_video_client import ComfyVideoClient

# Guarantee the clara/ project root is on sys.path so that
# `from config import ...` and `from core.x import ...` always resolve.
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import subprocess
import threading
import time
import customtkinter as ctk
from tkinter import filedialog, messagebox

from config import (
    CHAT_DIR, WINDOW_TITLE, WINDOW_WIDTH, WINDOW_HEIGHT,
    STATUS_READY, STATUS_THINKING, STATUS_TTS, STATUS_ERROR, STATUS_SEARCH, STATUS_MEMORY,
)
from core.logger import gui_logger
from core.backend import ClaraBackend, CancelledError
from core.comfy_client import ComfyUIClient
from ui.theme import ThemeManager
from ui.sidebar import Sidebar
from ui.chat_tab import ChatTab
from ui.generate_tab import GenerateTab


class ClaraGUI(ctk.CTk):
    """
    Top-level application window using customtkinter.

    Responsibilities:
      • Compose sidebar + notebook tabs
      • Route GUI-logger messages to the correct log pane
      • Manage the send/response lifecycle (threading, spinner, autosave)
      • Handle chat-file management (save / load / delete)
      • Handle shutdown
    """

    def __init__(self):
        super().__init__()

        # ── Theme ────────────────────────────────────────────────
        ThemeManager.apply_theme("dark")
        self._current_theme = "dark"
        self._colors = ThemeManager.get_colors("dark")

        # ── Backend ──────────────────────────────────────────────
        self.backend = ClaraBackend()
        self.backend.start()
        self.backend.launch_gemma()
        self.backend.start_comfyui()
        self.backend.start_voice_pipeline(
            on_wake=self._on_wake_word,
            on_transcribed=self._on_voice_transcribed,
            on_empty=self._on_voice_empty,
        )

        # Auto Logging On / Off
        self.autosave_enabled = False

        # Enable Memory Startup Value
        self.memory_enabled = True

        # ── Window setup ─────────────────────────────────────────
        self.title(WINDOW_TITLE)
        self.geometry(f"{WINDOW_WIDTH}x{WINDOW_HEIGHT}")
        self.protocol("WM_DELETE_WINDOW", self.on_exit)

        self.current_file: str | None = None

        # ── Status bar ───────────────────────────────────────────
        self.status_var = ctk.StringVar(value=STATUS_READY)

        # ── Spinner state ────────────────────────────────────────
        self.spinner_running = False
        self.spinner_index = 0
        self.spinner_states = ["Thinking", "Thinking.", "Thinking..", "Thinking..."]

        # ── Logs pin state ───────────────────────────────────────
        self._logs_pinned = self._load_logs_pinned()
        self._logs_collapsed = not self._logs_pinned

        # ── Build the UI ─────────────────────────────────────────
        self._build_ui()

        # ── Poll the logger queue ────────────────────────────────
        self.after(250, self._process_logs)

    def _on_wake_word(self) -> None:
        self.after(0, lambda: self._set_status("Listening..."))
        self.after(0, lambda: self._set_talk_button_recording(True))

    def _on_talk_button(self) -> None:
        self.backend.trigger_voice_now()

    def _on_wake_word_toggle(self) -> None:
        enabled = self.wake_word_var.get()
        self.backend.set_wake_word_enabled(enabled)
        self._log_tool(f"Wake word {'enabled' if enabled else 'disabled'}")

    def _set_talk_button_recording(self, recording: bool) -> None:
        self.chat_tab_frame.talk_btn.configure(
            text="⏹ Stop Recording" if recording else "🎤"
        )

    def _on_voice_transcribed(self, text: str) -> None:
        def _do():
            self._stop_audio()
            self._set_talk_button_recording(False)
            self._dispatch_message(text)
        self.after(0, _do)

    def _on_voice_empty(self) -> None:
        self.after(0, lambda: self._set_status(STATUS_READY))
        self.after(0, lambda: self._set_talk_button_recording(False))

    # ── UI Construction ────────────────────────────────────────

    def _build_ui(self) -> None:
        # Apply theme colors
        self.configure(fg_color=self._colors["bg"])

        # Root pane: sidebar | right content
        main_frame = ctk.CTkFrame(self, fg_color=self._colors["bg"])
        main_frame.pack(fill="both", expand=True)

        # ---- Sidebar ----
        self.sidebar = Sidebar(
            main_frame,
            on_toggle_think=self._toggle_think_mode,
            on_toggle_theme=self._toggle_theme,
        )
        self.sidebar.pack(side="left", fill="y", padx=(0, 2))
        self.sidebar.save_btn.configure(command=self._save_chat)
        self.sidebar.load_btn.configure(command=self._load_selected_chat)
        self.sidebar.delete_btn.configure(command=self._delete_selected_chat)
        self.sidebar._on_font_size_changed = self._on_font_size_changed

        # ---- Right frame ----
        right = ctk.CTkFrame(main_frame, fg_color=self._colors["bg"])
        right.pack(fill="both", expand=True)

        # Notebook (CTkTabview)
        self.tabview = ctk.CTkTabview(
            right,
            fg_color=self._colors["bg"],
            segmented_button_fg_color=self._colors["bg_dark"],
            segmented_button_selected_color=self._colors["accent"],
            segmented_button_selected_hover_color=self._colors["bg_accent_hover"],
            segmented_button_unselected_color=self._colors["bg_dark"],
            segmented_button_unselected_hover_color=self._colors["bg_accent_hover"],
            text_color=self._colors["fg"],
        )
        self.tabview.pack(fill="both", expand=True)

        # Chat tab
        self.chat_tab_frame = ChatTab(
            self.tabview.add("Chat"),
            font_size=self.sidebar.font_size,
        )
        self.chat_tab_frame.pack(fill="both", expand=True)

        # Generate tab (merged Image/Video)
        comfy = ComfyUIClient()
        comfy_video = ComfyVideoClient()
        self.generate_tab_frame = GenerateTab(
            self.tabview.add("Generate"),
            comfy_client=comfy,
            comfy_video_client=comfy_video,
            backend=self.backend,
            tts_var=self.sidebar.tts_var,
        )
        self.generate_tab_frame.pack(fill="both", expand=True)

        # Wire chat tab buttons
        self.chat_tab_frame.bind_send(self._send_message)
        self.chat_tab_frame.bind_talk(self._on_talk_button)
        self.chat_tab_frame.bind_clear(self._clear_chat)
        self.chat_tab_frame.bind_stop(self._stop_audio)
        self.chat_tab_frame.bind_enter(self._handle_enter)
        self.chat_tab_frame.bind_memory_toggle(self._toggle_memory_service)

        # ---- Status bar ----
        self.status_frame = ctk.CTkFrame(right, fg_color=self._colors["bg_bar"], height=24)
        self.status_frame.pack(fill="x", side="bottom")
        self.status_frame.pack_propagate(False)

        ctk.CTkLabel(
            self.status_frame,
            textvariable=self.status_var,
            font=("Inter", 9),
            text_color=self._colors["fg"],
            anchor="w",
        ).pack(fill="x", padx=6, pady=2)

        # ---- Activity logs ----
        self._logs_collapsed = not self._logs_pinned
        logs_frame = ctk.CTkFrame(right, fg_color=self._colors["bg"], height=0)
        if not self._logs_collapsed:
            logs_frame.pack(fill="x", side="bottom")
        self._logs_frame = logs_frame

        logs_btn_row = ctk.CTkFrame(right, fg_color="transparent")
        logs_btn_row.pack(side="bottom", fill="x", padx=8, pady=(0, 2))

        self._logs_toggle_btn = ctk.CTkButton(
            logs_btn_row,
            text="▼ Activity Logs",
            font=("Inter", 9),
            height=20,
            corner_radius=4,
            fg_color="transparent",
            hover_color=self._colors["bg_accent_hover"],
            command=self._toggle_logs,
        )
        self._logs_toggle_btn.pack(side="left", fill="x", expand=True)

        self._logs_pin_btn = ctk.CTkButton(
            logs_btn_row,
            text="📌",
            font=("Inter", 9),
            height=20,
            width=28,
            corner_radius=4,
            fg_color="transparent",
            hover_color=self._colors["bg_accent_hover"],
            command=self._toggle_logs_pin,
        )
        self._logs_pin_btn.pack(side="right", padx=(4, 0))

        if self._logs_pinned:
            self._logs_toggle_btn.configure(text="📌 Activity Logs (pinned)")
            self._logs_toggle_btn.configure(state="disabled")
            self._logs_pin_btn.configure(text="📍")
        else:
            self._logs_pin_btn.configure(text="📌")

        self._tool_log = ctk.CTkTextbox(
            logs_frame,
            height=120,
            font=("JetBrains Mono", 9),
            fg_color=self._colors["mono_bg"],
            text_color=self._colors["mono_fg"],
            corner_radius=0,
            state="disabled",
            wrap="word",
        )
        self._tool_log.pack(fill="both", expand=True, padx=4, pady=(4, 0))

        self._memory_log = ctk.CTkTextbox(
            logs_frame,
            height=120,
            font=("JetBrains Mono", 9),
            fg_color=self._colors["mono_bg"],
            text_color=self._colors["mono_fg"],
            corner_radius=0,
            state="disabled",
            wrap="word",
        )
        self._memory_log.pack(fill="both", expand=True, padx=4, pady=(0, 4))

        # ---- Bottom button row ----
        btn_row = ctk.CTkFrame(right, fg_color="transparent")
        btn_row.pack(fill="x", side="bottom", padx=8, pady=4)

        self.wake_word_var = ctk.BooleanVar(value=False)

        ctk.CTkSwitch(
            btn_row,
            text="Wake Word",
            variable=self.wake_word_var,
            font=("Inter", 10),
            command=self._on_wake_word_toggle,
        ).pack(side="left", padx=2)

        self.mem_btn = ctk.CTkButton(
            btn_row,
            text="Memory: ON",
            font=("Inter", 10),
            corner_radius=6,
            height=28,
            fg_color="transparent",
            hover_color=self._colors["bg_accent_hover"],
        )
        self.mem_btn.pack(side="right", padx=2)

        ctk.CTkButton(
            btn_row,
            text="Exit",
            font=("Inter", 10, "bold"),
            corner_radius=6,
            height=28,
            fg_color=self._colors["accent_red"],
            hover_color=self._colors["accent_red"],
            text_color=self._colors["fg"],
            command=self.on_exit,
        ).pack(side="right", padx=2)

        # Load saved chats
        self.sidebar.load_chat_list()

    def _toggle_theme(self, theme: str) -> None:
        self._current_theme = theme
        ThemeManager.apply_theme(theme)
        self._colors = ThemeManager.get_colors(theme)
        self.configure(fg_color=self._colors["bg"])
        self.tabview.configure(fg_color=self._colors["bg"])
        self.sidebar.update_theme()
        self.chat_tab_frame.update_theme()
        self.generate_tab_frame.update_theme()
        self._update_ui_colors()

    def _update_ui_colors(self) -> None:
        """Update all remaining UI elements that weren't covered by component update_theme() calls."""
        self.status_frame.configure(fg_color=self._colors["bg_bar"])
        self._logs_toggle_btn.configure(hover_color=self._colors["bg_accent_hover"])
        self._logs_pin_btn.configure(hover_color=self._colors["bg_accent_hover"])
        self.mem_btn.configure(hover_color=self._colors["bg_accent_hover"])

    def _on_font_size_changed(self, size: int) -> None:
        self.chat_tab_frame.set_font_size(size)

    def _toggle_logs(self) -> None:
        if self._logs_pinned:
            return
        self._logs_collapsed = not self._logs_collapsed
        if self._logs_collapsed:
            self._logs_frame.pack_forget()
            self._logs_toggle_btn.configure(text="▼ Activity Logs")
        else:
            self._logs_frame.pack(fill="x", side="bottom")
            self._logs_toggle_btn.configure(text="▲ Activity Logs")

    def _toggle_logs_pin(self) -> None:
        self._logs_pinned = not self._logs_pinned
        self._save_logs_pinned()
        if self._logs_pinned:
            self._logs_collapsed = False
            self._logs_frame.pack(fill="x", side="bottom")
            self._logs_toggle_btn.configure(text="📌 Activity Logs (pinned)")
            self._logs_toggle_btn.configure(state="disabled")
        else:
            self._logs_collapsed = True
            self._logs_frame.pack_forget()
            self._logs_toggle_btn.configure(text="▼ Activity Logs")
            self._logs_toggle_btn.configure(state="normal")

    def _load_logs_pinned(self) -> bool:
        try:
            config_dir = os.path.expanduser("~/.klara")
            os.makedirs(config_dir, exist_ok=True)
            pinned_file = os.path.join(config_dir, "logs_pinned")
            if os.path.exists(pinned_file):
                with open(pinned_file, "r") as f:
                    return f.read().strip().lower() == "true"
        except Exception:
            pass
        return False

    def _save_logs_pinned(self) -> None:
        try:
            config_dir = os.path.expanduser("~/.klara")
            os.makedirs(config_dir, exist_ok=True)
            pinned_file = os.path.join(config_dir, "logs_pinned")
            with open(pinned_file, "w") as f:
                f.write("true" if self._logs_pinned else "false")
        except Exception:
            pass

    # ── Enter key ──────────────────────────────────────────────

    def _handle_enter(self, event) -> str | None:
        if event.state & 0x1:  # Shift+Enter → newline
            return None
        self._send_message()
        return "break"

    # ── Status / Spinner ───────────────────────────────────────

    def _set_status(self, status: str) -> None:
        self.status_var.set(status)
        self.update_idletasks()

    def _start_spinner(self) -> None:
        if not self.spinner_running:
            self.spinner_running = True
            self._animate_spinner()

    def _stop_spinner(self) -> None:
        self.spinner_running = False
        self._set_status(STATUS_READY)

    def _animate_spinner(self) -> None:
        if not self.spinner_running:
            return
        self.status_var.set(f"🧠 {self.spinner_states[self.spinner_index]}")
        self.spinner_index = (self.spinner_index + 1) % len(self.spinner_states)
        self.after(500, self._animate_spinner)

    # ── Deep Thinking Toggle ───────────────────────────────────

    def _toggle_think_mode(self) -> None:
        deep = self.sidebar.deep_thinking
        history = self.backend.conversation_history
        if not history:
            return
        current = history[0]["content"]
        if deep:
            if current.startswith("/no_think "):
                history[0]["content"] = current[len("/no_think "):]
            self._log_tool("[system] Deep thinking enabled")
        else:
            if not current.startswith("/no_think "):
                history[0]["content"] = "/no_think " + current
            self._log_tool("[system] Deep thinking disabled (/no_think)")
        self.sidebar.set_think_label(deep)

    # ── Send / Process ─────────────────────────────────────────

    def _send_message(self) -> None:
        self._stop_audio()
        user_input = self.chat_tab_frame.get_input()
        if not user_input:
            return
        self.chat_tab_frame.clear_input()
        self._dispatch_message(user_input)

    def _dispatch_message(self, user_input: str) -> None:
        self.chat_tab_frame.append_message("You", user_input)
        self.chat_tab_frame.set_send_state(True)
        self._start_spinner()
        self.backend._chat_generation += 1
        threading.Thread(
            target=self._process_message, args=(user_input,), daemon=True
        ).start()

    def _process_message(self, user_input: str) -> None:
        try:
            self.after(0, lambda: self._set_status(STATUS_THINKING))
            reply = self.backend.chat(user_input)
            self.after(0, lambda: self.chat_tab_frame.append_message("Klara", reply))

            if self.sidebar.tts_enabled:
                self.after(0, lambda: self._set_status(STATUS_TTS))
                self.backend.speak_async(reply)

            self.after(0, self._auto_save)
        except CancelledError:
            self._log_tool("[system] Request cancelled, skipping response")
        except Exception as e:
            err_msg = str(e)
            self.after(0, lambda: self.chat_tab_frame.append_message("SYSTEM", err_msg))
            self.after(0, lambda: self._set_status(STATUS_ERROR))

        finally:
            self.after(0, self._finish_response)

    def _finish_response(self) -> None:
        self._stop_spinner()
        self.chat_tab_frame.set_send_state(True)
        self._set_status(STATUS_READY)

    # ── Log routing ────────────────────────────────────────────

    def _process_logs(self) -> None:
        while not gui_logger.queue.empty():
            self._route_log(gui_logger.queue.get())
        self.after(250, self._process_logs)

    def _route_log(self, msg: str) -> None:
        lower = msg.lower()
        if "[search]" in lower:
            self._set_status(STATUS_SEARCH)
            self._log_tool(msg)
        elif "[recall]" in lower:
            self._set_status(STATUS_MEMORY)
            self._log_memory(msg)
        elif "[memory]" in lower or "[analyze]" in lower:
            self._log_memory(msg)
        else:
            self._log_tool(msg)

    def _log_tool(self, text: str) -> None:
        ts = time.strftime("%H:%M:%S")
        self._tool_log.configure(state="normal")
        self._tool_log.insert("end", f"[{ts}] {text}\n")
        self._tool_log.see("end")
        self._tool_log.configure(state="disabled")
        # Also route to generate tab if visible
        if self.tabview.get() == "Generate":
            self.generate_tab_frame.log_tool(f"[{ts}] {text}")

    def _log_memory(self, text: str) -> None:
        ts = time.strftime("%H:%M:%S")
        self._memory_log.configure(state="normal")
        self._memory_log.insert("end", f"[{ts}] {text}\n")
        self._memory_log.see("end")
        self._memory_log.configure(state="disabled")

    # ── Audio ──────────────────────────────────────────────────

    def _stop_audio(self) -> None:
        try:
            self.backend.audio_interrupt.set()
            subprocess.run(["pkill", "-f", "pw-play"], check=False)
            self._log_tool("Audio stopped.")
        except Exception as e:
            self._log_tool(f"Audio stop failed: {e}")

    # ── Clear chat ─────────────────────────────────────────────

    def _clear_chat(self) -> None:
        if not messagebox.askyesno("Clear Chat", "Clear current conversation?"):
            return
        threading.Thread(target=self._do_clear_chat, daemon=True).start()

    def _do_clear_chat(self) -> None:
        self.after(0, lambda: self._set_status("Consolidating context..."))
        self.backend.consolidate_context()
        self.after(0, lambda: self.chat_tab_frame.clear_display())
        self.after(0, lambda: setattr(
            self.backend, 'conversation_history',
            self.backend.conversation_history[:3]
        ))
        self.after(0, lambda: setattr(self, 'current_file', None))
        self.after(0, lambda: self._log_tool("Conversation cleared and context refreshed."))
        self.after(0, lambda: self._set_status(STATUS_READY))

    # ── Chat file management ───────────────────────────────────

    def _save_chat(self) -> None:
        filename = filedialog.asksaveasfilename(
            initialdir=str(CHAT_DIR),
            defaultextension=".txt",
            filetypes=[("Text Files", "*.txt")],
        )
        if not filename:
            return
        try:
            content = self.chat_tab_frame.get_display_content()
            with open(filename, "w", encoding="utf-8") as f:
                f.write(content)
            self.current_file = filename
            self.sidebar.load_chat_list()
            self.sidebar.set_active_chat(filename)
            self._log_tool(f"Saved: {filename}")
        except Exception as e:
            messagebox.showerror("Save Error", str(e))

    def _toggle_memory_service(self) -> None:
        action = "stop" if self.memory_enabled else "start"
        self.chat_tab_frame.set_memory_button_state(False)
        try:
            result = subprocess.run(
                ["sudo", "-n", "systemctl", action, "clara-memory.service"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                self.memory_enabled = not self.memory_enabled
                self.mem_btn.configure(text=f"Memory: {'ON' if self.memory_enabled else 'OFF'}")
            else:
                self._log_tool(f"Memory service {action} failed: {result.stderr.strip()}")
        except subprocess.TimeoutExpired:
            self._log_tool(f"Memory service {action} timed out")
        except Exception as e:
            self._log_tool(f"Memory service {action} error: {e}")
        finally:
            self.chat_tab_frame.set_memory_button_state(True)

    def _auto_save(self) -> None:
        if not getattr(self, "autosave_enabled", True):
            return
        try:
            if self.current_file:
                filename = self.current_file
            else:
                ts = time.strftime("%Y%m%d_%H%M%S")
                filename = str(CHAT_DIR / f"chat_{ts}.txt")
                self.current_file = filename
            content = self.chat_tab_frame.get_display_content()
            with open(filename, "w", encoding="utf-8") as f:
                f.write(content)
            self.sidebar.load_chat_list()
            self.sidebar.set_active_chat(self.current_file)
        except Exception as e:
            self._log_tool(f"Autosave failed: {e}")

    def _load_selected_chat(self) -> None:
        filename = self.sidebar.get_selected_filename()
        if not filename:
            return
        path = CHAT_DIR / filename
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
            self.chat_tab_frame.clear_display()
            # Parse and append messages
            lines = content.splitlines()
            current_speaker = None
            current_text = []
            for line in lines:
                if "] You:" in line:
                    if current_speaker and current_text:
                        self.chat_tab_frame.append_message(current_speaker, "\n".join(current_text))
                    current_speaker, current_text = "You", []
                elif "] Klara:" in line:
                    if current_speaker and current_text:
                        self.chat_tab_frame.append_message(current_speaker, "\n".join(current_text))
                    current_speaker, current_text = "Klara", []
                else:
                    current_text.append(line)
            if current_speaker and current_text:
                self.chat_tab_frame.append_message(current_speaker, "\n".join(current_text))

            self.current_file = str(path)
            self.sidebar.set_active_chat(filename)
            self._rebuild_history(content)
            self._log_tool(f"Loaded: {filename}")
        except Exception as e:
            messagebox.showerror("Load Error", str(e))

    def _delete_selected_chat(self) -> None:
        filename = self.sidebar.get_selected_filename()
        if not filename:
            return
        path = CHAT_DIR / filename
        if not messagebox.askyesno("Delete Chat", f"Delete {filename}?"):
            return
        try:
            path.unlink()
            self.sidebar.load_chat_list()
            self._log_tool(f"Deleted: {filename}")
        except Exception as e:
            messagebox.showerror("Delete Error", str(e))

    def _rebuild_history(self, content: str) -> None:
        """Re-parse a saved chat file back into conversation_history."""
        self.backend.conversation_history = self.backend.conversation_history[:3]
        lines = content.splitlines()
        current_role = None
        current_text = []
        parsed = []

        for line in lines:
            if "] You:" in line:
                if current_role:
                    parsed.append((current_role, "\n".join(current_text)))
                current_role, current_text = "user", []
            elif "] Klara:" in line:
                if current_role:
                    parsed.append((current_role, "\n".join(current_text)))
                current_role, current_text = "assistant", []
            else:
                current_text.append(line)

        if current_role:
            parsed.append((current_role, "\n".join(current_text)))

        for role, text in parsed:
            text = text.strip()
            if text:
                self.backend.conversation_history.append({"role": role, "content": text})

    # ── Shutdown ───────────────────────────────────────────────

    def on_exit(self) -> None:
        if not messagebox.askyesno("Exit Klara", "Shutdown Klara?"):
            return
        self._set_status("Closing Klara...")
        try:
            self.backend.shutdown()
        except Exception as e:
            print(e)
        self.generate_tab_frame.shutdown()
        self.destroy()
