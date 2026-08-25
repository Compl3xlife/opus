from __future__ import annotations

from pathlib import Path

import webview

from opus.logutil import get_logger
from opus.settings import DEFAULT_HOST, Settings

log = get_logger()


class Panel:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.window: webview.Window | None = None

    def create(self) -> webview.Window:
        port = int(self.settings.get("port") or 5840)
        host = self.settings.get("host") or DEFAULT_HOST
        icon = Path(__file__).resolve().parent / "ui" / "icon.ico"
        png = Path(__file__).resolve().parent / "ui" / "icon.png"
        self.window = webview.create_window(
            "Opus",
            url=f"http://{host}:{port}/",
            width=420,
            height=760,
            frameless=True,
            easy_drag=False,
            on_top=False,
            shadow=True,
            hidden=True,
            background_color="#0c0d10",
        )
        for candidate in (icon, png):
            if candidate.exists():
                try:
                    self.window.set_icon(str(candidate))
                    break
                except Exception:
                    pass
        return self.window

    def show(self) -> None:
        if not self.window:
            return
        try:
            self.window.on_top = True
        except Exception:
            pass
        try:
            self.window.show()
        except Exception:
            log.exception("panel show failed")
        try:
            self.window.restore()
        except Exception:
            pass

    def hide(self) -> None:
        if not self.window:
            return
        try:
            self.window.on_top = False
        except Exception:
            pass
        try:
            self.window.hide()
        except Exception:
            log.exception("panel hide failed")
