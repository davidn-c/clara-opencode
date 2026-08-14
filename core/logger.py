"""
core/logger.py — Thread-safe event queue and print() capture for the GUI.

Import this module early (before any other clara imports) so that all
print() calls from clara2 or other modules are automatically forwarded
into the GUI's Tool-Activity log.
"""

import queue
import builtins

# ── Singleton event queue ───────────────────────────────────────


class GuiLogger:
    """
    Receives Clara backend events and forwards them into the GUI via a
    thread-safe queue.  The GUI polls this queue on the Tk main thread.
    """

    def __init__(self):
        self.queue: queue.Queue = queue.Queue()

    def log(self, message: str) -> None:
        self.queue.put(message)


# Module-level singleton — import this everywhere you need to log.
gui_logger = GuiLogger()


# ── print() capture ─────────────────────────────────────────────

_original_print = builtins.print


def _gui_print(*args, **kwargs):
    msg = " ".join(str(x) for x in args)
    gui_logger.log(msg)
    _original_print(*args, **kwargs)


def install_print_capture() -> None:
    """Replace builtins.print with the GUI-forwarding version."""
    builtins.print = _gui_print


def restore_print() -> None:
    """Restore the original builtins.print (useful in tests)."""
    builtins.print = _original_print
