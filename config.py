"""
config.py — Application-wide constants and paths for Clara Desktop.
"""

from pathlib import Path

# ── Directories ────────────────────────────────────────────────
CHAT_DIR = Path.home() / "ClaraChats"
CHAT_DIR.mkdir(exist_ok=True)

IMAGE_DIR = Path.home() / "ClaraImages"
IMAGE_DIR.mkdir(exist_ok=True)

VIDEO_DIR = Path.home() / "ClaraImages"

# ── Window ─────────────────────────────────────────────────────
WINDOW_TITLE  = "Klara Desktop"
WINDOW_WIDTH  = 1400
WINDOW_HEIGHT = 950

# ── ComfyUI ────────────────────────────────────────────────────
COMFYUI_HOST   = "127.0.0.1:8188"
COMFYUI_WS_URL = f"ws://{COMFYUI_HOST}/ws"
COMFYUI_API_URL = f"http://{COMFYUI_HOST}"

# ── External service scripts ────────────────────────────────────
GEMMA_LAUNCH_SCRIPT = "/home/dave/launch_qwen_codecus.sh"
GEMMA_LOG           = "/home/dave/qwen_codecus.log"
COMFYUI_DIR         = "/home/dave/ComfyUI"
COMFYUI_LOG         = "/home/dave/comfyui.log"

# ── Status bar strings ──────────────────────────────────────────
STATUS_READY    = "🟢 Ready"
STATUS_THINKING = "🧠 Klara is thinking..."
STATUS_SEARCH   = "🌐 Searching the web..."
STATUS_MEMORY   = "💾 Accessing memory..."
STATUS_TTS      = "🔊 Speaking..."
STATUS_ANALYZE  = "📚 Updating memory..."
STATUS_ERROR    = "🔴 Error"

# ── Colour palette ──────────────────────────────────────────────
COLOUR = {
    "bg":           "#2b2b2b",
    "bg_dark":      "#1e1e1e",
    "bg_input":     "#1a1a2e",
    "bg_panel":     "#222222",
    "bg_bar":       "#3a3a3a",
    "bg_sash":      "#404040",
    "accent":       "#5865f2",
    "accent_red":   "#c0392b",
    "fg":           "white",
    "fg_dim":       "#888888",
    "fg_subtle":    "#cccccc",
    "fg_user":      "#7ec8e3",
    "fg_clara":     "#c39bd3",
    "fg_tool":      "#888888",
    "mono_bg":      "#111111",
    "mono_fg":      "#cccccc",
}
