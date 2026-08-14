# Clara Desktop — Modular Project Layout

## Folder Structure

```
/home/dave/clara/
│
├── main.py                  # ← Entry point (run this)
├── config.py                # All constants: paths, colours, status strings, ComfyUI host
├── Clara.desktop            # .desktop launcher (copy to ~/.local/share/applications/)
│
├── core/                    # Pure-Python, no Tkinter
│   ├── __init__.py
│   ├── logger.py            # GuiLogger queue + print() capture
│   ├── backend.py           # ClaraBackend: wraps clara2, memory, search, TTS, process launchers
│   └── comfy_client.py      # ComfyUIClient: HTTP + WebSocket image generation
│
├── ui/                      # Tkinter widgets
│   ├── __init__.py
│   ├── main_window.py       # ClaraGUI (tk.Tk) — top-level controller
│   ├── sidebar.py           # Sidebar: chat history list, TTS/Deep-Think toggles
│   ├── chat_tab.py          # ChatTab: conversation display + prompt input
│   └── image_tab.py         # ImageTab: ComfyUI generation controls + canvas
│
└── assets/                  # Optional: icons, splash images
    └── clara_icon.png
```

## Dependency Map

```
main.py
  └─ core/logger.py          (install print capture first)
  └─ ui/main_window.py
       ├─ config.py
       ├─ core/logger.py
       ├─ core/backend.py    → clara2, subprocess
       ├─ core/comfy_client.py → websocket-client, urllib
       ├─ ui/sidebar.py      → config
       ├─ ui/chat_tab.py     → config
       └─ ui/image_tab.py    → config, core/comfy_client.py, PIL
```

## Running Clara

```bash
cd /home/dave/clara
/home/dave/clara-venv/clara-venv/bin/python main.py
```

## Installing the .desktop Launcher

```bash
cp /home/dave/clara/Clara.desktop ~/.local/share/applications/
update-desktop-database ~/.local/share/applications/
```

## Required pip packages (install into clara-venv)

```bash
pip install websocket-client pillow
```

`clara2` must remain on the Python path (it lives alongside these files or is
already installed in the venv, as before).

## What Changed vs the Original Single File

| Original               | Now                              | Reason                              |
|------------------------|----------------------------------|-------------------------------------|
| Config literals inline | `config.py`                      | Single place to change host/paths   |
| `GuiLogger` + print()  | `core/logger.py`                 | Importable, testable, restorable    |
| `ClaraBackend` class   | `core/backend.py`                | No Tk dependency — unit-testable    |
| `ComfyUIClient` class  | `core/comfy_client.py`           | No Tk dependency — unit-testable    |
| Sidebar code in GUI    | `ui/sidebar.py`                  | Reusable widget                     |
| Chat display in GUI    | `ui/chat_tab.py`                 | Reusable widget                     |
| Image tab in GUI       | `ui/image_tab.py`                | Reusable widget                     |
| `ClaraGUI` monolith    | `ui/main_window.py`              | Pure controller, delegates to above |
| Script body            | `main.py`                        | Clean entry point                   |
