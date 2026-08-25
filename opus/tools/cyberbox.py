"""Copy untrusted files into an isolated cyber box, extract there, scan there.

Windows Sandbox is the real box: no network, no clipboard, no host disk except a
read-only inbox and a tiny report outbox. Archives are never extracted on the PC.
If Sandbox is not installed, Opus uses a sealed quarantine folder and still never
runs anything from the sample.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

from opus.logutil import get_logger
from opus.settings import appdata_dir
from opus.tools.defender import RISKY_EXTENSIONS, parse_threat_names, scan_file

log = get_logger()

ARCHIVE_SUFFIXES = {".zip", ".nupkg", ".cab", ".tar", ".tgz", ".gz"}
ZIP_SUFFIXES = {".zip", ".nupkg"}
MAX_COPY_BYTES = 512 * 1024 * 1024
MAX_ZIP_FILES = 4000
MAX_ZIP_UNCOMPRESSED = 512 * 1024 * 1024
MAX_NESTED_ZIPS = 20
ZIP_BOMB_RATIO = 100
ZIP_BOMB_RATIO_MIN_UNCOMPRESSED = 50 * 1024 * 1024
SANDBOX_WAIT_SECONDS = 180
WORKER_NAME = "run_checks.ps1"

ALIASES = {
    "downloads": "Downloads",
    "download": "Downloads",
    "desktop": "Desktop",
    "documents": "Documents",
    "docs": "Documents",
    "temp": Path("AppData") / "Local" / "Temp",
    "tmp": Path("AppData") / "Local" / "Temp",
}


def box_root() -> Path:
    root = appdata_dir() / "cyberbox"
    for name in ("inbox", "outbox", "quarantine"):
        (root / name).mkdir(parents=True, exist_ok=True)
    return root


def sandbox_exe() -> Path | None:
    windir = Path(os.environ.get("WINDIR") or r"C:\Windows")
    exe = windir / "System32" / "WindowsSandbox.exe"
    return exe if exe.exists() else None


def _status_path() -> Path:
    return box_root() / "sandbox-status.json"


def mark_sandbox_status(*, enabled: bool, reboot_required: bool) -> None:
    payload = {
        "enabled": enabled,
        "reboot_required": reboot_required,
        "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    _status_path().write_text(json.dumps(payload, indent=2), encoding="utf-8")


def sandbox_reboot_pending() -> bool:
    if sandbox_exe():
        return False
    path = _status_path()
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return bool(data.get("enabled") and data.get("reboot_required"))


def sandbox_ready() -> bool:
    return sandbox_exe() is not None


def enable_sandbox_help() -> str:
    if sandbox_reboot_pending():
        return (
            "Windows Sandbox is installed. Restart this PC once, then I'll run scans in the cyber box — "
            "no network, no access to the rest of this computer."
        )
    return (
        "The real cyber box is Windows Sandbox — a disposable PC with no network and no access to this one. "
        "Run enable_opus_cyberbox.bat as administrator if it still isn't installed."
    )


def resolve_target(path: str) -> Path:
    raw = (path or "").strip().strip("\"'")
    raw = raw.replace("this folder", "").replace("that folder", "").replace("this file", "")
    raw = raw.replace("the folder", "").replace("the file", "").strip(" .,!")
    lowered = raw.lower()
    for prefix in ("in ", "from ", "on ", "my ", "the "):
        if lowered.startswith(prefix):
            raw = raw[len(prefix) :]
            lowered = raw.lower()
    if not raw or lowered in {"this", "that", "it", "here", "folder", "file"}:
        return Path.home() / "Downloads"
    alias = ALIASES.get(lowered)
    if alias:
        return (Path.home() / alias).resolve()
    target = Path(os.path.expandvars(os.path.expanduser(raw)))
    if not target.is_absolute():
        guessed = Path.home() / target
        if guessed.exists():
            target = guessed
        else:
            downloads = Path.home() / "Downloads" / target
            if downloads.exists():
                target = downloads
    return target.resolve()


def inspect_in_box(path: str) -> str:
    try:
        target = resolve_target(path)
    except OSError as exc:
        return f"Could not resolve that path: {exc}"
    if not target.exists():
        return f"I couldn't find {target}."
    blocked = preflight_archives(target)
    if blocked:
        return blocked
    if sandbox_ready():
        try:
            return _run_windows_sandbox(target)
        except ValueError as exc:
            return str(exc)
        except Exception:
            log.exception("windows sandbox scan failed")
            return "The cyber box failed to start. " + enable_sandbox_help()
    if sandbox_reboot_pending():
        return enable_sandbox_help()
    return _run_quarantine(target)


def _file_label(path: Path) -> str:
    return path.name or "that file"


def _stay_on_this_pc() -> str:
    return "I will not open, extract, or run it on this PC."


def preflight_archives(target: Path) -> str | None:
    """Read zip headers only. Never extract on the host."""
    paths = [target] if target.is_file() else [path for path in target.rglob("*") if path.is_file()]
    for path in paths[:200]:
        if path.suffix.lower() not in ZIP_SUFFIXES:
            continue
        reason = inspect_zip_metadata(path)
        if reason:
            return reason
    return None


def inspect_zip_metadata(path: Path) -> str | None:
    try:
        with zipfile.ZipFile(path) as archive:
            risk = analyze_zip_infos(archive.infolist())
    except (OSError, zipfile.BadZipFile):
        return None
    if risk["kind"] == "ok":
        return None
    return _zip_block_speech(_file_label(path), risk)


def analyze_zip_infos(infos: list[zipfile.ZipInfo]) -> dict:
    members = [info for info in infos if (info.filename or "").rstrip("/\\")]
    files = [info for info in members if not (info.filename or "").endswith(("/", "\\"))]
    uncompressed = 0
    compressed = 0
    nested_zips = 0
    slip = False
    for info in files:
        name = (info.filename or "").replace("\\", "/")
        uncompressed += max(0, int(info.file_size or 0))
        compressed += max(0, int(info.compress_size or 0))
        lowered = name.lower()
        if lowered.endswith(".zip") or lowered.endswith(".nupkg"):
            nested_zips += 1
        if name.startswith("/") or name.startswith("\\\\"):
            slip = True
        first = Path(name).parts[0] if Path(name).parts else ""
        if ":" in first or ".." in Path(name).parts:
            slip = True
    kind = "ok"
    if slip:
        kind = "zip_slip"
    elif len(files) > MAX_ZIP_FILES:
        kind = "zip_bomb_files"
    elif uncompressed > MAX_ZIP_UNCOMPRESSED:
        kind = "zip_bomb_size"
    elif compressed and uncompressed >= ZIP_BOMB_RATIO_MIN_UNCOMPRESSED and uncompressed / compressed >= ZIP_BOMB_RATIO:
        kind = "zip_bomb_ratio"
    elif nested_zips >= MAX_NESTED_ZIPS:
        kind = "nested_zip_bomb"
    return {
        "kind": kind,
        "files": len(files),
        "uncompressed": uncompressed,
        "compressed": compressed,
        "nested_zips": nested_zips,
        "ratio": int(uncompressed / compressed) if compressed else 0,
    }


def _zip_block_speech(label: str, risk: dict) -> str:
    kind = risk.get("kind") or ""
    stay = _stay_on_this_pc()
    files = int(risk.get("files") or 0)
    uncompressed = int(risk.get("uncompressed") or 0)
    nested = int(risk.get("nested_zips") or 0)
    ratio = int(risk.get("ratio") or 0)
    megabytes = max(1, uncompressed // (1024 * 1024))
    if kind == "zip_slip":
        return (
            f"I stopped {label} because it looks like zip-slip: files inside try to write outside the extract folder. {stay}"
        )
    if kind == "zip_bomb_files":
        return (
            f"I stopped {label} because it looks like a zip bomb: it claims {files} files, "
            f"over the {MAX_ZIP_FILES} file limit. {stay}"
        )
    if kind == "zip_bomb_size":
        return (
            f"I stopped {label} because it looks like a zip bomb: it unpacks to about {megabytes} megabytes, "
            f"over the cyber box limit. {stay}"
        )
    if kind == "zip_bomb_ratio":
        return (
            f"I stopped {label} because it looks like a zip bomb: a small archive claims about {megabytes} megabytes "
            f"unpacked, over {max(ratio, ZIP_BOMB_RATIO)} times the compressed size. {stay}"
        )
    if kind == "nested_zip_bomb":
        return (
            f"I stopped {label} because it looks like a nested zip bomb: {nested} zips packed inside. {stay}"
        )
    return f"I stopped {label} because that archive is unsafe to extract. {stay}"


def _wipe(folder: Path) -> None:
    if not folder.exists():
        return
    for child in folder.iterdir():
        try:
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)
        except OSError:
            log.exception("cyberbox wipe failed for %s", child)


def _copy_sample(source: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    limit_mb = MAX_COPY_BYTES // (1024 * 1024)
    if source.is_file():
        if source.stat().st_size > MAX_COPY_BYTES:
            raise ValueError(
                f"I stopped {_file_label(source)} because it is over {limit_mb} megabytes, "
                f"the cyber box copy limit. {_stay_on_this_pc()}"
            )
        shutil.copy2(source, dest / source.name)
        return
    total = 0
    for dirpath, dirnames, filenames in os.walk(source):
        dirnames[:] = [name for name in dirnames if not name.startswith(".")]
        for name in filenames:
            src = Path(dirpath) / name
            try:
                total += src.stat().st_size
            except OSError:
                continue
            if total > MAX_COPY_BYTES:
                raise ValueError(
                    f"I stopped that folder because it is over {limit_mb} megabytes, "
                    f"the cyber box copy limit. {_stay_on_this_pc()}"
                )
            rel = src.relative_to(source)
            if ".." in rel.parts:
                continue
            out = dest / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, out)


def _wsb_xml(inbox: Path, outbox: Path) -> str:
    return f"""<Configuration>
  <VGpu>Disable</VGpu>
  <Networking>Disable</Networking>
  <AudioInput>Disable</AudioInput>
  <VideoInput>Disable</VideoInput>
  <ClipboardRedirection>Disable</ClipboardRedirection>
  <PrinterRedirection>Disable</PrinterRedirection>
  <ProtectedClient>Enable</ProtectedClient>
  <MemoryInMB>2048</MemoryInMB>
  <MappedFolders>
    <MappedFolder>
      <HostFolder>{escape(str(inbox))}</HostFolder>
      <SandboxFolder>C:\\Inbox</SandboxFolder>
      <ReadOnly>true</ReadOnly>
    </MappedFolder>
    <MappedFolder>
      <HostFolder>{escape(str(outbox))}</HostFolder>
      <SandboxFolder>C:\\Outbox</SandboxFolder>
      <ReadOnly>false</ReadOnly>
    </MappedFolder>
  </MappedFolders>
  <LogonCommand>
    <Command>powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\\Inbox\\run_checks.ps1</Command>
  </LogonCommand>
</Configuration>
"""


def _run_windows_sandbox(target: Path) -> str:
    root = box_root()
    inbox = root / "inbox"
    outbox = root / "outbox"
    _wipe(inbox)
    _wipe(outbox)
    inbox.mkdir(parents=True, exist_ok=True)
    outbox.mkdir(parents=True, exist_ok=True)
    worker = Path(__file__).with_name("cyberbox_worker.ps1")
    shutil.copy2(worker, inbox / WORKER_NAME)
    try:
        _copy_sample(target, inbox / "sample")
    except ValueError as exc:
        _wipe(inbox)
        return str(exc)
    wsb = root / "opus-cyberbox.wsb"
    wsb.write_text(_wsb_xml(inbox, outbox), encoding="utf-8")
    exe = sandbox_exe()
    if not exe:
        return enable_sandbox_help()
    log.info("starting windows sandbox for %s", target)
    subprocess.Popen([str(exe), str(wsb)], close_fds=True)
    report_path = outbox / "report.json"
    deadline = time.time() + SANDBOX_WAIT_SECONDS
    while time.time() < deadline:
        if report_path.exists() and report_path.stat().st_size > 2:
            time.sleep(0.4)
            try:
                payload = json.loads(report_path.read_text(encoding="utf-8-sig", errors="replace")[:100000])
            except json.JSONDecodeError:
                time.sleep(0.5)
                continue
            time.sleep(1.2)
            if not payload.get("threat") and not payload.get("clean") and not payload.get("blocked"):
                host = scan_file(target, silent=True)
                payload["host_defender_code"] = host.code
                payload["host_defender_detail"] = host.detail
                if host.threat:
                    payload["threat"] = True
                    payload["threat_names"] = parse_threat_names(host.detail)
                elif host.clean:
                    payload["clean"] = True
                    payload["host_defender"] = True
            _wipe(inbox)
            return _summarize(target, payload, isolated=True)
        time.sleep(1.0)
    return (
        f"The cyber box is still checking {target.name}. "
        "It has no network and cannot touch this PC. Ask me again in a minute."
    )


def _safe_zip_members(archive: zipfile.ZipFile, dest: Path) -> list[zipfile.ZipInfo]:
    dest = dest.resolve()
    risk = analyze_zip_infos(archive.infolist())
    if risk["kind"] != "ok":
        raise ValueError(_zip_block_speech("that zip", risk))
    kept: list[zipfile.ZipInfo] = []
    for info in archive.infolist():
        name = (info.filename or "").replace("\\", "/")
        if not name or name.endswith("/"):
            continue
        if name.startswith("/") or name.startswith("\\\\") or ":" in Path(name).parts[0]:
            continue
        parts = Path(name).parts
        if ".." in parts:
            continue
        out = (dest / name).resolve()
        if dest != out and dest not in out.parents:
            continue
        kept.append(info)
    return kept


def _extract_zip(archive_path: Path, dest: Path) -> int:
    dest.mkdir(parents=True, exist_ok=True)
    count = 0
    with zipfile.ZipFile(archive_path) as archive:
        for info in _safe_zip_members(archive, dest):
            archive.extract(info, dest)
            count += 1
    return count


def _extract_tar(archive_path: Path, dest: Path) -> int:
    dest.mkdir(parents=True, exist_ok=True)
    # Windows tar stays inside dest; still do not run extracted files.
    completed = subprocess.run(
        ["tar", "-xf", str(archive_path), "-C", str(dest)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if completed.returncode != 0:
        return 0
    return sum(1 for _ in dest.rglob("*") if _.is_file())


def _extract_tree(root: Path) -> int:
    extracted = 0
    for _round in range(3):
        archives = [
            path
            for path in root.rglob("*")
            if path.is_file() and path.suffix.lower() in ARCHIVE_SUFFIXES
            and not path.name.endswith(".tar.gz")
        ]
        archives += [path for path in root.rglob("*.tar.gz") if path.is_file()]
        if not archives:
            break
        for archive in archives:
            dest = archive.parent / (archive.stem + "_extracted")
            if dest.exists():
                continue
            try:
                if archive.suffix.lower() in {".zip", ".nupkg"}:
                    extracted += _extract_zip(archive, dest)
                elif archive.suffix.lower() in {".tar", ".tgz", ".gz"} or archive.name.lower().endswith(".tar.gz"):
                    extracted += _extract_tar(archive, dest)
            except (OSError, zipfile.BadZipFile, subprocess.TimeoutExpired):
                log.exception("quarantine extract failed for %s", archive)
    return extracted


def _seal_folder(folder: Path) -> None:
    try:
        subprocess.run(
            [
                "icacls",
                str(folder),
                "/inheritance:r",
                "/grant:r",
                f"{os.environ.get('USERNAME') or 'Users'}:(OI)(CI)F",
                "/grant:r",
                "SYSTEM:(OI)(CI)F",
            ],
            capture_output=True,
            timeout=20,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def _run_quarantine(target: Path) -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    quarantine = box_root() / "quarantine" / stamp
    quarantine.mkdir(parents=True, exist_ok=True)
    try:
        _copy_sample(target, quarantine / "sample")
        extracted = _extract_tree(quarantine)
    except ValueError as exc:
        return str(exc)
    _seal_folder(quarantine)
    result = scan_file(quarantine, silent=False)
    files = [path for path in quarantine.rglob("*") if path.is_file()]
    risky = [path.name for path in files if path.suffix.lower() in RISKY_EXTENSIONS][:40]
    payload = {
        "ok": True,
        "isolated": False,
        "threat": result.threat,
        "clean": result.clean,
        "code": result.code,
        "files": len(files),
        "extracted_archives": extracted,
        "risky": risky,
        "note": "Windows Sandbox is off. Extract happened in a sealed folder; nothing from the sample was run.",
        "threat_names": parse_threat_names(result.detail),
        "host_defender_detail": result.detail,
    }
    return _summarize(target, payload, isolated=False) + " " + enable_sandbox_help()


def _summarize(target: Path, payload: dict, *, isolated: bool) -> str:
    label = _file_label(target)
    files = int(payload.get("files") or 0)
    extracted = int(payload.get("extracted_archives") or 0)
    where = "inside the cyber box" if isolated else "in a sealed folder on this PC"
    stay = _stay_on_this_pc()
    names = _threat_names_from_payload(payload)
    blocked = bool(payload.get("blocked"))
    reason = str(payload.get("reason") or payload.get("kind") or "")
    if payload.get("threat") or names:
        if names:
            named = ", ".join(names[:3])
            return (
                f"I stopped {label} because Windows Defender flagged it as {named}. {stay} "
                f"It stayed {where}."
            )
        return (
            f"I stopped {label} because Windows Defender reported malware. {stay} "
            f"It stayed {where}."
        )
    if blocked or reason in {
        "zip_slip",
        "zip_bomb_files",
        "zip_bomb_size",
        "zip_bomb_ratio",
        "nested_zip_bomb",
        "too_large",
    }:
        risk = {
            "kind": reason,
            "files": payload.get("zip_files") or files,
            "uncompressed": payload.get("uncompressed") or 0,
            "nested_zips": payload.get("nested_zips") or 0,
            "ratio": payload.get("ratio") or 0,
        }
        if reason == "too_large":
            limit_mb = MAX_COPY_BYTES // (1024 * 1024)
            return (
                f"I stopped {label} because it is over {limit_mb} megabytes, the cyber box copy limit. {stay}"
            )
        return _zip_block_speech(label, risk)
    if payload.get("clean"):
        extra = f" Extracted {extracted} archive(s)." if extracted else ""
        if payload.get("host_defender") and isolated:
            extra += " Defender scanned the original sample from this PC without unpacking it here."
        return f"Clean {where}: {label}, {files} file(s).{extra}"
    if payload.get("ok") and isolated:
        extra = f" Extracted {extracted} archive(s)." if extracted else ""
        return f"Cyber box finished {label}: {files} file(s).{extra}"
    if payload.get("error"):
        return f"Cyber box error while checking {label}."
    return f"Finished checking {label} {where} (code {payload.get('code')})."


def _threat_names_from_payload(payload: dict) -> list[str]:
    names: list[str] = []
    for item in payload.get("threat_names") or []:
        text = str(item).strip()
        if text and text.lower() not in {name.lower() for name in names}:
            names.append(text)
    blob = " ".join(
        str(payload.get(key) or "")
        for key in ("defender_log", "host_defender_detail", "detail", "error", "note")
    )
    for name in parse_threat_names(blob):
        if name.lower() not in {item.lower() for item in names}:
            names.append(name)
    return names[:6]
