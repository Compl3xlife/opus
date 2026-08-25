from __future__ import annotations

from opus.hub import hub


def browser_navigate(url: str) -> str:
    target = (url or "").strip()
    if not target:
        return "Need a URL to navigate to."
    if not target.startswith(("http://", "https://")):
        target = "https://" + target
    result = hub.browser_command("navigate", {"url": target})
    if not result.get("ok"):
        return result.get("error") or "Browser navigate failed."
    return f"Navigated to {target}."


def browser_click(selector: str = "", x: int | None = None, y: int | None = None, text: str = "") -> str:
    payload: dict = {}
    if selector:
        payload["selector"] = selector
    if text:
        payload["text"] = text
    if x is not None and y is not None:
        payload["x"] = int(x)
        payload["y"] = int(y)
    if not payload:
        return "Need a CSS selector, visible text, or x/y for browser click."
    result = hub.browser_command("click", payload)
    if not result.get("ok"):
        return result.get("error") or "Browser click failed."
    return result.get("result") or "Clicked in the browser."


def browser_type(text: str, selector: str = "", clear: bool = False) -> str:
    payload = {"text": text or "", "clear": bool(clear)}
    if selector:
        payload["selector"] = selector
    result = hub.browser_command("type", payload)
    if not result.get("ok"):
        return result.get("error") or "Browser type failed."
    return result.get("result") or "Typed in the browser."


def browser_press(key: str) -> str:
    result = hub.browser_command("press", {"key": key or "Enter"})
    if not result.get("ok"):
        return result.get("error") or "Browser key press failed."
    return result.get("result") or f"Pressed {key}."


def browser_read() -> str:
    result = hub.browser_command("read", {})
    if not result.get("ok"):
        # Fall back to last pushed context
        ctx = hub.get_browser_context()
        title = ctx.get("title") or ""
        url = ctx.get("url") or ""
        text = (ctx.get("page_text") or "")[:3000]
        if not (title or url or text):
            return result.get("error") or "Browser not connected."
        return f"Title: {title}\nURL: {url}\nText:\n{text}"
    data = result.get("result") or {}
    if isinstance(data, str):
        return data
    title = data.get("title") or ""
    url = data.get("url") or ""
    text = (data.get("page_text") or data.get("text") or "")[:4000]
    return f"Title: {title}\nURL: {url}\nText:\n{text}"
