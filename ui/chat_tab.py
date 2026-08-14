"""
ui/chat_tab.py — Modern chat interface with message bubbles and styled input.
"""

import time
import tkinter as tk
import customtkinter as ctk

from ui.theme import ThemeManager


class ChatTab(ctk.CTkFrame):
    """
    Chat tab with:
      • Scrollable message area with styled bubbles
      • User messages: right-aligned, accent background
      • Klara messages: left-aligned, lighter background
      • Input area with Send button and microphone button
    """

    def __init__(self, parent, font_size=11, **kwargs):
        super().__init__(parent, **kwargs)
        self._font_size = font_size
        self._message_font = ("Inter", font_size)
        self._messages = []  # Store messages for saving
        self._msg_frames = []  # Track message frames for theme updates
        self._build()

    def append_message(self, speaker: str, message: str) -> None:
        """Add a styled message bubble to the chat."""
        ts = time.strftime("%H:%M:%S")
        is_user = speaker == "You"
        C = ThemeManager.get_current()

        # Store for saving
        self._messages.append((speaker, message, ts))

        # Create message row frame (always stacks vertically)
        msg_frame = ctk.CTkFrame(
            self.scrollable_frame,
            fg_color="transparent",
        )
        msg_frame.pack(fill="x", padx=10, pady=(4, 0))

        # Message bubble frame - sizes to content
        bubble_bg = C["bg_message_user"] if is_user else C["bg_message_klara"]
        bubble_frame = ctk.CTkFrame(
            msg_frame,
            fg_color=bubble_bg,
            corner_radius=12,
        )
        if is_user:
            bubble_frame.pack(side="right", padx=(60, 10), pady=(2, 0))
        else:
            bubble_frame.pack(side="left", padx=(10, 60), pady=(2, 0))

        # Inner frame for speaker label + message text
        inner_frame = ctk.CTkFrame(bubble_frame, fg_color="transparent")
        inner_frame.pack(fill="x", expand=True, padx=0, pady=0)

        # Speaker label + timestamp (inside bubble, top-left)
        label_color = C["fg_user"] if is_user else C["fg_clara"]
        label = ctk.CTkLabel(
            inner_frame,
            text=f"{speaker}  •  {ts}",
            font=("Inter", 9),
            text_color=label_color,
            anchor="w",
            fg_color="transparent",
        )
        label.pack(anchor="w", padx=(12, 12), pady=(8, 0))

        # Message text (native tk.Label for proper left-justified text)
        bubble = tk.Label(
            inner_frame,
            text=message,
            font=self._message_font,
            fg=C["fg"],
            bg=bubble_bg,
            justify="left",
            anchor="w",
            wraplength=500,
        )
        bubble.pack(fill="x", expand=True, padx=(12, 12), pady=(2, 8))

        # Track for theme updates
        self._msg_frames.append({
            "msg_frame": msg_frame,
            "label": label,
            "bubble_frame": bubble_frame,
            "bubble": bubble,
            "inner_frame": inner_frame,
            "is_user": is_user,
        })

        # Auto-scroll to bring new message into view at the top of the viewport
        self.after(0, self._scroll_to_bottom)

    def _scroll_to_bottom(self) -> None:
        """Scroll the chat view so the latest message appears at the top of the viewport."""
        self.scrollable_frame.update_idletasks()
        canvas = getattr(self.scrollable_frame, "_parent_canvas", None)
        if canvas is not None and self._msg_frames:
            try:
                last_msg = self._msg_frames[-1]["msg_frame"]
                msg_y = last_msg.winfo_y()
                canvas_bbox = canvas.bbox("all")
                if canvas_bbox:
                    total_height = canvas_bbox[3] - canvas_bbox[1]
                    if total_height > 0:
                        fraction = msg_y / total_height
                        canvas.yview_moveto(max(0.0, min(1.0, fraction)))
            except Exception:
                pass

    def update_theme(self) -> None:
        """Re-apply theme colors to all existing widgets."""
        C = ThemeManager.get_current()

        # Update input area
        self.action_bar.configure(fg_color="transparent")
        self.input_frame.configure(fg_color=C["bg_input"], border_color=C["border"])
        self.prompt_box.configure(text_color=C["fg"])
        self.talk_btn.configure(hover_color=C["bg_accent_hover"])
        self.stop_btn.configure(hover_color=C["accent_red"], text_color=C["accent_red"])
        self.send_btn.configure(fg_color=C["accent"], hover_color=C["bg_accent_hover"])
        self.clear_btn.configure(hover_color=C["bg_accent_hover"])
        self.memory_btn.configure(hover_color=C["bg_accent_hover"], text_color=C["fg_dim"])

        # Update all message frames
        for msg_data in self._msg_frames:
            C = ThemeManager.get_current()
            is_user = msg_data["is_user"]
            label_color = C["fg_user"] if is_user else C["fg_clara"]
            bubble_bg = C["bg_message_user"] if is_user else C["bg_message_klara"]

            msg_data["label"].configure(text_color=label_color)
            msg_data["bubble_frame"].configure(fg_color=bubble_bg)
            msg_data["bubble"].configure(fg=C["fg"], bg=bubble_bg)
            msg_data["inner_frame"].configure(fg_color="transparent")

    def clear_display(self) -> None:
        """Clear all messages from the chat."""
        for widget in self.scrollable_frame.winfo_children():
            widget.destroy()
        self._messages.clear()
        self._msg_frames.clear()

    def get_input(self) -> str:
        return self.prompt_box.get("1.0", "end-1c").strip()

    def clear_input(self) -> None:
        self.prompt_box.delete("1.0", "end")

    def get_display_content(self) -> str:
        """Get all message content for saving to file."""
        content = ""
        for speaker, message, ts in self._messages:
            content += f"[{ts}] {speaker}: {message}\n\n"
        return content

    def set_send_state(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        self.send_btn.configure(state=state)
        self.talk_btn.configure(state=state)

    def set_memory_button_text(self, text: str) -> None:
        self.memory_btn.configure(text=text)

    def set_memory_button_state(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        self.memory_btn.configure(state=state)

    def bind_send(self, callback) -> None:
        self._send_callback = callback
        self.send_btn.configure(command=callback)

    def bind_talk(self, callback) -> None:
        self.talk_btn.configure(command=callback)

    def bind_clear(self, callback) -> None:
        self.clear_btn.configure(command=callback)

    def bind_stop(self, callback) -> None:
        self.stop_btn.configure(command=callback)

    def bind_memory_toggle(self, callback) -> None:
        self.memory_btn.configure(command=callback)

    def bind_enter(self, callback) -> None:
        self.prompt_box.bind("<Return>", callback)

    def set_font_size(self, size: int) -> None:
        self._font_size = size
        self._message_font = ("Inter", size)
        self.prompt_box.configure(font=("Inter", size))

    def _build(self) -> None:
        C = ThemeManager.get_current()

        # Message display area
        self.scrollable_frame = ctk.CTkScrollableFrame(
            self,
            fg_color="transparent",
            corner_radius=0,
        )
        self.scrollable_frame.pack(fill="both", expand=True, padx=0, pady=0)

        # Action Bar frame (input + buttons in grid layout)
        self.action_bar = ctk.CTkFrame(
            self,
            fg_color="transparent",
        )
        self.action_bar.pack(fill="x", padx=10, pady=(0, 10))

        # Input area frame (spans all columns)
        self.input_frame = ctk.CTkFrame(
            self.action_bar,
            fg_color=C["bg_input"],
            corner_radius=12,
            border_width=1,
            border_color=C["border"],
        )
        self.input_frame.grid(row=0, column=0, columnspan=4, sticky="ew")

        # Text input
        self.prompt_box = ctk.CTkTextbox(
            self.input_frame,
            height=60,
            font=("Inter", self._font_size),
            fg_color="transparent",
            text_color=C["fg"],
            corner_radius=0,
            border_width=0,
            wrap="word",
        )
        self.prompt_box.pack(side="left", fill="both", expand=True, padx=10, pady=8)

        # Voice/stop buttons (right side of input)
        btn_frame = ctk.CTkFrame(self.input_frame, fg_color="transparent")
        btn_frame.pack(side="right", fill="y", padx=(0, 8), pady=4)

        self.talk_btn = ctk.CTkButton(
            btn_frame,
            text="🎤",
            width=36,
            height=36,
            fg_color="transparent",
            hover_color=C["bg_accent_hover"],
            corner_radius=18,
        )
        self.talk_btn.pack(pady=2)

        self.stop_btn = ctk.CTkButton(
            btn_frame,
            text="⏹",
            width=36,
            height=36,
            fg_color="transparent",
            hover_color=C["accent_red"],
            corner_radius=18,
            text_color=C["accent_red"],
        )
        self.stop_btn.pack(pady=2)

        # Action buttons row (below input)
        self.send_btn = ctk.CTkButton(
            self.action_bar,
            text="Send",
            font=("Inter", 11, "bold"),
            corner_radius=8,
            height=36,
            fg_color=C["accent"],
            hover_color=C["bg_accent_hover"],
        )
        self.send_btn.grid(row=1, column=3, padx=(0, 2), pady=(4, 0), sticky="e")

        self.clear_btn = ctk.CTkButton(
            self.action_bar,
            text="Clear",
            font=("Inter", 11),
            corner_radius=8,
            height=36,
            fg_color="transparent",
            hover_color=C["bg_accent_hover"],
        )
        self.clear_btn.grid(row=1, column=2, padx=2, pady=(4, 0), sticky="e")

        self.memory_btn = ctk.CTkButton(
            self.action_bar,
            text="Disable Memory",
            font=("Inter", 10),
            corner_radius=8,
            height=36,
            fg_color="transparent",
            hover_color=C["bg_accent_hover"],
            text_color=C["fg_dim"],
        )
        self.memory_btn.grid(row=1, column=1, padx=2, pady=(4, 0), sticky="e")

        self.action_bar.columnconfigure(0, weight=1)
