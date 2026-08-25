from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from opus.logutil import get_logger

log = get_logger()

THREAT_NAME_RE = re.compile(
    r"\b((?:Virus|Trojan|Worm|Ransom(?:ware)?|Malware|PUA|PUP|HackTool|Backdoor|Exploit|Spyware|Adware|Behavior|HackTool|Program|VirTool|Joke|Tool)"
    r"[:=/][A-Za-z0-9._+\-]+(?:/[A-Za-z0-9._+\-]+)?(?:![A-Za-z0-9._+\-]+)?)",
    re.IGNORECASE,
)


def parse_threat_names(detail: str) -> list[str]:
    found: list[str] = []
    for match in THREAT_NAME_RE.finditer(detail or ""):
        name = match.group(1).strip().rstrip(".,;:")
        if name and name.lower() not in {item.lower() for item in found}:
            found.append(name)
    return found[:6]


INCOMPLETE = {".crdownload", ".tmp", ".partial", ".opdownload", ".download", ".part"}
RISKY_EXTENSIONS = {
    ".exe",
    ".dll",
    ".scr",
    ".bat",
    ".cmd",
    ".ps1",
    ".vbs",
    ".js",
    ".msi",
    ".jar",
    ".com",
    ".hta",
    ".wsf",
    ".reg",
    ".inf",
    ".cpl",
    ".apk",
    ".iso",
    ".lnk",
}


@dataclass
class ScanResult:
    path: Path
    clean: bool
    threat: bool
    code: int
    detail: str

    @property
    def summary(self) -> str:
        if self.threat:
            names = parse_threat_names(self.detail)
            if names:
                return (
                    f"I stopped {self.path.name or 'that file'} because Windows Defender flagged it as "
                    f"{', '.join(names[:3])}. I will not open, extract, or run it on this PC."
                )
            return (
                f"I stopped {self.path.name or 'that file'} because Windows Defender reported malware. "
                "I will not open, extract, or run it on this PC."
            )
        if self.clean:
            return f"Clean: {self.path.name}"
        return f"Scan issue ({self.code}): {self.path.name}"


def _mp_cmd() -> Path | None:
    for base in (os.environ.get("ProgramFiles", r"C:\Program Files"), r"C:\Program Files"):
        candidate = Path(base) / "Windows Defender" / "MpCmdRun.exe"
        if candidate.exists():
            return candidate
    return None


def _powershell_json(script: str, timeout: float = 12.0) -> dict:
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if completed.returncode != 0:
        return {}
    raw = (completed.stdout or "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def defender_status() -> dict:
    script = """
    $s = Get-MpComputerStatus
    [pscustomobject]@{
        service = [bool]$s.AMServiceEnabled
        antispyware = [bool]$s.AntispywareEnabled
        realtime = [bool]$s.RealTimeProtectionEnabled
        on_access = [bool]$s.OnAccessProtectionEnabled
        ioav = [bool]$s.IoavProtectionEnabled
        sig_age = [int]$s.AntivirusSignatureAge
    } | ConvertTo-Json -Compress
    """
    data = _powershell_json(script)
    return {
        "service": bool(data.get("service")),
        "antispyware": bool(data.get("antispyware")),
        "realtime": bool(data.get("realtime")),
        "on_access": bool(data.get("on_access")),
        "ioav": bool(data.get("ioav")),
        "signature_age_days": int(data.get("sig_age") or 0),
    }


def ensure_realtime_protection() -> bool:
    status = defender_status()
    if status.get("realtime") and status.get("service"):
        return True
    script = """
    try {
        Set-MpPreference -DisableRealtimeMonitoring $false -ErrorAction Stop
        $s = Get-MpComputerStatus
        if ($s.RealTimeProtectionEnabled) { exit 0 } else { exit 1 }
    } catch { exit 1 }
    """
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def scan_file(path: str | Path, *, silent: bool = False) -> ScanResult:
    target = Path(os.path.expandvars(os.path.expanduser(str(path)))).resolve()
    if not target.exists():
        detail = f"Missing: {target}"
        if not silent:
            log.info(detail)
        return ScanResult(target, clean=True, threat=False, code=0, detail=detail)
    mp = _mp_cmd()
    if not mp:
        detail = "Windows Defender command-line scanner was not found."
        log.warning(detail)
        return ScanResult(target, clean=False, threat=False, code=-1, detail=detail)
    try:
        completed = subprocess.run(
            [str(mp), "-Scan", "-ScanType", "3", "-File", str(target)],
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        detail = f"Scan timed out: {target}"
        log.warning(detail)
        return ScanResult(target, clean=False, threat=False, code=-2, detail=detail)
    output = ((completed.stdout or "") + "\n" + (completed.stderr or "")).strip()
    code = int(completed.returncode)
    threat = code == 2
    clean = code == 0
    result = ScanResult(target, clean=clean, threat=threat, code=code, detail=output or f"exit {code}")
    if threat:
        names = parse_threat_names(result.detail)
        log.warning("Threat detected: %s %s", target, names or result.detail[:200])
        _log_threat(result)
    elif not silent:
        log.info("Scan clean: %s", target.name)
    return result


def scan_path(path: str) -> str:
    from opus.tools.cyberbox import inspect_in_box

    return inspect_in_box(path)


def _log_threat(result: ScanResult) -> None:
    from opus.settings import appdata_dir

    path = appdata_dir() / "threats.log"
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} | {result.path} | code={result.code}\n"
    try:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)
            if result.detail:
                handle.write(result.detail[:2000] + "\n")
    except OSError:
        log.exception("failed to write threat log")


_scan_lock = threading.Lock()
_recent: dict[str, float] = {}
_RECENT_TTL = 900.0


def scan_file_queued(path: str | Path, *, silent: bool = True) -> ScanResult | None:
    target = Path(os.path.expandvars(os.path.expanduser(str(path))))
    try:
        target = target.resolve()
    except OSError:
        return None
    if not target.exists() or target.is_dir():
        return None
    key = f"{target}:{target.stat().st_size}:{int(target.stat().st_mtime)}"
    now = time.time()
    with _scan_lock:
        seen = _recent.get(key)
        if seen and now - seen < _RECENT_TTL:
            return None
        _recent[key] = now
        stale = [item for item, stamp in _recent.items() if now - stamp > _RECENT_TTL]
        for item in stale:
            _recent.pop(item, None)
    return scan_file(target, silent=silent)
