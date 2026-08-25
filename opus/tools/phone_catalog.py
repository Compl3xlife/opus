"""Phone-safe tools: APIs only. No games, window clicking, or Windows Defender."""
from __future__ import annotations

from opus.tools.spotify import control_spotify, play_spotify
from opus.tools.weather import get_weather, location_summary
from opus.tools.web import search_web


def _obj(properties: dict, required: list[str] | None = None) -> dict:
    schema = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = required
    return schema


def _fn(name: str, description: str, parameters: dict) -> dict:
    return {
        "type": "function",
        "function": {"name": name, "description": description, "parameters": parameters},
    }


_NONE = _obj({"reason": {"type": "string", "description": "Optional note"}})

TOOL_SCHEMAS = [
    _fn(
        "play_spotify",
        "Play or resume music through the connected Spotify account API.",
        _obj(
            {
                "query": {
                    "type": "string",
                    "description": "Song, artist, playlist, or album. Empty string resumes.",
                }
            },
            ["query"],
        ),
    ),
    _fn(
        "control_spotify",
        "Pause, skip, go back, or say what's playing on Spotify.",
        _obj(
            {
                "action": {
                    "type": "string",
                    "enum": ["pause", "skip", "previous", "now"],
                }
            },
            ["action"],
        ),
    ),
    _fn(
        "search_web",
        "Search the web for current facts before answering factual questions.",
        _obj({"query": {"type": "string"}, "limit": {"type": "integer"}}, ["query"]),
    ),
    _fn(
        "get_weather",
        "Get the current weather for the user's location or an optional city.",
        _obj({"city": {"type": "string", "description": "Optional city name"}}),
    ),
    _fn("get_location", "Tell the user where they are.", _NONE),
]

CHAT_TOOL_SCHEMAS = TOOL_SCHEMAS


def run_tool(name: str, arguments: dict, file_access: bool, default_browser: str = "", settings=None) -> str:
    if name == "play_spotify":
        return play_spotify(arguments.get("query", ""), raw=arguments.get("query", ""))
    if name == "control_spotify":
        return control_spotify(arguments.get("action", "pause"))
    if name == "search_web":
        return search_web(arguments.get("query", ""), arguments.get("limit") or 5)
    if name == "get_weather":
        if settings is None:
            return "Weather isn't ready yet."
        return get_weather(settings, city=arguments.get("city", ""))
    if name == "get_location":
        if settings is None:
            return "Location isn't ready yet."
        return location_summary(settings)
    return "That action is only on the PC version of Opus."
