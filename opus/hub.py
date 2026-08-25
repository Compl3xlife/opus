from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from collections import deque
from concurrent import futures
from typing import Any

from opus.logutil import get_logger

log = get_logger()


class EventHub:
    """Thread-safe fan-out for the overlay, tray, and browser sockets."""

    def __init__(self) -> None:
        self.loop: asyncio.AbstractEventLoop | None = None
        self.ui_clients: set[Any] = set()
        self.browser_clients: set[Any] = set()
        self._browser_meta: dict[Any, dict] = {}
        self._browser_waiters: dict[str, futures.Future] = {}
        self._primary_browser: Any | None = None
        self._lock = threading.Lock()
        self.status = {
            "listening": True,
            "speaking": False,
            "mode": "idle",
            "last_heard": "",
            "last_spoken": "",
            "speaking_since": 0.0,
            "recording": False,
            "buffering": False,
            "message": "Say “Opus” to wake me.",
        }
        self.conversation: deque[dict] = deque(maxlen=40)
        self.browser_context = {
            "url": "",
            "title": "",
            "selection": "",
            "page_text": "",
            "browser": "",
        }

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop

    def set_status(self, **kwargs) -> None:
        with self._lock:
            self.status.update(kwargs)
            payload = {"type": "status", "data": dict(self.status)}
        self.emit_ui(payload)

    def add_message(self, role: str, content: str) -> None:
        item = {"role": role, "content": content}
        with self._lock:
            self.conversation.append(item)
        self.emit_ui({"type": "message", "data": item})

    def set_browser_context(self, data: dict) -> None:
        with self._lock:
            self.browser_context.update({k: v for k, v in data.items() if k in self.browser_context})
            context = dict(self.browser_context)
        self.emit_ui({"type": "browser", "data": context})

    def get_browser_context(self) -> dict:
        with self._lock:
            return dict(self.browser_context)

    def recent_conversation(self) -> list[dict]:
        with self._lock:
            return list(self.conversation)

    def register_browser_client(self, client: Any, browser: str = "") -> None:
        with self._lock:
            self.browser_clients.add(client)
            self._browser_meta[client] = {"browser": (browser or "").lower(), "ts": time.time()}
            self._primary_browser = client
        log.info("Opus Bridge connected browser=%s clients=%s", browser or "?", len(self.browser_clients))

    def note_browser_client(self, client: Any, browser: str) -> None:
        with self._lock:
            if client in self.browser_clients:
                meta = self._browser_meta.setdefault(client, {})
                meta["browser"] = (browser or "").lower()
                meta["ts"] = time.time()
                self._primary_browser = client

    def forget_browser_client(self, client: Any) -> None:
        with self._lock:
            self.browser_clients.discard(client)
            self._browser_meta.pop(client, None)
            if self._primary_browser is client:
                self._primary_browser = next(iter(self.browser_clients), None)

    def browser_connected(self, browser_id: str = "") -> bool:
        needle = (browser_id or "").lower()
        with self._lock:
            if not self.browser_clients:
                return False
            if not needle:
                return True
            aliases = _browser_aliases(needle)
            for meta in self._browser_meta.values():
                if meta.get("browser") in aliases:
                    return True
            return bool(self.browser_clients)

    def browser_command(
        self,
        action: str,
        payload: dict | None = None,
        *,
        browser_id: str = "",
        timeout: float = 45.0,
    ) -> dict:
        if not self.loop:
            return {"ok": False, "error": "Opus server is not ready."}
        # Blocking wait on the asyncio thread deadlocks Bridge replies.
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is not None and running is self.loop:
            log.error("browser_command(%s) called on server loop — skipped", action)
            return {"ok": False, "error": "Browser command cannot run on the server event loop."}

        with self._lock:
            clients = _pick_browser_clients(
                self.browser_clients,
                self._browser_meta,
                browser_id,
                primary=self._primary_browser,
            )
        if not clients:
            return {"ok": False, "error": "Opus Bridge browser extension is not connected."}
        request_id = uuid.uuid4().hex
        message = json.dumps({"action": action, "request_id": request_id, **(payload or {})})
        future: futures.Future = futures.Future()
        with self._lock:
            self._browser_waiters[request_id] = future
        log.info(
            "browser_command send action=%s timeout=%.1f clients=%s thread=%s",
            action,
            timeout,
            len(clients),
            threading.current_thread().name,
        )
        asyncio.run_coroutine_threadsafe(self._send_all(clients, message), self.loop)

        # Hard ceiling: never block the chess loop forever even if Future.result misbehaves.
        box: dict[str, Any] = {}

        def _wait() -> None:
            try:
                box["result"] = future.result(timeout=timeout)
            except Exception as exc:
                box["error"] = exc

        waiter = threading.Thread(target=_wait, name=f"opus-browser-{action}", daemon=True)
        waiter.start()
        waiter.join(timeout=max(1.0, timeout + 1.5))
        if waiter.is_alive():
            with self._lock:
                self._browser_waiters.pop(request_id, None)
            log.error("browser_command hard-timeout action=%s", action)
            return {"ok": False, "error": "Browser extension did not respond in time."}
        if "error" in box:
            with self._lock:
                self._browser_waiters.pop(request_id, None)
            err = box["error"]
            if isinstance(err, futures.TimeoutError):
                return {"ok": False, "error": "Browser extension did not respond in time."}
            log.exception("browser command failed action=%s", action, exc_info=err)
            return {"ok": False, "error": str(err)}
        result = box.get("result")
        log.info("browser_command recv action=%s ok=%s", action, getattr(result, "get", lambda *_: None)("ok") if isinstance(result, dict) else None)
        return result if isinstance(result, dict) else {"ok": False, "error": "Invalid browser response."}

    def resolve_browser_response(self, request_id: str, data: dict) -> bool:
        with self._lock:
            future = self._browser_waiters.pop(request_id or "", None)
        if future and not future.done():
            future.set_result(data)
            return True
        return False

    def emit_ui(self, payload: dict) -> None:
        self._broadcast(self.ui_clients, payload)

    def broadcast_sync(self, payload: dict) -> None:
        self._broadcast(self.ui_clients, payload)

    def _broadcast(self, clients: set[Any], payload: dict) -> None:
        if not self.loop:
            return
        message = json.dumps(payload) if isinstance(payload, dict) else payload
        asyncio.run_coroutine_threadsafe(self._send_all(clients, message), self.loop)

    async def _send_all(self, clients: set[Any], message: str) -> None:
        stale = []
        for ws in list(clients):
            try:
                await ws.send_text(message)
            except Exception:
                stale.append(ws)
        for ws in stale:
            self.forget_browser_client(ws)


def _browser_aliases(browser_id: str) -> set[str]:
    bid = (browser_id or "").lower()
    aliases = {bid}
    if bid in {"opera-gx", "opera_gx", "operagx"}:
        aliases.update({"opera", "opera-gx", "operagx"})
    if bid == "opera":
        aliases.add("opera-gx")
    return aliases


def _pick_browser_clients(
    clients: set[Any],
    meta: dict[Any, dict],
    browser_id: str,
    *,
    primary: Any | None = None,
) -> set[Any]:
    if not clients:
        return set()
    aliases = _browser_aliases(browser_id)
    pool = set(clients)
    if browser_id:
        matched = {client for client in clients if meta.get(client, {}).get("browser") in aliases}
        if matched:
            pool = matched
    # One client only — extra sockets (probes/reconnect ghosts) race or drop commands.
    if primary in pool:
        return {primary}
    ranked = sorted(
        pool,
        key=lambda c: float(meta.get(c, {}).get("ts") or 0.0),
        reverse=True,
    )
    return {ranked[0]} if ranked else set()


hub = EventHub()
