from __future__ import annotations

import threading
import pystray

from opus.settings import APP_NAME
from opus.ui.icons import draw_icon


def _icon_image(active: bool = True):
    return draw_icon(256, active=active)


class Tray:
    def __init__(self, controller) -> None:
        self.controller = controller
        self.icon = pystray.Icon(
            APP_NAME,
            _icon_image(True),
            APP_NAME,
            menu=pystray.Menu(
                pystray.MenuItem("Open panel", self._open, default=True),
                pystray.MenuItem("Listening", self._toggle, checked=lambda _: controller.listening_enabled()),
                pystray.MenuItem("Quit Opus", self._quit),
            ),
        )

    def start(self) -> None:
        threading.Thread(target=self.icon.run, daemon=True, name="opus-tray").start()

    def stop(self) -> None:
        try:
            self.icon.stop()
        except Exception:
            pass

    def set_active(self, active: bool) -> None:
        # pystray icon updates must not run on the audio/STT thread.
        def _apply() -> None:
            try:
                self.icon.icon = _icon_image(active)
            except Exception:
                pass

        threading.Thread(target=_apply, daemon=True, name="opus-tray-icon").start()

    def _open(self, icon, item):  # noqa: ARG002
        self.controller.show_panel()

    def _toggle(self, icon, item):  # noqa: ARG002
        self.controller.toggle_listening()

    def _quit(self, icon, item):  # noqa: ARG002
        self.controller.shutdown()
