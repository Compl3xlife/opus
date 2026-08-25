"""Windows taskbar / process icon helpers."""
from __future__ import annotations

import sys

APP_ID = "Opus.Assistant.1"


def set_process_icon() -> None:
    """Use Opus branding instead of the Python executable icon on Windows."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except Exception:
        pass
