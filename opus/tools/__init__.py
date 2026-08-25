from __future__ import annotations

from opus.tools.apps import open_url, run_app
from opus.tools.spotify import control_spotify, play_spotify
from opus.tools.browser import add_extension, browser_history, list_extensions, remove_extension
from opus.tools.browser_actions import browser_click, browser_navigate, browser_press, browser_read, browser_type
from opus.tools.defender import scan_path
from opus.tools.files import list_directory, open_path, read_file, search_files, write_file
from opus.tools.images import generate_png
from opus.tools.input_control import click, drag, focus_info, move_mouse, press_keys, scroll, type_keys, wait_ms
from opus.tools.keyboard import type_text
from opus.tools.record import game_bar_clip, game_bar_record, open_clips_folder, recorder
from opus.tools.screen import foreground_window
from opus.tools.vault import fill_login, list_logins, save_login
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
        "search_files",
        "Search the user's files by name. Default scope is the user profile folders.",
        _obj({"query": {"type": "string"}, "scope": {"type": "string", "enum": ["user", "all"]}}, ["query"]),
    ),
    _fn("read_file", "Read a text file from disk.", _obj({"path": {"type": "string"}}, ["path"])),
    _fn("list_directory", "List a folder.", _obj({"path": {"type": "string"}}, ["path"])),
    _fn("open_path", "Open a file or folder with the default Windows app.", _obj({"path": {"type": "string"}}, ["path"])),
    _fn(
        "write_file",
        "Save code or text to a new file in D:/Opus/Scripts. After saving, just say Done — never speak the filename or path.",
        _obj(
            {
                "content": {"type": "string", "description": "Full file contents"},
                "filename": {"type": "string", "description": "Optional file name"},
                "language": {"type": "string", "description": "python, powershell, javascript, bat, txt"},
            },
            ["content"],
        ),
    ),
    _fn(
        "type_text",
        "Type or paste text into Notes, Notepad, or the currently focused field. Use this for 'type hello'. Never refuse for lack of access.",
        _obj({"text": {"type": "string", "description": "Exact text to type"}}, ["text"]),
    ),
    _fn(
        "run_app",
        "Launch an application by name or path, for example notepad, opera, or a full exe path.",
        _obj({"name": {"type": "string"}}, ["name"]),
    ),
    _fn(
        "play_spotify",
        "Play or resume music through the connected Spotify account API. Use only when the user wants a song, artist, playlist, or Spotify itself. Never use this for games — pokemon, showdown, osu, mania, and chess are games, not songs.",
        _obj(
            {
                "query": {
                    "type": "string",
                    "description": "Song/artist/playlist/album to play, or empty string to resume Spotify",
                }
            },
            ["query"],
        ),
    ),
    _fn(
        "control_spotify",
        "Pause, skip, go back, or say what's playing on the connected Spotify account.",
        _obj(
            {
                "action": {
                    "type": "string",
                    "enum": ["pause", "skip", "previous", "now"],
                    "description": "pause, skip, previous, or now",
                }
            },
            ["action"],
        ),
    ),
    _fn(
        "open_url",
        "Open a URL in the user's preferred browser.",
        _obj({"url": {"type": "string"}}, ["url"]),
    ),
    _fn(
        "scan_path",
        "Copy a file or folder into Opus's isolated cyber box, extract archives there, and run Defender. Never extract or run the sample on the host PC.",
        _obj({"path": {"type": "string", "description": "File, folder, zip, or Downloads/Desktop"}}),
    ),
    _fn("start_recording", "Start capturing the desktop to a file in D:/Clips/Opus/Recordings.", _NONE),
    _fn("stop_recording", "Stop the current Opus desktop recording and save the file.", _NONE),
    _fn("save_clip", "Save the last about 30 seconds of screen as a clip in D:/Clips/Opus/Clips.", _NONE),
    _fn("screenshot", "Save a screenshot to D:/Clips/Opus/Clips.", _NONE),
    _fn("open_clips", "Open the D:/Clips/Opus folder.", _NONE),
    _fn("game_bar_clip", "Use Xbox Game Bar to save the last few seconds (Win+Alt+G).", _NONE),
    _fn("game_bar_record", "Toggle Xbox Game Bar recording (Win+Alt+R).", _NONE),
    _fn("foreground_window", "Get the title of the window in the foreground.", _NONE),
    _fn(
        "describe_screen",
        "Capture the screen and describe what the user is looking at.",
        _obj({"focus": {"type": "string", "description": "What to look for"}}),
    ),
    _fn("show_panel", "Show the Opus settings panel on screen.", _NONE),
    _fn("hide_panel", "Hide or close the Opus settings panel.", _NONE),
    _fn("restart_opus", "Close Opus and immediately start it again.", _NONE),
    _fn(
        "generate_png",
        "Generate a PNG image from a description and save it. Do not speak the filename.",
        _obj({"prompt": {"type": "string"}}, ["prompt"]),
    ),
    _fn(
        "browser_history",
        "Read recent history from Opera GX, Chrome, Edge, and Brave.",
        _obj({"query": {"type": "string", "description": "Optional search filter"}, "limit": {"type": "integer"}}),
    ),
    _fn("list_extensions", "List installed browser extensions.", _NONE),
    _fn(
        "add_extension",
        "Install and enable a browser extension by name in the user's default browser. Actually installs it; do not just open the store.",
        _obj({"query": {"type": "string"}}, ["query"]),
    ),
    _fn(
        "remove_extension",
        "Uninstall and unpin a browser extension by name.",
        _obj({"query": {"type": "string"}}, ["query"]),
    ),
    _fn(
        "save_login",
        "Save a website email and password in Opus's local vault. Never repeat the password out loud.",
        _obj(
            {
                "site": {"type": "string"},
                "email": {"type": "string"},
                "password": {"type": "string"},
                "url": {"type": "string"},
            },
            ["site", "email", "password"],
        ),
    ),
    _fn(
        "list_logins",
        "List website names from Opus, the browsers, and Windows Credential Manager. Names only, never passwords.",
        _NONE,
    ),
    _fn(
        "fill_login",
        "Type the saved email and password for a website into the focused login form. Uses Opus's vault, browser saved passwords, and Windows Credential Manager. Never speak the password.",
        _obj({"site": {"type": "string"}}, ["site"]),
    ),
    _fn(
        "get_weather",
        "Get the current weather for the user's PC location or an optional city.",
        _obj({"city": {"type": "string", "description": "Optional city name"}}),
    ),
    _fn("get_location", "Tell the user where their PC is located.", _NONE),
    _fn(
        "search_web",
        "Search the web for up-to-date facts, news, people, dates, prices, scores, and current information.",
        _obj({"query": {"type": "string"}, "limit": {"type": "integer"}}, ["query"]),
    ),
    _fn(
        "start_playing",
        "Play the open browser game. Follows the current tab: Pokémon Showdown, Web osu!mania, or chess. Do not use this for Spotify. Bare 'play' must not open osu unless that tab is already open.",
        _obj({"goal": {"type": "string", "description": "Optional goal, e.g. play showdown"}}),
    ),
    _fn(
        "start_showdown",
        "Play Pokémon Showdown at play.pokemonshowdown.com. Use when they say play pokemon, play showdown, or play Pokémon. Never search the web or Spotify.",
        _NONE,
    ),
    _fn(
        "start_chess",
        "Start the Stockfish chess engine on the active Lichess/Chess.com board. Plays full strength and only sacrifices when the winning line requires it.",
        _obj(
            {
                "color": {
                    "type": "string",
                    "enum": ["auto", "white", "black"],
                    "description": "Which side Opus plays",
                }
            }
        ),
    ),
    _fn("stop_playing", "Stop the Opus Game Agent or chess engine play loop.", _NONE),
    _fn(
        "mouse_click",
        "Click at screen coordinates (pixels from top-left of primary monitor).",
        _obj(
            {
                "x": {"type": "integer"},
                "y": {"type": "integer"},
                "button": {"type": "string", "enum": ["left", "right", "middle"]},
                "clicks": {"type": "integer"},
            },
            ["x", "y"],
        ),
    ),
    _fn(
        "mouse_move",
        "Move the mouse to screen coordinates.",
        _obj({"x": {"type": "integer"}, "y": {"type": "integer"}}, ["x", "y"]),
    ),
    _fn(
        "mouse_drag",
        "Drag the mouse from one screen point to another.",
        _obj(
            {
                "x1": {"type": "integer"},
                "y1": {"type": "integer"},
                "x2": {"type": "integer"},
                "y2": {"type": "integer"},
                "button": {"type": "string", "enum": ["left", "right", "middle"]},
            },
            ["x1", "y1", "x2", "y2"],
        ),
    ),
    _fn(
        "press_keys",
        "Press a key or chord, e.g. enter, escape, ctrl+z, w.",
        _obj({"keys": {"type": "string"}}, ["keys"]),
    ),
    _fn(
        "scroll_mouse",
        "Scroll the mouse wheel. Positive dy scrolls up.",
        _obj({"dx": {"type": "integer"}, "dy": {"type": "integer"}}),
    ),
    _fn(
        "browser_click",
        "Click in the active browser tab via Opus Bridge (CSS selector, visible text, or tab x/y).",
        _obj(
            {
                "selector": {"type": "string"},
                "text": {"type": "string"},
                "x": {"type": "integer"},
                "y": {"type": "integer"},
            }
        ),
    ),
    _fn(
        "browser_type",
        "Type into a field in the active browser tab via Opus Bridge.",
        _obj(
            {
                "text": {"type": "string"},
                "selector": {"type": "string"},
                "clear": {"type": "boolean"},
            },
            ["text"],
        ),
    ),
    _fn(
        "browser_navigate",
        "Navigate the active browser tab to a URL via Opus Bridge.",
        _obj({"url": {"type": "string"}}, ["url"]),
    ),
    _fn("browser_read", "Read the active browser tab title, URL, and page text via Opus Bridge.", _NONE),
    _fn("input_focus_info", "Report foreground window, mouse position, and screen size.", _NONE),
]

CHAT_TOOL_SCHEMAS = [
    _fn(
        "search_web",
        "Search the web for current facts before answering factual questions.",
        _obj({"query": {"type": "string"}, "limit": {"type": "integer"}}, ["query"]),
    ),
    _fn(
        "get_weather",
        "Get the current weather for the user's PC location or an optional city.",
        _obj({"city": {"type": "string", "description": "Optional city name"}}),
    ),
    _fn("get_location", "Tell the user where their PC is located.", _NONE),
]


def run_tool(name: str, arguments: dict, file_access: bool, default_browser: str = "", settings=None) -> str:
    if name in {"search_files", "read_file", "list_directory", "open_path", "write_file"} and not file_access:
        return "File access is disabled in the Opus panel."
    if name == "search_files":
        return search_files(arguments.get("query", ""), arguments.get("scope", "user"))
    if name == "read_file":
        return read_file(arguments.get("path", ""))
    if name == "list_directory":
        return list_directory(arguments.get("path", ""))
    if name == "open_path":
        return open_path(arguments.get("path", ""))
    if name == "write_file":
        return write_file(
            arguments.get("content", ""),
            arguments.get("filename", ""),
            arguments.get("language", "python"),
        )
    if name == "type_text":
        return type_text(arguments.get("text", ""))
    if name == "run_app":
        return run_app(arguments.get("name", ""))
    if name == "play_spotify":
        return play_spotify(arguments.get("query", ""), raw=arguments.get("query", ""))
    if name == "control_spotify":
        return control_spotify(arguments.get("action", "pause"))
    if name == "open_url":
        return open_url(arguments.get("url", ""), default_browser)
    if name == "scan_path":
        return scan_path(arguments.get("path", ""))
    if name == "start_recording":
        return recorder.start()
    if name == "stop_recording":
        return recorder.stop()
    if name == "save_clip":
        return recorder.clip()
    if name == "screenshot":
        return recorder.screenshot()
    if name == "open_clips":
        return open_clips_folder()
    if name == "game_bar_clip":
        return game_bar_clip()
    if name == "game_bar_record":
        return game_bar_record()
    if name == "foreground_window":
        info = foreground_window()
        return info.get("title") or "Unknown window"
    if name == "generate_png":
        return generate_png(arguments.get("prompt", ""))
    if name == "browser_history":
        return browser_history(arguments.get("query", ""), arguments.get("limit") or 25)
    if name == "list_extensions":
        return list_extensions()
    if name == "add_extension":
        return add_extension(arguments.get("query", ""), default_browser)
    if name == "remove_extension":
        return remove_extension(arguments.get("query", ""), default_browser)
    if name == "save_login":
        return save_login(
            arguments.get("site", ""),
            arguments.get("email", ""),
            arguments.get("password", ""),
            arguments.get("url", ""),
        )
    if name == "list_logins":
        return list_logins()
    if name == "fill_login":
        return fill_login(arguments.get("site", ""))
    if name == "get_weather":
        if settings is None:
            return "Weather isn't ready yet."
        return get_weather(settings, city=arguments.get("city", ""))
    if name == "get_location":
        if settings is None:
            return "Location isn't ready yet."
        return location_summary(settings)
    if name == "search_web":
        return search_web(arguments.get("query", ""), arguments.get("limit") or 5)
    if name == "start_playing":
        from opus.game.play_router import start_play
        from opus.hub import hub as event_hub

        if settings is None:
            return "Game agent isn't ready."
        url = event_hub.get_browser_context().get("url") or ""
        return start_play(settings, arguments.get("goal") or "", url=url)
    if name == "start_showdown":
        from opus.game.chess_player import chess_player
        from opus.game.mania_player import mania_player
        from opus.game.showdown_player import showdown_player
        from opus.game.agent import game_agent

        if settings is None:
            return "Showdown isn't ready."
        chess_player.stop("Switching to Showdown.")
        mania_player.stop("Switching to Showdown.")
        game_agent.stop("Switching to Showdown.")
        return showdown_player.start(settings, create_tab=True)
    if name == "start_chess":
        from opus.game.chess_player import chess_player
        from opus.game.mania_player import mania_player
        from opus.game.showdown_player import showdown_player

        if settings is None:
            return "Chess engine isn't ready."
        mania_player.stop("Switching to chess.")
        showdown_player.stop("Switching to chess.")
        return chess_player.start(settings, color=arguments.get("color") or "auto")
    if name == "stop_playing":
        from opus.game.agent import game_agent
        from opus.game.chess_player import chess_player
        from opus.game.mania_player import mania_player
        from opus.game.showdown_player import showdown_player

        chess_player.stop("Stopped chess.")
        mania_player.stop("Stopped mania.")
        showdown_player.stop("Stopped Showdown.")
        return game_agent.stop("Stopped playing.")
    if name == "mouse_click":
        return click(
            arguments.get("x"),
            arguments.get("y"),
            button=arguments.get("button") or "left",
            clicks=arguments.get("clicks") or 1,
        )
    if name == "mouse_move":
        return move_mouse(arguments.get("x"), arguments.get("y"))
    if name == "mouse_drag":
        return drag(
            arguments.get("x1"),
            arguments.get("y1"),
            arguments.get("x2"),
            arguments.get("y2"),
            button=arguments.get("button") or "left",
        )
    if name == "press_keys":
        return press_keys(arguments.get("keys", ""))
    if name == "scroll_mouse":
        return scroll(arguments.get("dx") or 0, arguments.get("dy") or 0)
    if name == "browser_click":
        return browser_click(
            selector=arguments.get("selector") or "",
            x=arguments.get("x"),
            y=arguments.get("y"),
            text=arguments.get("text") or "",
        )
    if name == "browser_type":
        return browser_type(
            text=arguments.get("text") or "",
            selector=arguments.get("selector") or "",
            clear=bool(arguments.get("clear")),
        )
    if name == "browser_navigate":
        return browser_navigate(arguments.get("url") or "")
    if name == "browser_read":
        return browser_read()
    if name == "input_focus_info":
        return focus_info()
    return f"Unknown tool: {name}"
