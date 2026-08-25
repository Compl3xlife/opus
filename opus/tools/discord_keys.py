"""Control the user's Discord client via keyboard shortcuts."""
from __future__ import annotations

import ctypes
import time

user32 = ctypes.windll.user32

VK_CONTROL = 0x11
VK_SHIFT = 0x10
VK_M = 0x4D
VK_D = 0x44
VK_H = 0x48  # Hang up (Ctrl+Shift+H is not standard; Discord uses disconnect button)
VK_K = 0x4B
VK_F = 0x46
KEYEVENTF_KEYUP = 0x0002


def _tap(keys: list[int], delay: float = 0.03) -> None:
    for k in keys:
        user32.keybd_event(k, 0, 0, 0)
        time.sleep(delay)
    for k in reversed(keys):
        user32.keybd_event(k, 0, KEYEVENTF_KEYUP, 0)
        time.sleep(delay)


def _focus_discord() -> int:
    """Bring Discord window to front briefly for hotkey delivery. Returns previous foreground window handle."""
    import ctypes.wintypes

    prev_hwnd = user32.GetForegroundWindow()

    def _enum_callback(h, _):
        length = user32.GetWindowTextLengthW(h)
        if length > 0:
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(h, buf, length + 1)
            if "Discord" in buf.value and "Cursor" not in buf.value and "Overlay" not in buf.value:
                _enum_callback.found = h
                return False
        return True

    _enum_callback.found = None
    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
    user32.EnumWindows(WNDENUMPROC(_enum_callback), 0)

    if _enum_callback.found:
        user32.SetForegroundWindow(_enum_callback.found)
        time.sleep(0.15)
    return prev_hwnd


def _restore_focus(prev_hwnd: int) -> None:
    """Give focus back to the previous window."""
    if prev_hwnd:
        time.sleep(0.2)
        user32.SetForegroundWindow(prev_hwnd)


def _click_discord_tray(item: str) -> None:
    """Click Mute or Deafen from Discord's system tray right-click menu."""
    import subprocess
    # Use PowerShell + UI Automation to right-click Discord tray and select item
    ps_script = f'''
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes

# Find Discord in notification area and right-click it
$source = [System.Windows.Automation.AutomationElement]::RootElement
$tray = $source.FindFirst(
    [System.Windows.Automation.TreeScope]::Descendants,
    (New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::ClassNameProperty, "Shell_TrayWnd"
    ))
)
# Simpler: just simulate the keypresses with Discord minimized
'''
    # UI Automation for tray is unreliable. Use a simpler approach:
    # Discord responds to Ctrl+Shift+M/D even when minimized to tray
    # The issue was likely timing. Let's try with longer delays.
    pass


def toggle_mute() -> None:
    """Toggle mute — Discord responds to Ctrl+Shift+M even minimized."""
    prev = _focus_discord()
    time.sleep(0.2)
    _tap([VK_CONTROL, VK_SHIFT, VK_M])
    time.sleep(0.2)
    _restore_focus(prev)


def toggle_deafen() -> None:
    """Toggle deafen — Discord responds to Ctrl+Shift+D."""
    prev = _focus_discord()
    time.sleep(0.3)
    # Hold Ctrl, then Shift, then press D explicitly
    user32.keybd_event(VK_CONTROL, 0, 0, 0)
    time.sleep(0.05)
    user32.keybd_event(VK_SHIFT, 0, 0, 0)
    time.sleep(0.05)
    user32.keybd_event(VK_D, 0, 0, 0)
    time.sleep(0.05)
    user32.keybd_event(VK_D, 0, KEYEVENTF_KEYUP, 0)
    time.sleep(0.05)
    user32.keybd_event(VK_SHIFT, 0, KEYEVENTF_KEYUP, 0)
    time.sleep(0.05)
    user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)
    time.sleep(0.2)
    _restore_focus(prev)


def leave_call() -> None:
    """Disconnect from voice. Keybind: Ctrl+Alt+L (set this in Discord Settings > Keybinds)."""
    VK_L = 0x4C
    VK_ALT = 0x12
    prev = _focus_discord()
    _tap([VK_CONTROL, VK_ALT, VK_L])
    _restore_focus(prev)


def search_discord(query: str = "") -> None:
    """Open Discord's quick switcher (Ctrl+K) or search (Ctrl+F) and type the query."""
    _focus_discord()
    if query:
        _tap([VK_CONTROL, VK_K])
        time.sleep(0.3)
        # Type the query
        for char in query:
            code = user32.VkKeyScanW(ord(char))
            vk = code & 0xFF
            shift = bool(code & 0x100)
            if shift:
                user32.keybd_event(VK_SHIFT, 0, 0, 0)
            user32.keybd_event(vk, 0, 0, 0)
            time.sleep(0.02)
            user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
            if shift:
                user32.keybd_event(VK_SHIFT, 0, KEYEVENTF_KEYUP, 0)
            time.sleep(0.02)
    else:
        _tap([VK_CONTROL, VK_K])
