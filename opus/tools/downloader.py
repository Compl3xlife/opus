from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path

from opus.hub import hub
from opus.logutil import get_logger
from opus.settings import Settings

log = get_logger()

CREATE_NO_WINDOW = 0x08000000
DOWNLOADS_ROOT = Path("D:/Downloads/Opus")

_active_lock = threading.Lock()
_active: dict | None = None


def downloads_dir(kind: str = "Video") -> Path:
    d = DOWNLOADS_ROOT / kind
    d.mkdir(parents=True, exist_ok=True)
    return d


def _find_ffmpeg() -> str | None:
    try:
        import imageio_ffmpeg

        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and Path(exe).exists():
            return exe
    except Exception:
        pass
    candidates = [
        Path(os.environ.get("FFMPEG_PATH", "")),
        Path(__file__).resolve().parent.parent.parent / "ffmpeg.exe",
        Path("C:/Users/matth/OneDrive/Desktop/Video Donwloader/EXE/ffmpeg.exe"),
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    import shutil
    found = shutil.which("ffmpeg")
    return found


def download_video(url: str, *, format_type: str = "video", settings: Settings | None = None) -> str:
    global _active

    with _active_lock:
        if _active and _active.get("running"):
            return "A download is already in progress."
        _active = {"running": True, "url": url, "progress": "Starting...", "done": False, "error": None}

    def _run():
        global _active
        try:
            kind = {"video": "Video", "audio": "Audio", "music": "Music"}.get(format_type, "Video")
            out_dir = downloads_dir(kind)

            import yt_dlp

            opts: dict = {
                "outtmpl": str(out_dir / "%(title)s [%(id)s].%(ext)s"),
                "quiet": True,
                "no_warnings": True,
                "noprogress": False,
                "progress_hooks": [_progress_hook],
            }

            ffmpeg = _find_ffmpeg()
            if ffmpeg:
                ffmpeg_path = Path(ffmpeg)
                # yt-dlp accepts either directory or full executable path.
                # imageio ships ffmpeg with versioned filenames, so pass full path.
                opts["ffmpeg_location"] = str(ffmpeg_path if ffmpeg_path.is_file() else ffmpeg_path.parent)

            if format_type == "video":
                opts["format"] = "bv*+ba/b"
                opts["merge_output_format"] = "mp4"
                opts["postprocessors"] = [{"key": "EmbedThumbnail"}, {"key": "FFmpegMetadata"}]
            elif format_type == "audio":
                opts["format"] = "ba/b"
                opts["postprocessors"] = [
                    {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"},
                    {"key": "EmbedThumbnail"},
                    {"key": "FFmpegMetadata"},
                ]
            elif format_type == "music":
                opts["format"] = "ba/b"
                opts["postprocessors"] = [
                    {"key": "FFmpegExtractAudio", "preferredcodec": "flac"},
                    {"key": "FFmpegMetadata"},
                ]

            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    ydl.download([url])
            except Exception as exc:
                # Fallback for systems where ffmpeg merge isn't available.
                if format_type == "video" and "ffmpeg is not installed" in str(exc).lower():
                    fallback = dict(opts)
                    fallback.pop("merge_output_format", None)
                    fallback.pop("postprocessors", None)
                    fallback.pop("ffmpeg_location", None)
                    fallback["format"] = "b"
                    with yt_dlp.YoutubeDL(fallback) as ydl:
                        ydl.download([url])
                else:
                    raise

            with _active_lock:
                _active["progress"] = "Done"
                _active["done"] = True
                _active["running"] = False
            hub.add_message("opus", f"Downloaded: {url}")
            hub.broadcast_sync({"type": "download_status", "data": get_status()})

        except Exception as exc:
            log.exception("download failed")
            with _active_lock:
                _active["progress"] = f"Error: {exc}"
                _active["error"] = str(exc)
                _active["running"] = False
            hub.add_message("opus", f"Download failed: {exc}")
            hub.broadcast_sync({"type": "download_status", "data": get_status()})

    threading.Thread(target=_run, daemon=True, name="opus-download").start()
    return "Download started."


def _progress_hook(d: dict) -> None:
    global _active
    status = d.get("status", "")
    if status == "downloading":
        pct = d.get("_percent_str", "").strip()
        speed = d.get("_speed_str", "").strip()
        eta = d.get("_eta_str", "").strip()
        msg = f"{pct} at {speed}" + (f" — ETA {eta}" if eta else "")
        with _active_lock:
            if _active:
                _active["progress"] = msg
        hub.broadcast_sync({"type": "download_status", "data": {"progress": msg, "running": True, "done": False}})
    elif status == "finished":
        with _active_lock:
            if _active:
                _active["progress"] = "Processing..."
        hub.broadcast_sync({"type": "download_status", "data": {"progress": "Processing...", "running": True, "done": False}})


def get_status() -> dict:
    with _active_lock:
        if _active:
            return {
                "running": _active["running"],
                "progress": _active["progress"],
                "done": _active["done"],
                "error": _active.get("error"),
                "url": _active.get("url", ""),
            }
    return {"running": False, "progress": "", "done": False, "error": None, "url": ""}


def open_downloads_folder() -> str:
    folder = str(DOWNLOADS_ROOT)
    try:
        os.startfile(folder)
        return "Opened downloads folder."
    except Exception as exc:
        return f"Could not open folder: {exc}"
