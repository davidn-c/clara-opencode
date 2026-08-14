"""
ui/generate_tab.py — Merged Image/Video generation tab with modern UI.
"""

import io
import shutil
import subprocess
import threading
import time
import customtkinter as ctk
from tkinter import filedialog, messagebox

from ui.theme import ThemeManager
from config import COMFYUI_HOST, IMAGE_DIR, VIDEO_DIR

try:
    from PIL import Image, ImageTk
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

MPV_AVAILABLE = shutil.which("mpv") is not None


def _numeric_row(parent, label, var, from_=0, to=100, increment=1, width=80):
    """Create a numeric input row with +/- buttons."""
    row = ctk.CTkFrame(parent, fg_color="transparent")
    row.pack(fill="x", padx=8, pady=1)
    ctk.CTkLabel(row, text=label, font=("Inter", 9), text_color=ThemeManager.get_current()["fg_dim"], width=80).pack(side="left")
    
    btn_frame = ctk.CTkFrame(row, fg_color="transparent")
    btn_frame.pack(side="left", padx=4)
    
    def _dec():
        val = max(from_, var.get() - increment)
        var.set(val)
    
    def _inc():
        val = min(to, var.get() + increment)
        var.set(val)
    
    ctk.CTkButton(btn_frame, text="−", width=24, height=24, font=("Inter", 12, "bold"),
                  corner_radius=4, fg_color=ThemeManager.get_current()["bg_dark"],
                  hover_color=ThemeManager.get_current()["bg_accent_hover"], command=_dec).pack(side="left")
    ctk.CTkEntry(btn_frame, textvariable=var, width=60, height=24).pack(side="left", padx=2)
    ctk.CTkButton(btn_frame, text="+", width=24, height=24, font=("Inter", 12, "bold"),
                  corner_radius=4, fg_color=ThemeManager.get_current()["bg_dark"],
                  hover_color=ThemeManager.get_current()["bg_accent_hover"], command=_inc).pack(side="left")
    
    return row


class GenerateTab(ctk.CTkFrame):
    """
    Merged Image/Video generation tab.
    
    Uses ctk.CTkTabview for Image/Video switching.
    Each sub-tab has: left pane (chat log + prompt + settings), right pane (output).
    """

    def __init__(self, parent, comfy_client, comfy_video_client, backend, tts_var, **kwargs):
        super().__init__(parent, **kwargs)
        self._comfy = comfy_client
        self._comfy_video = comfy_video_client
        self._backend = backend
        self._tts_var = tts_var

        self._img_pil = None
        self._img_tk = None
        self._img_generating = False
        self._vid_generating = False
        self._mpv_proc = None

        self._build()
        threading.Thread(target=self._load_comfy_options, daemon=True).start()
        threading.Thread(target=self._load_video_status, daemon=True).start()

    def _build(self) -> None:
        C = ThemeManager.get_current()
        
        # Tab view for Image / Video
        self.tabview = ctk.CTkTabview(
            self,
            fg_color=C["bg"],
            segmented_button_fg_color=C["bg_dark"],
            segmented_button_selected_color=C["accent"],
            segmented_button_selected_hover_color=C["bg_accent_hover"],
            segmented_button_unselected_color=C["bg_dark"],
            segmented_button_unselected_hover_color=C["bg_accent_hover"],
            text_color=C["fg"],
        )
        self.tabview.pack(fill="both", expand=True)

        # Image tab
        self.image_tab = self.tabview.add("Image")
        self._build_image_panel(self.image_tab)

        # Video tab
        self.video_tab = self.tabview.add("Video")
        self._build_video_panel(self.video_tab)

    def _build_image_panel(self, parent) -> None:
        C = ThemeManager.get_current()

        pane = ctk.CTkFrame(parent, fg_color="transparent")
        pane.pack(fill="both", expand=True)

        # Left pane
        left = ctk.CTkFrame(pane, fg_color="transparent", width=400)
        left.pack(side="left", fill="both", padx=(0, 2))
        left.pack_propagate(False)

        # Chat display
        self._img_chat = ctk.CTkTextbox(
            left, font=("Inter", 10), fg_color=C["bg_dark"],
            text_color=C["fg"], corner_radius=8, state="disabled",
        )
        self._img_chat.pack(fill="both", expand=True, padx=8, pady=(8, 4))

        # Prompt input
        self._img_prompt = ctk.CTkTextbox(
            left, height=80, font=("Inter", 10),
            fg_color=C["bg_input"], text_color=C["fg"],
            corner_radius=8, wrap="word",
        )
        self._img_prompt.pack(fill="x", padx=8, pady=(0, 4))

        # ComfyUI Load/Unload buttons
        self._img_comfy_btn_frame = ctk.CTkFrame(left, fg_color="transparent")
        self._img_comfy_btn_frame.pack(fill="x", padx=8, pady=(0, 4))

        self._img_load_btn = ctk.CTkButton(
            self._img_comfy_btn_frame, text="▶ Load ComfyUI",
            font=("Inter", 10), corner_radius=6, height=28,
            fg_color=C["accent"], hover_color=C["bg_accent_hover"],
            command=self._img_load_comfyui,
        )
        self._img_load_btn.pack(side="left", padx=2)

        self._img_unload_btn = ctk.CTkButton(
            self._img_comfy_btn_frame, text="⏹ Unload ComfyUI",
            font=("Inter", 10), corner_radius=6, height=28,
            fg_color="transparent", text_color=C["accent_red"],
            hover_color=C["bg_accent_hover"],
            command=self._img_unload_comfyui,
        )
        self._img_unload_btn.pack(side="left", padx=2)

        # Generation buttons
        btn_frame = ctk.CTkFrame(left, fg_color="transparent")
        btn_frame.pack(fill="x", padx=8, pady=(0, 4))

        self._img_send_btn = ctk.CTkButton(btn_frame, text="Send", command=self._img_send,
                      font=("Inter", 11), corner_radius=6, height=32).pack(side="left", padx=2)
        self._img_clear_btn = ctk.CTkButton(btn_frame, text="Clear", command=self._img_clear,
                      font=("Inter", 11), corner_radius=6, height=32,
                      fg_color="transparent").pack(side="left", padx=2)
        self._img_stop_btn = ctk.CTkButton(btn_frame, text="Stop", command=self._img_stop,
                      font=("Inter", 11), corner_radius=6, height=32,
                      fg_color="transparent", text_color=C["accent_red"]).pack(side="right", padx=2)

        # Settings
        self._build_image_settings(left)

        # Right pane - image display
        self._img_right_frame = ctk.CTkFrame(pane, fg_color=C["bg_dark"])
        self._img_right_frame.pack(side="right", fill="both", expand=True, padx=2)

        ctk.CTkLabel(self._img_right_frame, text="Generated Image", font=("Inter", 12, "bold"),
                     text_color=C["fg"], anchor="w").pack(fill="x", padx=12, pady=8)

        self._img_canvas = ctk.CTkCanvas(self._img_right_frame, bg=C["mono_bg"], highlightthickness=0)
        self._img_canvas.pack(fill="both", expand=True, padx=8, pady=8)

        act_frame = ctk.CTkFrame(self._img_right_frame, fg_color="transparent")
        act_frame.pack(fill="x", padx=8, pady=4)

        self._img_save_btn = ctk.CTkButton(act_frame, text="💾 Save", command=self._img_save,
                      font=("Inter", 10), corner_radius=6, height=28).pack(side="left", padx=2)
        self._img_folder_btn = ctk.CTkButton(act_frame, text="📁 Folder", command=self._img_open_folder,
                      font=("Inter", 10), corner_radius=6, height=28,
                      fg_color="transparent").pack(side="left", padx=2)

        self._img_size_label = ctk.CTkLabel(act_frame, text="", font=("Inter", 9), text_color=C["fg_dim"])
        self._img_size_label.pack(side="right", padx=8)

    def _build_image_settings(self, parent) -> None:
        C = ThemeManager.get_current()
        self._img_settings_open = False

        self._img_toggle_btn = ctk.CTkButton(
            parent, text="▶ Generation Settings", font=("Inter", 10),
            corner_radius=6, height=28, fg_color="transparent",
            hover_color=C["bg_accent_hover"], command=self._img_toggle_settings,
        )
        self._img_toggle_btn.pack(fill="x", padx=8, pady=4)

        self._img_settings_frame = ctk.CTkFrame(parent, fg_color=C["bg_panel"], corner_radius=8)

        # Checkpoint
        row = ctk.CTkFrame(self._img_settings_frame, fg_color="transparent")
        row.pack(fill="x", padx=8, pady=2)
        ctk.CTkLabel(row, text="Checkpoint", font=("Inter", 9), text_color=C["fg_dim"], width=80).pack(side="left")
        self._img_ckpt_var = ctk.StringVar(value="Loading...")
        self._img_ckpt_cb = ctk.CTkOptionMenu(row, values=[], variable=self._img_ckpt_var, width=150, height=24)
        self._img_ckpt_cb.pack(side="left", padx=4)

        # Negative prompt
        row = ctk.CTkFrame(self._img_settings_frame, fg_color="transparent")
        row.pack(fill="x", padx=8, pady=2)
        ctk.CTkLabel(row, text="Negative", font=("Inter", 9), text_color=C["fg_dim"], width=80).pack(side="left")
        self._img_neg = ctk.CTkTextbox(row, height=40, font=("Inter", 9), fg_color=C["bg_input"],
                                       text_color=C["fg"], corner_radius=4)
        self._img_neg.insert("1.0", "blurry, bad quality, watermark, text, signature, low resolution, deformed, ugly")
        self._img_neg.pack(side="left", fill="x", expand=True, padx=4)

        # Width/Height/Steps
        self._img_width_var = ctk.IntVar(value=1024)
        _numeric_row(self._img_settings_frame, "Width", self._img_width_var, 64, 2048, 64)
        
        self._img_height_var = ctk.IntVar(value=1024)
        _numeric_row(self._img_settings_frame, "Height", self._img_height_var, 64, 2048, 64)
        
        self._img_steps_var = ctk.IntVar(value=20)
        _numeric_row(self._img_settings_frame, "Steps", self._img_steps_var, 1, 150, 1)

        # CFG
        row = ctk.CTkFrame(self._img_settings_frame, fg_color="transparent")
        row.pack(fill="x", padx=8, pady=1)
        ctk.CTkLabel(row, text="CFG", font=("Inter", 9), text_color=C["fg_dim"], width=80).pack(side="left")
        self._img_cfg_var = ctk.DoubleVar(value=7.0)
        
        def _cfg_dec():
            self._img_cfg_var.set(max(1.0, self._img_cfg_var.get() - 0.5))
        def _cfg_inc():
            self._img_cfg_var.set(min(20.0, self._img_cfg_var.get() + 0.5))
        
        btn_frame = ctk.CTkFrame(row, fg_color="transparent")
        btn_frame.pack(side="left", padx=4)
        self._img_cfg_dec_btn = ctk.CTkButton(btn_frame, text="−", width=24, height=24, font=("Inter", 12, "bold"),
                      corner_radius=4, fg_color=C["bg_dark"], hover_color=C["bg_accent_hover"],
                      command=_cfg_dec).pack(side="left")
        self._img_cfg_entry = ctk.CTkEntry(btn_frame, textvariable=self._img_cfg_var, width=60, height=24).pack(side="left", padx=2)
        self._img_cfg_inc_btn = ctk.CTkButton(btn_frame, text="+", width=24, height=24, font=("Inter", 12, "bold"),
                      corner_radius=4, fg_color=C["bg_dark"], hover_color=C["bg_accent_hover"],
                      command=_cfg_inc).pack(side="left")

        # Seed
        self._img_seed_var = ctk.IntVar(value=-1)
        _numeric_row(self._img_settings_frame, "Seed (-1=random)", self._img_seed_var, -1, 999999, 1)

        # Sampler/Scheduler
        for label, attr, default in [
            ("Sampler", "_img_sampler_var", "euler"),
            ("Scheduler", "_img_scheduler_var", "normal"),
        ]:
            row = ctk.CTkFrame(self._img_settings_frame, fg_color="transparent")
            row.pack(fill="x", padx=8, pady=1)
            ctk.CTkLabel(row, text=label, font=("Inter", 9), text_color=C["fg_dim"], width=80).pack(side="left")
            var = ctk.StringVar(value=default)
            setattr(self, attr, var)
            cb = ctk.CTkOptionMenu(row, values=[], variable=var, width=100, height=24)
            # attr is like "_img_sampler_var", we want "_img_sampler_cb"
            short_name = attr.replace("_img_", "").replace("_var", "")
            setattr(self, f"_img_{short_name}_cb", cb)
            cb.pack(side="left", padx=4)

        # Progress
        self._img_progress_var = ctk.DoubleVar(value=0)
        ctk.CTkProgressBar(self._img_settings_frame, variable=self._img_progress_var,
                          corner_radius=4, height=8).pack(fill="x", padx=8, pady=4)

    def _img_toggle_settings(self) -> None:
        if self._img_settings_open:
            self._img_settings_frame.pack_forget()
            self._img_toggle_btn.configure(text="▶ Generation Settings")
            self._img_settings_open = False
        else:
            self._img_settings_frame.pack(fill="x", padx=8, pady=(0, 8))
            self._img_toggle_btn.configure(text="▼ Generation Settings")
            self._img_settings_open = True

    def _build_video_panel(self, parent) -> None:
        C = ThemeManager.get_current()

        pane = ctk.CTkFrame(parent, fg_color="transparent")
        pane.pack(fill="both", expand=True)

        # Left pane
        left = ctk.CTkFrame(pane, fg_color="transparent", width=400)
        left.pack(side="left", fill="both", padx=(0, 2))
        left.pack_propagate(False)

        self._vid_chat = ctk.CTkTextbox(left, font=("Inter", 10), fg_color=C["bg_dark"],
                                        text_color=C["fg"], corner_radius=8, state="disabled")
        self._vid_chat.pack(fill="both", expand=True, padx=8, pady=(8, 4))

        self._vid_prompt = ctk.CTkTextbox(left, height=80, font=("Inter", 10),
                                          fg_color=C["bg_input"], text_color=C["fg"],
                                          corner_radius=8, wrap="word")
        self._vid_prompt.pack(fill="x", padx=8, pady=(0, 4))

        # ComfyUI Load/Unload buttons
        self._vid_comfy_btn_frame = ctk.CTkFrame(left, fg_color="transparent")
        self._vid_comfy_btn_frame.pack(fill="x", padx=8, pady=(0, 4))

        self._vid_load_btn = ctk.CTkButton(
            self._vid_comfy_btn_frame, text="▶ Load ComfyUI",
            font=("Inter", 10), corner_radius=6, height=28,
            fg_color=C["accent"], hover_color=C["bg_accent_hover"],
            command=self._vid_load_comfyui,
        )
        self._vid_load_btn.pack(side="left", padx=2)

        self._vid_unload_btn = ctk.CTkButton(
            self._vid_comfy_btn_frame, text="⏹ Unload ComfyUI",
            font=("Inter", 10), corner_radius=6, height=28,
            fg_color="transparent", text_color=C["accent_red"],
            hover_color=C["bg_accent_hover"],
            command=self._vid_unload_comfyui,
        )
        self._vid_unload_btn.pack(side="left", padx=2)

        # Generation buttons
        btn_frame = ctk.CTkFrame(left, fg_color="transparent")
        btn_frame.pack(fill="x", padx=8, pady=(0, 4))

        self._vid_send_btn = ctk.CTkButton(btn_frame, text="Send", command=self._vid_send,
                      font=("Inter", 11), corner_radius=6, height=32).pack(side="left", padx=2)
        self._vid_clear_btn = ctk.CTkButton(btn_frame, text="Clear", command=self._vid_clear,
                      font=("Inter", 11), corner_radius=6, height=32,
                      fg_color="transparent").pack(side="left", padx=2)
        self._vid_stop_btn = ctk.CTkButton(btn_frame, text="Stop", command=self._vid_stop,
                      font=("Inter", 11), corner_radius=6, height=32,
                      fg_color="transparent", text_color=C["accent_red"]).pack(side="right", padx=2)

        self._build_video_settings(left)

        # Right pane - video player
        self._vid_right_frame = ctk.CTkFrame(pane, fg_color=C["bg_dark"])
        self._vid_right_frame.pack(side="right", fill="both", expand=True, padx=2)

        ctk.CTkLabel(self._vid_right_frame, text="Generated Video", font=("Inter", 12, "bold"),
                     text_color=C["fg"], anchor="w").pack(fill="x", padx=12, pady=8)

        self._vid_player_frame = ctk.CTkFrame(self._vid_right_frame, fg_color="black")
        self._vid_player_frame.pack(fill="both", expand=True, padx=8, pady=8)

        if not MPV_AVAILABLE:
            ctk.CTkLabel(self._vid_player_frame,
                        text="mpv not found — install mpv to preview video here.",
                        font=("Inter", 9), text_color="#999999").pack(expand=True)

        act_frame = ctk.CTkFrame(self._vid_right_frame, fg_color="transparent")
        act_frame.pack(fill="x", padx=8, pady=4)

        self._vid_save_btn = ctk.CTkButton(act_frame, text="💾 Save", command=self._vid_save,
                      font=("Inter", 10), corner_radius=6, height=28).pack(side="left", padx=2)
        self._vid_folder_btn = ctk.CTkButton(act_frame, text="📁 Folder", command=self._vid_open_folder,
                      font=("Inter", 10), corner_radius=6, height=28,
                      fg_color="transparent").pack(side="left", padx=2)
        self._vid_replay_btn = ctk.CTkButton(act_frame, text="🔁 Replay", command=self._vid_replay,
                      font=("Inter", 10), corner_radius=6, height=28,
                      fg_color="transparent").pack(side="left", padx=2)

        self._vid_info_label = ctk.CTkLabel(act_frame, text="", font=("Inter", 9), text_color=C["fg_dim"])
        self._vid_info_label.pack(side="right", padx=8)

    def _build_video_settings(self, parent) -> None:
        C = ThemeManager.get_current()
        self._vid_settings_open = False

        self._vid_toggle_btn = ctk.CTkButton(
            parent, text="▶ Generation Settings", font=("Inter", 10),
            corner_radius=6, height=28, fg_color="transparent",
            hover_color=C["bg_accent_hover"], command=self._vid_toggle_settings,
        )
        self._vid_toggle_btn.pack(fill="x", padx=8, pady=4)

        self._vid_settings_frame = ctk.CTkFrame(parent, fg_color=C["bg_panel"], corner_radius=8)

        # Negative prompt
        row = ctk.CTkFrame(self._vid_settings_frame, fg_color="transparent")
        row.pack(fill="x", padx=8, pady=2)
        ctk.CTkLabel(row, text="Negative", font=("Inter", 9), text_color=C["fg_dim"], width=80).pack(side="left")
        self._vid_neg = ctk.CTkTextbox(row, height=40, font=("Inter", 9), fg_color=C["bg_input"],
                                       text_color=C["fg"], corner_radius=4)
        self._vid_neg.pack(side="left", fill="x", expand=True, padx=4)

        # Parameters
        self._vid_width_var = ctk.IntVar(value=640)
        _numeric_row(self._vid_settings_frame, "Width", self._vid_width_var, 64, 1280, 64)
        
        self._vid_height_var = ctk.IntVar(value=640)
        _numeric_row(self._vid_settings_frame, "Height", self._vid_height_var, 64, 1280, 64)
        
        self._vid_duration_var = ctk.IntVar(value=5)
        _numeric_row(self._vid_settings_frame, "Duration (s)", self._vid_duration_var, 1, 30, 1)
        
        self._vid_fps_var = ctk.IntVar(value=16)
        _numeric_row(self._vid_settings_frame, "FPS", self._vid_fps_var, 8, 30, 1)
        
        self._vid_steps_var = ctk.IntVar(value=20)
        _numeric_row(self._vid_settings_frame, "Steps", self._vid_steps_var, 1, 50, 1)

        # Seed
        self._vid_seed_var = ctk.IntVar(value=-1)
        _numeric_row(self._vid_settings_frame, "Seed (-1=random)", self._vid_seed_var, -1, 999999, 1)

        # Lightning LoRA
        row = ctk.CTkFrame(self._vid_settings_frame, fg_color="transparent")
        row.pack(fill="x", padx=8, pady=2)
        self._vid_lightning_var = ctk.BooleanVar(value=False)
        self._vid_lightning_cb = ctk.CTkCheckBox(row, text="Lightning LoRA (4-step fast)",
                       variable=self._vid_lightning_var, font=("Inter", 9)).pack(side="left")

        # CFG
        row = ctk.CTkFrame(self._vid_settings_frame, fg_color="transparent")
        row.pack(fill="x", padx=8, pady=1)
        ctk.CTkLabel(row, text="CFG", font=("Inter", 9), text_color=C["fg_dim"], width=80).pack(side="left")
        self._vid_cfg_var = ctk.DoubleVar(value=3.5)
        
        def _vid_cfg_dec():
            self._vid_cfg_var.set(max(0.5, self._vid_cfg_var.get() - 0.5))
        def _vid_cfg_inc():
            self._vid_cfg_var.set(min(10.0, self._vid_cfg_var.get() + 0.5))
        
        btn_frame = ctk.CTkFrame(row, fg_color="transparent")
        btn_frame.pack(side="left", padx=4)
        self._vid_cfg_dec_btn = ctk.CTkButton(btn_frame, text="−", width=24, height=24, font=("Inter", 12, "bold"),
                      corner_radius=4, fg_color=C["bg_dark"], hover_color=C["bg_accent_hover"],
                      command=_vid_cfg_dec).pack(side="left")
        self._vid_cfg_entry = ctk.CTkEntry(btn_frame, textvariable=self._vid_cfg_var, width=60, height=24).pack(side="left", padx=2)
        self._vid_cfg_inc_btn = ctk.CTkButton(btn_frame, text="+", width=24, height=24, font=("Inter", 12, "bold"),
                      corner_radius=4, fg_color=C["bg_dark"], hover_color=C["bg_accent_hover"],
                      command=_vid_cfg_inc).pack(side="left")

        # Progress
        self._vid_progress_var = ctk.DoubleVar(value=0)
        ctk.CTkProgressBar(self._vid_settings_frame, variable=self._vid_progress_var,
                          corner_radius=4, height=8).pack(fill="x", padx=8, pady=4)

    def _vid_toggle_settings(self) -> None:
        if self._vid_settings_open:
            self._vid_settings_frame.pack_forget()
            self._vid_toggle_btn.configure(text="▶ Generation Settings")
            self._vid_settings_open = False
        else:
            self._vid_settings_frame.pack(fill="x", padx=8, pady=(0, 8))
            self._vid_toggle_btn.configure(text="▼ Generation Settings")
            self._vid_settings_open = True

    # ── Image handlers ─────────────────────────────────────────

    def _img_log(self, msg: str) -> None:
        def _do():
            self._img_chat.configure(state="normal")
            self._img_chat.insert("end", f"{msg}\n")
            self._img_chat.see("end")
            self._img_chat.configure(state="disabled")
        self.after(0, _do)

    def log_tool(self, msg: str) -> None:
        if self.tabview.get() == "Image":
            self._img_log(msg)
        else:
            self._vid_log(msg)

    def _img_send(self) -> None:
        prompt = self._img_prompt.get("1.0", "end-1c").strip()
        if not prompt or self._img_generating:
            return
        if not self._backend.is_comfyui_running():
            self._img_log("[tool] ComfyUI not loaded — click Load ComfyUI first")
            messagebox.showwarning("ComfyUI Not Loaded", "Please click Load ComfyUI before generating.")
            return
        self._img_chat.configure(state="normal")
        self._img_chat.insert("end", f"\n[You]: {prompt}\n")
        self._img_chat.see("end")
        self._img_chat.configure(state="disabled")
        self._img_prompt.delete("1.0", "end")
        self._start_image_gen(prompt)

    def _img_clear(self) -> None:
        self._img_prompt.delete("1.0", "end")

    def _img_stop(self) -> None:
        self._comfy.cancel()
        self._img_generating = False
        self._img_progress_var.set(0)
        self._img_log("[tool] ComfyUI: generation cancelled")

    def _start_image_gen(self, prompt: str) -> None:
        if self._img_generating:
            return
        if not self._backend.is_comfyui_running():
            self._img_log("[tool] ComfyUI stopped during generation — please reload")
            messagebox.showerror("ComfyUI Stopped", "ComfyUI stopped unexpectedly. Please reload.")
            return
        self._img_generating = True
        neg = self._img_neg.get("1.0", "end-1c").strip()
        ckpt = self._img_ckpt_var.get()

        self._img_log("[tool] ComfyUI: Generating image...")
        self._img_progress_var.set(5)

        def _progress(msg, pct):
            self.after(0, lambda m=msg, p=pct: (
                self._img_log(f"[tool] ComfyUI: {m}"),
                self._img_progress_var.set(p),
            ))

        def _done(img_data, w, h, path):
            self.after(0, lambda: self._img_done(img_data, w, h, path))

        def _error(msg):
            self.after(0, lambda: self._img_error(msg))

        self._comfy.generate(
            positive_prompt=prompt, negative_prompt=neg,
            checkpoint=ckpt if "No " not in ckpt and "Error" not in ckpt else None,
            width=self._img_width_var.get(), height=self._img_height_var.get(),
            steps=self._img_steps_var.get(), cfg=self._img_cfg_var.get(),
            sampler=self._img_sampler_var.get(), scheduler=self._img_scheduler_var.get(),
            seed=self._img_seed_var.get(), progress_callback=_progress,
            done_callback=_done, error_callback=_error,
        )

    def _img_done(self, img_data, w, h, path) -> None:
        self._img_generating = False
        self._img_progress_var.set(100)
        self._img_log(f"[tool] ComfyUI: Image received ({w}×{h})")
        self._img_chat.configure(state="normal")
        self._img_chat.insert("end", f"\n[Klara]: Here is your image.\n")
        self._img_chat.see("end")
        self._img_chat.configure(state="disabled")
        self._img_size_label.configure(text=f"{w}×{h}")
        self._comfy.last_image_path = path
        self._display_image(img_data)

    def _img_error(self, msg: str) -> None:
        self._img_generating = False
        self._img_progress_var.set(0)
        self._img_log(f"[tool] ComfyUI: Error — {msg}")
        self._img_chat.configure(state="normal")
        self._img_chat.insert("end", f"\n[Klara]: Image generation failed: {msg}\n")
        self._img_chat.see("end")
        self._img_chat.configure(state="disabled")

    def _display_image(self, img_data: bytes) -> None:
        if not PIL_AVAILABLE:
            self._img_log("[tool] PIL not installed; cannot display image.")
            return
        try:
            pil_img = Image.open(io.BytesIO(img_data))
            self._render_image(pil_img)
        except Exception as e:
            self._img_log(f"[tool] Image render error: {e}")

    def _render_image(self, pil_img) -> None:
        cw = self._img_canvas.winfo_width() or 500
        ch = self._img_canvas.winfo_height() or 500
        img_copy = pil_img.copy()
        img_copy.thumbnail((cw, ch), Image.LANCZOS)
        self._img_pil = img_copy
        self._img_tk = ImageTk.PhotoImage(img_copy)
        self._img_canvas.delete("all")
        self._img_canvas.create_image(cw // 2, ch // 2, anchor="center", image=self._img_tk)
        self._img_canvas.bind("<Configure>", lambda e: self._render_image(pil_img))

    def _img_save(self) -> None:
        path = self._comfy.last_image_path
        if not path:
            messagebox.showinfo("No Image", "Generate an image first.")
            return
        dest = filedialog.asksaveasfilename(
            initialdir=str(IMAGE_DIR), defaultextension=".png",
            filetypes=[("PNG Images", "*.png"), ("All", "*.*")],
        )
        if dest:
            shutil.copy2(path, dest)
            self._img_log(f"[tool] Image saved: {dest}")

    def _img_open_folder(self) -> None:
        try:
            subprocess.Popen(["xdg-open", str(IMAGE_DIR)])
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def _img_load_comfyui(self) -> None:
        if self._backend.is_comfyui_running():
            self._img_log("[tool] ComfyUI already running")
            return
        self._backend.start_comfyui()
        self._img_log("[tool] ComfyUI loading...")
        self._img_load_btn.configure(state="disabled")
        # Poll for readiness
        def _check():
            if self._backend.is_comfyui_running():
                self.after(0, lambda: (
                    self._img_log("[tool] ComfyUI loaded and ready"),
                    self._img_load_btn.configure(state="normal"),
                ))
            else:
                self.after(2000, _check)
        self.after(2000, _check)

    def _img_unload_comfyui(self) -> None:
        if not self._backend.is_comfyui_running():
            self._img_log("[tool] ComfyUI not running")
            return
        self._backend.stop_comfyui()
        self._img_log("[tool] ComfyUI unloaded")
        self._img_load_btn.configure(state="normal")

    # ── Video handlers ─────────────────────────────────────────

    def _vid_save(self) -> None:
        path = self._comfy_video.last_video_path
        if not path:
            messagebox.showinfo("No Video", "Generate a video first.")
            return
        ext = path.rsplit(".", 1)[-1] if "." in path else "mp4"
        dest = filedialog.asksaveasfilename(
            initialdir=str(VIDEO_DIR), defaultextension=f".{ext}",
            filetypes=[("Video", f"*.{ext}"), ("All", "*.*")],
        )
        if dest:
            shutil.copy2(path, dest)
            self._vid_log(f"[tool] Video saved: {dest}")

    def _vid_open_folder(self) -> None:
        try:
            subprocess.Popen(["xdg-open", str(VIDEO_DIR)])
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def _vid_log(self, msg: str) -> None:
        def _do():
            self._vid_chat.configure(state="normal")
            self._vid_chat.insert("end", f"{msg}\n")
            self._vid_chat.see("end")
            self._vid_chat.configure(state="disabled")
        self.after(0, _do)

    def _vid_send(self) -> None:
        prompt = self._vid_prompt.get("1.0", "end-1c").strip()
        if not prompt or self._vid_generating:
            return
        if not self._backend.is_comfyui_running():
            self._vid_log("[tool] ComfyUI not loaded — click Load ComfyUI first")
            messagebox.showwarning("ComfyUI Not Loaded", "Please click Load ComfyUI before generating.")
            return
        self._vid_chat.configure(state="normal")
        self._vid_chat.insert("end", f"\n[You]: {prompt}\n")
        self._vid_chat.see("end")
        self._vid_chat.configure(state="disabled")
        self._vid_prompt.delete("1.0", "end")
        self._start_video_gen(prompt)

    def _vid_clear(self) -> None:
        self._vid_prompt.delete("1.0", "end")

    def _vid_stop(self) -> None:
        self._comfy_video.cancel()
        self._vid_generating = False
        self._vid_progress_var.set(0)
        self._vid_log("[tool] ComfyUI: video generation cancelled")

    def _start_video_gen(self, prompt: str) -> None:
        if self._vid_generating:
            return
        if not self._backend.is_comfyui_running():
            self._vid_log("[tool] ComfyUI stopped during generation — please reload")
            messagebox.showerror("ComfyUI Stopped", "ComfyUI stopped unexpectedly. Please reload.")
            return
        self._vid_generating = True
        neg = self._vid_neg.get("1.0", "end-1c").strip()

        self._vid_log("[tool] ComfyUI: Generating video...")
        self._vid_progress_var.set(5)

        def _progress(msg, pct):
            self.after(0, lambda m=msg, p=pct: (
                self._vid_log(f"[tool] ComfyUI: {m}"),
                self._vid_progress_var.set(p),
            ))

        def _done(path):
            self.after(0, lambda: self._vid_done(path))

        def _error(msg):
            self.after(0, lambda: self._vid_error(msg))

        self._comfy_video.generate(
            positive_prompt=prompt, negative_prompt=neg,
            width=self._vid_width_var.get(), height=self._vid_height_var.get(),
            duration_seconds=self._vid_duration_var.get(), fps=self._vid_fps_var.get(),
            seed=self._vid_seed_var.get(), use_lightning_lora=self._vid_lightning_var.get(),
            steps=self._vid_steps_var.get(), cfg=self._vid_cfg_var.get(),
            progress_callback=_progress, done_callback=_done, error_callback=_error,
        )

    def _vid_done(self, path: str) -> None:
        self._vid_generating = False
        self._vid_progress_var.set(100)
        self._vid_log(f"[tool] ComfyUI: Video received ({path})")
        self._vid_chat.configure(state="normal")
        self._vid_chat.insert("end", f"\n[Klara]: Here is your video.\n")
        self._vid_chat.see("end")
        self._vid_chat.configure(state="disabled")
        self._vid_info_label.configure(text=f"{self._vid_duration_var.get()}s @ {self._vid_fps_var.get()}fps")
        self._comfy_video.last_video_path = path
        self._play_video(path)

    def _vid_error(self, msg: str) -> None:
        self._vid_generating = False
        self._vid_progress_var.set(0)
        self._vid_log(f"[tool] ComfyUI: Error — {msg}")
        self._vid_chat.configure(state="normal")
        self._vid_chat.insert("end", f"\n[Klara]: Video generation failed: {msg}\n")
        self._vid_chat.see("end")
        self._vid_chat.configure(state="disabled")

    def _vid_load_comfyui(self) -> None:
        if self._backend.is_comfyui_running():
            self._vid_log("[tool] ComfyUI already running")
            return
        self._backend.start_comfyui()
        self._vid_log("[tool] ComfyUI loading...")
        self._vid_load_btn.configure(state="disabled")
        def _check():
            if self._backend.is_comfyui_running():
                self.after(0, lambda: (
                    self._vid_log("[tool] ComfyUI loaded and ready"),
                    self._vid_load_btn.configure(state="normal"),
                ))
            else:
                self.after(2000, _check)
        self.after(2000, _check)

    def _vid_unload_comfyui(self) -> None:
        if not self._backend.is_comfyui_running():
            self._vid_log("[tool] ComfyUI not running")
            return
        self._backend.stop_comfyui()
        self._vid_log("[tool] ComfyUI unloaded")
        self._vid_load_btn.configure(state="normal")

    def _play_video(self, path: str) -> None:
        if not MPV_AVAILABLE:
            return
        self._stop_player()
        try:
            wid = self._vid_player_frame.winfo_id()
            self._mpv_proc = subprocess.Popen([
                "mpv", f"--wid={wid}", "--loop-file=no", "--osc=yes",
                "--keep-open=yes", "--really-quiet", path,
            ])
        except Exception as e:
            self._vid_log(f"[tool] mpv launch failed: {e}")

    def _vid_replay(self) -> None:
        path = self._comfy_video.last_video_path
        if not path:
            messagebox.showinfo("No Video", "Generate a video first.")
            return
        self._play_video(path)

    def _stop_player(self) -> None:
        if self._mpv_proc is not None and self._mpv_proc.poll() is None:
            try:
                self._mpv_proc.terminate()
                self._mpv_proc.wait(timeout=2)
            except Exception:
                try:
                    self._mpv_proc.kill()
                except Exception:
                    pass
        self._mpv_proc = None

    def shutdown(self) -> None:
        self._stop_player()

    # ── Load options ───────────────────────────────────────────

    def _load_comfy_options(self) -> None:
        try:
            if not self._backend.is_comfyui_running():
                self.after(0, lambda: self._img_ckpt_var.set("ComfyUI not running"))
                self._img_log(f"[tool] ComfyUI: server not found at {COMFYUI_HOST}")
                return
            if not self._comfy.is_running():
                self.after(0, lambda: self._img_ckpt_var.set("ComfyUI not running"))
                self._img_log(f"[tool] ComfyUI: server not found at {COMFYUI_HOST}")
                return
            checkpoints = self._comfy.get_checkpoints()
            samplers, schedulers = self._comfy.get_samplers()

            def _set():
                if checkpoints:
                    self._img_ckpt_cb["values"] = checkpoints
                    self._img_ckpt_var.set(checkpoints[0])
                else:
                    self._img_ckpt_var.set("No checkpoints found")
                if samplers:
                    self._img_sampler_cb["values"] = samplers
                    self._img_sampler_var.set("euler" if "euler" in samplers else samplers[0])
                if schedulers:
                    self._img_scheduler_cb["values"] = schedulers
                    self._img_scheduler_var.set("normal" if "normal" in schedulers else schedulers[0])
                self._img_log("[tool] ComfyUI: loaded options")

            self.after(0, _set)
        except Exception as e:
            self.after(0, lambda: self._img_ckpt_var.set("Error"))
            self._img_log(f"[tool] ComfyUI: option load failed: {e}")

    def _load_video_status(self) -> None:
        try:
            if not self._backend.is_comfyui_running():
                self._vid_log(f"[tool] ComfyUI: server not found at {COMFYUI_HOST}")
            elif not self._comfy_video.is_running():
                self._vid_log(f"[tool] ComfyUI: server not found at {COMFYUI_HOST}")
            else:
                self._vid_log("[tool] ComfyUI: video client ready")
        except Exception as e:
            self._vid_log(f"[tool] ComfyUI: status check failed: {e}")

    def update_theme(self) -> None:
        """Re-apply theme colors to all existing widgets."""
        C = ThemeManager.get_current()

        # Tabview
        self.tabview.configure(fg_color=C["bg"],
            segmented_button_fg_color=C["bg_dark"],
            segmented_button_selected_color=C["accent"],
            segmented_button_selected_hover_color=C["bg_accent_hover"],
            segmented_button_unselected_color=C["bg_dark"],
            segmented_button_unselected_hover_color=C["bg_accent_hover"],
            text_color=C["fg"])

        # Image panel
        self._img_chat.configure(fg_color=C["bg_dark"], text_color=C["fg"])
        self._img_prompt.configure(fg_color=C["bg_input"], text_color=C["fg"])
        self._img_stop_btn.configure(text_color=C["accent_red"])
        self._img_settings_frame.configure(fg_color=C["bg_panel"])
        self._img_toggle_btn.configure(hover_color=C["bg_accent_hover"])
        self._img_right_frame.configure(fg_color=C["bg_dark"])
        self._img_canvas.configure(bg=C["mono_bg"])
        self._img_size_label.configure(text_color=C["fg_dim"])

        # Image settings - update labels and entries
        for widget in self._img_settings_frame.winfo_children():
            for child in widget.winfo_children() if hasattr(widget, 'winfo_children') else []:
                if isinstance(child, ctk.CTkLabel):
                    child.configure(text_color=C["fg_dim"])
                elif isinstance(child, ctk.CTkTextbox):
                    child.configure(fg_color=C["bg_input"], text_color=C["fg"])
                elif isinstance(child, ctk.CTkEntry):
                    pass  # Entry colors handled by customtkinter theme

        # Image button colors
        if hasattr(self, '_img_cfg_dec_btn') and self._img_cfg_dec_btn:
            self._img_cfg_dec_btn.configure(fg_color=C["bg_dark"], hover_color=C["bg_accent_hover"])
        if hasattr(self, '_img_cfg_inc_btn') and self._img_cfg_inc_btn:
            self._img_cfg_inc_btn.configure(fg_color=C["bg_dark"], hover_color=C["bg_accent_hover"])

        # ComfyUI Load/Unload buttons
        if hasattr(self, '_img_load_btn') and self._img_load_btn:
            self._img_load_btn.configure(fg_color=C["accent"], hover_color=C["bg_accent_hover"])
        if hasattr(self, '_img_unload_btn') and self._img_unload_btn:
            self._img_unload_btn.configure(fg_color="transparent", text_color=C["accent_red"], hover_color=C["bg_accent_hover"])

        # Video panel
        self._vid_chat.configure(fg_color=C["bg_dark"], text_color=C["fg"])
        self._vid_prompt.configure(fg_color=C["bg_input"], text_color=C["fg"])
        self._vid_stop_btn.configure(text_color=C["accent_red"])
        self._vid_settings_frame.configure(fg_color=C["bg_panel"])
        self._vid_toggle_btn.configure(hover_color=C["bg_accent_hover"])
        self._vid_right_frame.configure(fg_color=C["bg_dark"])
        self._vid_info_label.configure(text_color=C["fg_dim"])

        # Video settings
        for widget in self._vid_settings_frame.winfo_children():
            for child in widget.winfo_children() if hasattr(widget, 'winfo_children') else []:
                if isinstance(child, ctk.CTkLabel):
                    child.configure(text_color=C["fg_dim"])
                elif isinstance(child, ctk.CTkTextbox):
                    child.configure(fg_color=C["bg_input"], text_color=C["fg"])

        if hasattr(self, '_vid_cfg_dec_btn') and self._vid_cfg_dec_btn:
            self._vid_cfg_dec_btn.configure(fg_color=C["bg_dark"], hover_color=C["bg_accent_hover"])
        if hasattr(self, '_vid_cfg_inc_btn') and self._vid_cfg_inc_btn:
            self._vid_cfg_inc_btn.configure(fg_color=C["bg_dark"], hover_color=C["bg_accent_hover"])

        # ComfyUI Load/Unload buttons (video)
        if hasattr(self, '_vid_load_btn') and self._vid_load_btn:
            self._vid_load_btn.configure(fg_color=C["accent"], hover_color=C["bg_accent_hover"])
        if hasattr(self, '_vid_unload_btn') and self._vid_unload_btn:
            self._vid_unload_btn.configure(fg_color="transparent", text_color=C["accent_red"], hover_color=C["bg_accent_hover"])
