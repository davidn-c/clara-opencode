#!/usr/bin/env python3
"""
main.py — Clara Desktop entry point.

Run via the .desktop launcher.
"""

import sys
import os

# Ensure the directory containing clara2.py is on the path.
# Adjust this if clara2 lives somewhere other than /home/dave/.
sys.path.insert(0, "/home/dave/Clara_OpenCode")

# Also make sure the clara/ package root itself is on the path
# so that `from core.x import ...` works regardless of cwd.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Install print() capture BEFORE importing anything that might print.
from core.logger import install_print_capture
install_print_capture()

from ui.main_window import ClaraGUI


def main() -> None:
    app = ClaraGUI()
    app.mainloop()


if __name__ == "__main__":
    main()
