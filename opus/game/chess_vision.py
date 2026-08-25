from __future__ import annotations

import base64
import json
import re
from typing import Any

from opus.api_pool import ApiPool, is_rate_limit
from opus.logutil import get_logger
from opus.settings import Settings
from opus.tools.screen import capture_screen

log = get_logger()

_JSON_RE = re.compile(r"\{[\s\S]*\}")

VISION_PROMPT = """You are reading a chess board on a PC screenshot.
Return ONLY JSON (no markdown) with this shape:
{
  "ok": true,
  "fen": "piece placement + side to move, e.g. rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w",
  "orientation": "white" or "black" (which color is at the bottom of the board),
  "our_turn": true/false (true if the bottom side should move now),
  "board_rect": {"left": 0, "top": 0, "width": 0, "height": 0}
}
Rules:
- fen must be valid piece placement for an 8x8 board. Include side to move (w or b).
- board_rect is the chessboard bounding box in IMAGE pixels (origin top-left of this screenshot).
- If no chess board is visible, return {"ok": false, "error": "no board"}.
- Prefer the largest clearly visible chess board.
"""


def read_board_from_screen(settings: Settings) -> dict[str, Any]:
    """Vision fallback when Opus Bridge is not connected."""
    try:
        png, meta = capture_screen(max_width=1280)
    except Exception as exc:
        log.exception("chess vision capture failed")
        return {"ok": False, "error": f"screenshot failed: {exc}"}

    pool = ApiPool(settings)
    if not pool.has_keys():
        return {"ok": False, "error": "No API key for board vision."}

    image = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
    models = _vision_models(settings)
    last_error: Exception | None = None
    for model in models:
        for _ in range(pool.max_attempts()):
            client, _key, base_url, provider = pool.next_client()
            resolved = _resolve_vision(pool, model, provider, base_url)
            try:
                response = client.chat.completions.create(
                    model=resolved,
                    messages=[
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": VISION_PROMPT},
                                {"type": "image_url", "image_url": {"url": image}},
                            ],
                        }
                    ],
                    temperature=0.0,
                    max_tokens=400,
                )
                raw = (response.choices[0].message.content or "").strip()
                data = _parse_json(raw)
                if not data:
                    return {"ok": False, "error": "Vision returned no JSON."}
                if not data.get("ok"):
                    return {"ok": False, "error": data.get("error") or "no board"}
                fen = str(data.get("fen") or "").strip()
                if not fen:
                    return {"ok": False, "error": "Vision omitted FEN."}
                # Normalize fen to 4+ fields
                parts = fen.split()
                if len(parts) == 1:
                    fen = f"{parts[0]} w - - 0 1"
                elif len(parts) == 2:
                    fen = f"{parts[0]} {parts[1]} - - 0 1"
                rect = data.get("board_rect") or {}
                # Map image-space rect → screen pixels
                scale = float(meta.get("scale") or 1.0) or 1.0
                left = int(meta.get("monitor_left") or 0)
                top = int(meta.get("monitor_top") or 0)
                screen_rect = {
                    "left": int(round(float(rect.get("left", 0)) / scale)) + left,
                    "top": int(round(float(rect.get("top", 0)) / scale)) + top,
                    "width": int(round(float(rect.get("width", 0)) / scale)),
                    "height": int(round(float(rect.get("height", 0)) / scale)),
                    "x": int(round(float(rect.get("left", 0)) / scale)) + left,
                    "y": int(round(float(rect.get("top", 0)) / scale)) + top,
                }
                orientation = str(data.get("orientation") or "white").lower()
                our_turn = bool(data.get("our_turn"))
                # Align fen side-to-move with our_turn + orientation when possible.
                turn = "w"
                if len(fen.split()) > 1:
                    turn = fen.split()[1]
                bottom_is_white = orientation.startswith("w")
                if our_turn:
                    turn = "w" if bottom_is_white else "b"
                else:
                    turn = "b" if bottom_is_white else "w"
                bits = fen.split()
                bits[1] = turn
                fen = " ".join(bits)
                return {
                    "ok": True,
                    "fen": fen,
                    "orientation": orientation,
                    "source": "vision",
                    "url": "",
                    "board_rect": screen_rect,
                    "image_meta": meta,
                    "piece_count": 0,
                }
            except Exception as exc:
                last_error = exc
                log.exception("chess vision failed model=%s", resolved)
                if is_rate_limit(exc):
                    continue
                break
    return {"ok": False, "error": f"vision failed: {last_error}"}


def _vision_models(settings: Settings) -> list[str]:
    configured = (settings.get("vision_model") or "").strip()
    models: list[str] = []
    if configured:
        models.append(configured)
    for name in ("gpt-4o", "qwen/qwen3.6-27b"):
        if name not in models:
            models.append(name)
    return models


def _resolve_vision(pool: ApiPool, preferred: str, provider: str, base_url: str) -> str:
    override = pool.model_for_provider(provider, "vision")
    if override:
        return override
    if provider == "fallback" and "groq.com" not in (base_url or ""):
        if preferred.startswith("qwen/"):
            return "gpt-4o"
    return preferred


def _parse_json(raw: str) -> dict | None:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        match = _JSON_RE.search(text)
        if not match:
            return None
        try:
            data = json.loads(match.group(0))
            return data if isinstance(data, dict) else None
        except json.JSONDecodeError:
            return None
