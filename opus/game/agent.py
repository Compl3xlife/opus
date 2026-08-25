from __future__ import annotations

import base64
import json
import re
import threading
import time
from typing import Any, Callable

from opus.api_pool import ApiPool, is_rate_limit
from opus.hub import hub
from opus.logutil import get_logger
from opus.settings import Settings
from opus.tools import browser_actions
from opus.tools import input_control as controls
from opus.tools.screen import capture_screen, foreground_window, map_image_to_screen

log = get_logger()

PLAY_SYSTEM = """You are Opus Game Agent — a master-tier strategy / turn-based / browser-game player.
You see a screenshot of the user's game. Choose the strongest legal play every turn.

Rules:
- Play to WIN at a high competitive level: long-term planning, tempo, resource efficiency, forcing lines.
- Prefer decisive winning moves over safe mediocre ones when the win is clear.
- If it is not your turn / waiting / animating, use wait.
- If a menu or dialog blocks play, dismiss or confirm it first.
- Coordinates are in IMAGE pixels (origin top-left of the provided screenshot).
- Prefer browser_* actions when the game is clearly in a browser tab and selectors/text are reliable.
- Output ONLY valid JSON (no markdown) with this shape:
{
  "game": "short name of the game if known",
  "phase": "what is happening now",
  "plan": "one sentence strategy for this step",
  "actions": [
    {"type":"click","x":0,"y":0,"button":"left","clicks":1},
    {"type":"drag","x1":0,"y1":0,"x2":0,"y2":0},
    {"type":"key","keys":"enter"},
    {"type":"hotkey","keys":"ctrl+z"},
    {"type":"type","text":"..."},
    {"type":"scroll","dx":0,"dy":-3},
    {"type":"browser_click","selector":"...","text":"...","x":0,"y":0},
    {"type":"browser_type","text":"...","selector":"...","clear":false},
    {"type":"browser_press","key":"Enter"},
    {"type":"wait","ms":400},
    {"type":"done","reason":"..."},
    {"type":"fail","reason":"..."}
  ]
}
- Emit 1–4 actions max per step. Prefer one strong action + short wait.
- Use done only when the match/game is clearly finished or no further play is possible.
- Use fail only if the screen is not a playable game or you cannot act safely.
"""

JSON_RE = re.compile(r"\{[\s\S]*\}")


class GameAgent:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._running = False
        self._goal = ""
        self._settings: Settings | None = None
        self._speak: Callable[[str], None] | None = None
        self._step = 0
        self._last_plan = ""

    @property
    def running(self) -> bool:
        return self._running

    def status(self) -> dict:
        return {
            "running": self._running,
            "step": self._step,
            "goal": self._goal,
            "plan": self._last_plan,
        }

    def start(
        self,
        settings: Settings,
        goal: str = "",
        *,
        speak: Callable[[str], None] | None = None,
    ) -> str:
        with self._lock:
            if self._running:
                return "Already playing. Say stop playing to halt."
            self._settings = settings
            self._goal = (goal or "").strip() or "Play this game at a master competitive level and win."
            self._speak = speak
            self._stop.clear()
            self._running = True
            self._step = 0
            self._last_plan = ""
            self._thread = threading.Thread(target=self._loop, name="opus-game-agent", daemon=True)
            self._thread.start()
        hub.set_status(mode="playing", message="Playing — say Opus, stop playing to halt.")
        return "Taking control. I'll play this at a high level. Say stop playing when you want me to stop."

    def stop(self, reason: str = "Stopped.") -> str:
        self._stop.set()
        with self._lock:
            self._running = False
        hub.set_status(mode="idle", message=reason[:120])
        return reason

    def _loop(self) -> None:
        settings = self._settings
        assert settings is not None
        pool = ApiPool(settings)
        max_steps = int(settings.get("game_max_steps") or 250)
        delay_ms = int(settings.get("game_step_delay_ms") or 350)
        try:
            while not self._stop.is_set() and self._step < max_steps:
                self._step += 1
                hub.set_status(
                    mode="playing",
                    message=f"Playing step {self._step}…",
                )
                try:
                    png, meta = capture_screen(max_width=1280)
                except Exception:
                    log.exception("game capture failed")
                    time.sleep(0.8)
                    continue
                plan = self._plan_move(pool, settings, png, meta)
                if not plan:
                    time.sleep(max(0.2, delay_ms / 1000.0))
                    continue
                self._last_plan = str(plan.get("plan") or plan.get("phase") or "")[:200]
                actions = plan.get("actions")
                if not isinstance(actions, list) or not actions:
                    time.sleep(max(0.2, delay_ms / 1000.0))
                    continue
                stop_loop = False
                for action in actions[:4]:
                    if self._stop.is_set():
                        stop_loop = True
                        break
                    if not isinstance(action, dict):
                        continue
                    kind = str(action.get("type") or "").lower()
                    if kind == "done":
                        msg = str(action.get("reason") or "Game finished.")
                        self._announce(msg)
                        self.stop(msg)
                        return
                    if kind == "fail":
                        msg = str(action.get("reason") or "I can't play this screen.")
                        self._announce(msg)
                        self.stop(msg)
                        return
                    try:
                        self._execute(action, meta)
                    except Exception:
                        log.exception("game action failed: %s", action)
                if stop_loop:
                    break
                time.sleep(max(0.15, delay_ms / 1000.0))
            if self._step >= max_steps and not self._stop.is_set():
                self._announce("Hit the step limit. Say play again if you want me to continue.")
                self.stop("Step limit reached.")
        except Exception:
            log.exception("game agent crashed")
            self.stop("Game agent stopped on error.")
        finally:
            with self._lock:
                self._running = False
            if hub.status.get("mode") == "playing":
                hub.set_status(mode="idle", message="Say “Opus” to wake me.")

    def _announce(self, text: str) -> None:
        if self._speak:
            try:
                self._speak(text)
            except Exception:
                log.exception("game announce failed")

    def _plan_move(self, pool: ApiPool, settings: Settings, png: bytes, meta: dict) -> dict | None:
        if not pool.has_keys():
            self._announce("Add an API key so I can play.")
            self.stop("Missing API key.")
            return None
        image = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
        window = foreground_window()
        browser = hub.get_browser_context()
        user_prompt = (
            f"Goal: {self._goal}\n"
            f"Step: {self._step}\n"
            f"Foreground window: {window.get('title') or 'Unknown'}\n"
            f"Browser tab: {browser.get('title') or ''} | {browser.get('url') or ''}\n"
            f"Image size: {meta.get('image_width')}x{meta.get('image_height')} "
            f"(native {meta.get('native_width')}x{meta.get('native_height')})\n"
            f"Page text snippet:\n{(browser.get('page_text') or '')[:1500]}\n"
            "Return JSON actions now."
        )
        models = self._vision_models(settings)
        last_error: Exception | None = None
        for model in models:
            for _ in range(pool.max_attempts()):
                if self._stop.is_set():
                    return None
                client, _key, base_url, provider = pool.next_client()
                resolved = self._resolve_vision_model(pool, model, provider, base_url)
                try:
                    response = client.chat.completions.create(
                        model=resolved,
                        messages=[
                            {"role": "system", "content": PLAY_SYSTEM},
                            {
                                "role": "user",
                                "content": [
                                    {"type": "text", "text": user_prompt},
                                    {"type": "image_url", "image_url": {"url": image}},
                                ],
                            },
                        ],
                        temperature=0.15,
                        max_tokens=700,
                    )
                    raw = (response.choices[0].message.content or "").strip()
                    parsed = _parse_json(raw)
                    if parsed:
                        return parsed
                    log.warning("game plan not JSON: %s", raw[:300])
                    return None
                except Exception as exc:
                    last_error = exc
                    log.exception("game plan failed model=%s", resolved)
                    if is_rate_limit(exc):
                        continue
                    break
        if last_error:
            log.error("game planning exhausted: %s", last_error)
        return None

    def _vision_models(self, settings: Settings) -> list[str]:
        configured = (settings.get("vision_model") or "").strip()
        models: list[str] = []
        if configured:
            models.append(configured)
        for name in ("gpt-4o", "qwen/qwen3.6-27b"):
            if name not in models:
                models.append(name)
        return models

    def _resolve_vision_model(self, pool: ApiPool, preferred: str, provider: str, base_url: str) -> str:
        override = pool.model_for_provider(provider, "vision")
        if override:
            return override
        if provider == "fallback" and "groq.com" not in (base_url or ""):
            if preferred.startswith("qwen/"):
                return "gpt-4o"
        return preferred

    def _execute(self, action: dict[str, Any], meta: dict) -> str:
        kind = str(action.get("type") or "").lower()
        if kind == "click":
            x, y = map_image_to_screen(float(action["x"]), float(action["y"]), meta)
            return controls.click(x, y, button=str(action.get("button") or "left"), clicks=int(action.get("clicks") or 1))
        if kind == "drag":
            x1, y1 = map_image_to_screen(float(action["x1"]), float(action["y1"]), meta)
            x2, y2 = map_image_to_screen(float(action["x2"]), float(action["y2"]), meta)
            return controls.drag(x1, y1, x2, y2, button=str(action.get("button") or "left"))
        if kind == "key":
            return controls.press_keys(str(action.get("keys") or action.get("key") or ""))
        if kind == "hotkey":
            return controls.press_keys(str(action.get("keys") or ""))
        if kind == "type":
            return controls.type_keys(str(action.get("text") or ""))
        if kind == "scroll":
            return controls.scroll(int(action.get("dx") or 0), int(action.get("dy") or 0))
        if kind == "wait":
            return controls.wait_ms(int(action.get("ms") or 400))
        if kind == "browser_click":
            return browser_actions.browser_click(
                selector=str(action.get("selector") or ""),
                x=action.get("x"),
                y=action.get("y"),
                text=str(action.get("text") or ""),
            )
        if kind == "browser_type":
            return browser_actions.browser_type(
                text=str(action.get("text") or ""),
                selector=str(action.get("selector") or ""),
                clear=bool(action.get("clear")),
            )
        if kind == "browser_press":
            return browser_actions.browser_press(str(action.get("key") or "Enter"))
        if kind == "browser_navigate":
            return browser_actions.browser_navigate(str(action.get("url") or ""))
        return f"Unknown action: {kind}"


def _parse_json(raw: str) -> dict | None:
    text = (raw or "").strip()
    if not text:
        return None
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        match = JSON_RE.search(text)
        if not match:
            return None
        try:
            data = json.loads(match.group(0))
            return data if isinstance(data, dict) else None
        except json.JSONDecodeError:
            return None


game_agent = GameAgent()
