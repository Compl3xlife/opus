from __future__ import annotations

import ctypes
import traceback
import urllib.request


def _fatal_log(exc: BaseException) -> None:
    try:
        from opus.settings import appdata_dir

        log_path = appdata_dir() / "opus.log"
        with log_path.open("a", encoding="utf-8") as f:
            f.write("\n[STARTUP FATAL]\n")
            f.write("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
            f.write("\n")
    except Exception:
        pass


def _already_running(port: int) -> bool:
    """True if another Opus is already serving on this port."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{int(port)}/api/port", timeout=1.2) as resp:
            return 200 <= getattr(resp, "status", 200) < 300
    except Exception:
        return False


_OPUS_MUTEX = None


def _acquire_mutex() -> bool:
    """Windows named mutex so two tray Opuses don't fight over the port."""
    global _OPUS_MUTEX
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.CreateMutexW(None, False, "Local\\OpusAssistantSingleton")
        if not handle:
            return True
        error_already_exists = 183
        if kernel32.GetLastError() == error_already_exists:
            return False
        # Keep handle alive for process lifetime.
        _OPUS_MUTEX = handle
        return True
    except Exception:
        return True


if __name__ == "__main__":
    try:
        from opus.runtime import set_runtime

        set_runtime("pc")
        from opus.settings import Settings

        settings = Settings()
        port = int(settings.get("port") or 5840)
        if _already_running(port) or not _acquire_mutex():
            # Second launch — leave the existing instance alone.
            try:
                from opus.logutil import get_logger

                get_logger().warning("Opus already running on port %s — exiting duplicate.", port)
            except Exception:
                pass
            raise SystemExit(0)
        from opus.app import main

        main()
    except SystemExit:
        raise
    except Exception as exc:
        _fatal_log(exc)
        raise
