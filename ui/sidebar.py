"""
ui/sidebar.py — Collapsible left sidebar with conversation list and controls.
"""

import customtkinter as ctk
from pathlib import Path

from config import CHAT_DIR
from ui.theme import ThemeManager


class Sidebar(ctk.CTkFrame):
    """
    Left sidebar with:
      • Collapse/expand toggle
      • Conversation file list (scrollable with clickable items)
      • Save / Load / Delete buttons
      • TTS toggle switch
      • Deep Thinking toggle switch
    """

    def __init__(self, parent, on_toggle_think, on_toggle_theme, **kwargs):
        super().__init__(parent, **kwargs)
        self._on_toggle_think = on_toggle_think
        self._on_toggle_theme = on_toggle_theme
        self._collapsed = False
        self._font_size = 13
        self._active_file = None
        self._chat_items = []  # Track chat items for theme updates
        self._build()

    @property
    def tts_enabled(self) -> bool:
        return self.tts_var.get()

    @property
    def deep_thinking(self) -> bool:
        return self.think_var.get()

    @property
    def font_size(self) -> int:
        return self._font_size

    def set_font_size(self, size: int) -> None:
        self._font_size = size

    def load_chat_list(self) -> None:
        # Clear existing items
        for widget in self.history_container.winfo_children():
            widget.destroy()
        self._chat_items.clear()
        
        files = sorted(CHAT_DIR.glob("*.txt"), reverse=True)
        for f in files:
            self._add_chat_item(f.name)

    def _add_chat_item(self, filename: str) -> None:
        """Add a chat file item to the list."""
        C = ThemeManager.get_current()
        item_frame = ctk.CTkFrame(
            self.history_container,
            fg_color="transparent",
            height=32,
        )
        item_frame.pack(fill="x", padx=4, pady=1)
        item_frame.pack_propagate(False)

        label = ctk.CTkLabel(
            item_frame,
            text=f"  {filename}",
            font=("Inter", 10),
            text_color=C["fg"],
            anchor="w",
            wraplength=240,
        )
        label.pack(fill="both", expand=True)

        item_data = {"frame": item_frame, "label": label, "filename": filename}
        self._chat_items.append(item_data)

        # Click handler
        def _select(e, data=item_data):
            self._set_active(data["filename"])
            data["label"].configure(fg_color=C["bg_accent"], text_color=C["fg_user"])
            # Reset previous active
            for item in self._chat_items:
                if item is not data:
                    item["label"].configure(fg_color="transparent", text_color=C["fg"])
            
            if hasattr(self, '_on_chat_selected'):
                self._on_chat_selected(data["filename"])

        label.bind("<Button-1>", _select)
        label.pack_propagate(False)

    def _set_active(self, filename: str | None) -> None:
        self._active_file = filename

    def get_selected_filename(self) -> str | None:
        return self._active_file

    def set_active_chat(self, filename: str | None) -> None:
        self._set_active(filename)

    def update_theme(self) -> None:
        """Re-apply theme colors to all existing widgets."""
        C = ThemeManager.get_current()

        # Update sidebar frame
        self.configure(fg_color=C["bg"])

        # Update chat items
        for item in self._chat_items:
            item["label"].configure(text_color=C["fg"])
            # Re-check if this item is active
            if item["filename"] == self._active_file:
                item["label"].configure(fg_color=C["bg_accent"], text_color=C["fg_user"])

        # Update buttons
        self.save_btn.configure(hover_color=C["bg_accent_hover"])
        self.load_btn.configure(hover_color=C["bg_accent_hover"])
        self.delete_btn.configure(hover_color=C["accent_red"], text_color=C["accent_red"])
        self.collapse_btn.configure(hover_color=C["bg_accent_hover"])
        self.theme_btn.configure(hover_color=C["bg_accent_hover"])

        # Update labels
        self.tts_label.configure(text_color=C["fg"])
        self.think_label.configure(text_color=C["fg"])
        self.think_status.configure(text_color=C["fg_dim"])

        # Update switches
        self.tts_switch.configure(fg_color=C["accent"], text_color=C["fg"])
        self.think_switch.configure(fg_color=C["accent"], text_color=C["fg"])

    def _build(self) -> None:
        C = ThemeManager.get_current()
        self.configure(fg_color=C["bg"], width=280)
        self.pack_propagate(False)

        # Header row
        header_frame = ctk.CTkFrame(self, fg_color="transparent", height=40)
        header_frame.pack(fill="x", padx=0, pady=0)
        header_frame.pack_propagate(False)

        # Collapse button
        self.collapse_btn = ctk.CTkButton(
            header_frame,
            text="◀",
            width=30,
            height=30,
            fg_color="transparent",
            hover_color=C["bg_accent_hover"],
            corner_radius=5,
            command=self._toggle_collapse,
        )
        self.collapse_btn.pack(side="left", padx=(10, 0), pady=5)

        # Title
        self.title_label = ctk.CTkLabel(
            header_frame,
            text="Klara",
            font=("Inter", 14, "bold"),
            text_color=C["fg"],
        )
        self.title_label.pack(side="left", padx=5)

        # Theme toggle
        self.theme_var = ctk.StringVar(value="dark")
        self.theme_btn = ctk.CTkButton(
            header_frame,
            text="🌙",
            width=30,
            height=30,
            fg_color="transparent",
            hover_color=C["bg_accent_hover"],
            corner_radius=5,
            command=self._toggle_theme,
        )
        self.theme_btn.pack(side="right", padx=(0, 10), pady=5)

        # Scrollable conversation list
        self.history_container = ctk.CTkScrollableFrame(
            self,
            fg_color="transparent",
            width=260,
        )
        self.history_container.pack(fill="both", expand=True, padx=10, pady=5)

        # Action buttons
        btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        btn_frame.pack(fill="x", padx=10, pady=10)

        self.save_btn = ctk.CTkButton(
            btn_frame,
            text="💾 Save",
            font=("Inter", 11),
            corner_radius=5,
            height=32,
        )
        self.save_btn.pack(fill="x", pady=2)

        self.load_btn = ctk.CTkButton(
            btn_frame,
            text="📂 Load",
            font=("Inter", 11),
            corner_radius=5,
            height=32,
        )
        self.load_btn.pack(fill="x", pady=2)

        self.delete_btn = ctk.CTkButton(
            btn_frame,
            text="🗑 Delete",
            font=("Inter", 11),
            corner_radius=5,
            height=32,
            fg_color="transparent",
            hover_color=C["accent_red"],
            text_color=C["accent_red"],
        )
        self.delete_btn.pack(fill="x", pady=2)

        # Toggles section
        toggle_frame = ctk.CTkFrame(self, fg_color="transparent")
        toggle_frame.pack(fill="x", padx=10, pady=10)

        # TTS toggle
        self.tts_var = ctk.BooleanVar(value=True)
        tts_row = ctk.CTkFrame(toggle_frame, fg_color="transparent")
        tts_row.pack(fill="x", pady=2)

        self.tts_label = ctk.CTkLabel(
            tts_row,
            text="🔊 TTS",
            font=("Inter", 11),
            text_color=C["fg"],
            width=60,
        )
        self.tts_label.pack(side="left")

        self.tts_switch = ctk.CTkSwitch(
            tts_row,
            variable=self.tts_var,
            font=("Inter", 11),
            text_color=C["fg"],
            fg_color=C["accent"],
            switch_height=20,
            switch_width=40,
            text="",
        )
        self.tts_switch.pack(side="right", padx=5)

        # Deep thinking toggle
        think_row = ctk.CTkFrame(toggle_frame, fg_color="transparent")
        think_row.pack(fill="x", pady=2)

        self.think_label = ctk.CTkLabel(
            think_row,
            text="🧠 Think",
            font=("Inter", 11),
            text_color=C["fg"],
            width=60,
        )
        self.think_label.pack(side="left")

        self.think_var = ctk.BooleanVar(value=False)
        self.think_switch = ctk.CTkSwitch(
            think_row,
            variable=self.think_var,
            font=("Inter", 11),
            text_color=C["fg"],
            fg_color=C["accent"],
            switch_height=20,
            switch_width=40,
            command=self._on_toggle_think,
            text="",
        )
        self.think_switch.pack(side="right", padx=5)

        self.think_status = ctk.CTkLabel(
            think_row,
            text="/no_think",
            font=("Inter", 9),
            text_color=C["fg_dim"],
        )
        self.think_status.pack(side="right", padx=(0, 5))

    def set_think_label(self, deep: bool) -> None:
        if deep:
            self.think_status.configure(text="ON", text_color=ThemeManager.get_current()["accent"])
        else:
            self.think_status.configure(text="/no_think", text_color=ThemeManager.get_current()["fg_dim"])

    def _toggle_collapse(self) -> None:
        self._collapsed = not self._collapsed
        if self._collapsed:
            self.collapse_btn.configure(text="▶")
            self.configure(width=50)
        else:
            self.collapse_btn.configure(text="◀")
            self.configure(width=280)

    def _toggle_theme(self) -> None:
        current = self.theme_var.get()
        new_theme = "light" if current == "dark" else "dark"
        self.theme_var.set(new_theme)
        self.theme_btn.configure(text="☀️" if new_theme == "light" else "🌙")
        self._on_toggle_theme(new_theme)
