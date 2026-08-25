from __future__ import annotations

import threading
import time
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from opus.logutil import get_logger
from opus.settings import Settings
from opus.tools.defender import INCOMPLETE, RISKY_EXTENSIONS, defender_status, ensure_realtime_protection, scan_file_queued

log = get_logger()

WATCH_FOLDERS = ("Downloads", "Desktop")
TEMP_FOLDER = Path("AppData") / "Local" / "Temp"
STATUS_INTERVAL = 6 * 60 * 60


class _Handler(FileSystemEventHandler):
    def __init__(self, settings: Settings, *, risky_only: bool = False) -> None:
        self.settings = settings
        self.risky_only = risky_only

    def on_created(self, event):  # noqa: ANN001
        if event.is_directory:
            return
        self._schedule(event.src_path)

    def on_moved(self, event):  # noqa: ANN001
        if event.is_directory:
            return
        self._schedule(event.dest_path)

    def _schedule(self, path: str) -> None:
        if not self.settings.get("background_protection"):
            return
        target = Path(path)
        suffix = target.suffix.lower()
        if suffix in INCOMPLETE:
            return
        if self.risky_only and suffix not in RISKY_EXTENSIONS:
            return
        threading.Thread(target=self._wait_and_scan, args=(path,), daemon=True, name="opus-guard-scan").start()

    def _wait_and_scan(self, path: str) -> None:
        target = Path(path)
        last_size = -1
        stable = 0
        for _ in range(40):
            if not target.exists():
                time.sleep(0.5)
                continue
            size = target.stat().st_size
            if size == last_size and size > 0:
                stable += 1
                if stable >= 2:
                    break
            else:
                stable = 0
                last_size = size
            time.sleep(0.6)
        if not target.exists():
            return
        result = scan_file_queued(target, silent=True)
        if result and result.threat:
            log.warning("Background protection blocked a threat in %s", target)


class Guardian:
    """Silent background malware protection using Windows Defender."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._observers: list[Observer] = []
        self._stop = threading.Event()
        self._health_thread: threading.Thread | None = None

    def start(self) -> None:
        if not self.settings.get("background_protection"):
            log.info("background protection disabled")
            return
        self._ensure_defender()
        self._watch_folders()
        self._stop.clear()
        self._health_thread = threading.Thread(target=self._health_loop, daemon=True, name="opus-guard-health")
        self._health_thread.start()
        log.info("background protection active")

    def stop(self) -> None:
        self._stop.set()
        for observer in self._observers:
            try:
                observer.stop()
                observer.join(timeout=2)
            except Exception:
                pass
        self._observers.clear()

    def scan_now(self, path: str | Path) -> None:
        if not self.settings.get("background_protection"):
            return
        threading.Thread(
            target=lambda: scan_file_queued(path, silent=True),
            daemon=True,
            name="opus-guard-now",
        ).start()

    def _ensure_defender(self) -> None:
        status = defender_status()
        if status.get("realtime") and status.get("service"):
            log.info(
                "defender ready realtime=%s signatures=%sd",
                status.get("realtime"),
                status.get("signature_age_days"),
            )
            return
        log.warning("defender realtime protection was off — trying to turn it on")
        if ensure_realtime_protection():
            log.info("defender realtime protection enabled")
            return
        log.warning("could not enable defender realtime protection — Opus will still scan new files")

    def _watch_folders(self) -> None:
        home = Path.home()
        for name in WATCH_FOLDERS:
            folder = home / name
            folder.mkdir(exist_ok=True)
            self._add_watch(folder, risky_only=False)
        temp = home / TEMP_FOLDER
        temp.mkdir(parents=True, exist_ok=True)
        self._add_watch(temp, risky_only=True)

    def _add_watch(self, folder: Path, *, risky_only: bool) -> None:
        handler = _Handler(self.settings, risky_only=risky_only)
        observer = Observer()
        observer.schedule(handler, str(folder), recursive=False)
        observer.start()
        self._observers.append(observer)
        log.info("watching %s risky_only=%s", folder, risky_only)

    def _health_loop(self) -> None:
        while not self._stop.wait(STATUS_INTERVAL):
            if not self.settings.get("background_protection"):
                continue
            status = defender_status()
            if not status.get("realtime") or not status.get("service"):
                log.warning("defender protection dropped — attempting restore")
                ensure_realtime_protection()
