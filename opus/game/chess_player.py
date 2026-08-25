from __future__ import annotations

import random
import threading
import time
from typing import Callable

import chess

from opus.game.chess_board import read_board_from_browser, square_to_viewport
from opus.game.chess_engine import EngineMove, chess_engine
from opus.game.chess_history import BoardLedger
from opus.game.chess_memory import position_memory
from opus.game.chess_vision import read_board_from_screen
from opus.hub import hub
from opus.logutil import get_logger
from opus.settings import Settings
from opus.tools import input_control as controls

log = get_logger()


class ChessPlayer:
    """Continuous Stockfish play loop with a full board-state ledger."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._generation = 0
        self._running = False
        self._settings: Settings | None = None
        self._speak: Callable[[str], None] | None = None
        self._color: chess.Color | None = None
        self._engine = chess_engine
        self._ledger = BoardLedger()
        self._moves = 0
        self._last_event = ""
        self._mode = "bridge"  # bridge | vision
        self._bridge_misses = 0
        self._told_fallback = False
        self._last_attempt_uci = ""
        self._last_attempt_placement = ""
        self._same_attempt_count = 0

    @property
    def running(self) -> bool:
        return self._running

    @property
    def ledger(self) -> BoardLedger:
        return self._ledger

    def start(
        self,
        settings: Settings,
        *,
        color: str = "auto",
        speak: Callable[[str], None] | None = None,
    ) -> str:
        if not self._engine.available():
            return (
                "Stockfish is missing. Put stockfish.exe in "
                "%AppData%\\Opus\\engines\\ and try again."
            )

        # Fully stop any prior worker before starting another. Overlapping
        # chess threads were spam-clicking and crashing Stockfish/Opus.
        with self._lock:
            self._generation += 1
            gen = self._generation
            self._stop.set()
            old = self._thread

        if old is not None and old.is_alive() and old is not threading.current_thread():
            old.join(timeout=5.0)

        with self._lock:
            if self._generation != gen:
                return "Chess start was superseded."
            self._settings = settings
            self._speak = speak
            self._stop = threading.Event()
            self._running = True
            self._moves = 0
            self._last_event = ""
            self._mode = "bridge"
            self._bridge_misses = 0
            self._told_fallback = False
            self._last_attempt_uci = ""
            self._last_attempt_placement = ""
            self._same_attempt_count = 0
            self._color = _parse_color(color)
            self._thread = threading.Thread(
                target=self._loop,
                args=(gen,),
                name="opus-chess",
                daemon=True,
            )
            self._thread.start()
            log.info("chess player started color=%s gen=%s", self._color, gen)

        hub.set_status(mode="playing", message="Playing chess…")
        return "Playing chess."

    def stop(self, reason: str = "Stopped chess.") -> str:
        with self._lock:
            self._generation += 1
            self._stop.set()
            old = self._thread
        if old is not None and old.is_alive() and old is not threading.current_thread():
            old.join(timeout=3.0)
        path = None
        try:
            path = self._ledger.finish(result=reason)
        except Exception:
            log.exception("chess ledger finish failed")
        try:
            self._engine.close()
        except Exception:
            pass
        with self._lock:
            self._running = False
            self._thread = None
        if hub.status.get("mode") == "playing":
            hub.set_status(mode="idle", message=reason[:120])
        if path:
            log.info("chess game saved %s", path)
        return reason

    def _announce(self, text: str) -> None:
        if self._speak:
            try:
                self._speak(text)
            except Exception:
                log.exception("chess announce failed")

    def _alive(self, gen: int) -> bool:
        return (not self._stop.is_set()) and self._generation == gen

    def _loop(self, gen: int) -> None:
        settings = self._settings
        assert settings is not None
        movetime = max(3000, int(settings.get("chess_movetime_ms") or 4000))
        min_depth = int(settings.get("chess_memory_min_depth") or 12)
        initialized = False
        between_games = False
        last_url = ""
        engine_fail_streak = 0
        try:
            log.info("chess loop begin gen=%s", gen)
            try:
                self._engine.ensure()
            except Exception:
                log.exception("stockfish failed to start")
                hub.set_status(mode="idle", message="Chess engine failed to start.")
                return
            idle_turns = 0
            ticks = 0
            try:
                hub.browser_command("chess_reset", {}, timeout=4.0)
            except Exception:
                pass
            while self._alive(gen):
                ticks += 1
                if ticks == 1 or ticks % 15 == 0:
                    log.info(
                        "chess tick n=%s idle=%s between=%s bridge=%s gen=%s",
                        ticks,
                        idle_turns,
                        between_games,
                        hub.browser_connected(),
                        gen,
                    )
                snapshot = self._read_board(settings)
                if not self._alive(gen):
                    break
                if not snapshot.get("ok"):
                    idle_turns += 1
                    if idle_turns == 1:
                        log.info("chess waiting for board: %s", snapshot.get("error"))
                        hub.set_status(
                            mode="playing",
                            message="Looking for a chess board…",
                        )
                    time.sleep(0.9)
                    continue

                fen = str(snapshot.get("fen") or "")
                orientation = str(snapshot.get("orientation") or "white").lower()
                site = str(snapshot.get("source") or "")
                url = str(snapshot.get("url") or "")
                turn_hint = ""
                parts = fen.split()
                if len(parts) > 1:
                    turn_hint = parts[1]
                placement = parts[0] if parts else ""

                new_game = False
                if _is_starting_placement(placement):
                    if (not initialized) or between_games or self._ledger.ply > 2:
                        new_game = True
                elif between_games and url and url != last_url:
                    new_game = True
                elif between_games and _looks_like_fresh_game(placement, self._ledger.ply):
                    new_game = True

                if new_game or not initialized:
                    if self._color is None or new_game:
                        self._color = (
                            chess.WHITE if orientation.startswith("w") else chess.BLACK
                        )
                    assert self._color is not None
                    if initialized and between_games:
                        self._announce("New game — playing.")
                    self._ledger.reset(self._color, site=site, url=url)
                    initialized = True
                    between_games = False
                    last_url = url
                    self._last_attempt_uci = ""
                    self._last_attempt_placement = ""
                    self._same_attempt_count = 0
                    board, event = self._ledger.sync_from_observation(
                        fen,
                        orientation=orientation,
                        site=site,
                        url=url,
                        turn_hint=turn_hint,
                        unstable=bool(snapshot.get("unstable")),
                    )
                    self._last_event = event
                    hub.set_status(
                        mode="playing",
                        message=f"Chess live — {self._ledger.summary()}"[:120],
                    )
                    log.info("chess game ready %s", self._ledger.summary())
                    if event not in {"our_turn", "resync"} or board.turn != self._color:
                        time.sleep(0.35)
                        continue
                else:
                    board, event = self._ledger.sync_from_observation(
                        fen,
                        orientation=orientation,
                        site=site,
                        url=url,
                        turn_hint=turn_hint,
                        unstable=bool(snapshot.get("unstable")),
                    )
                    if event != self._last_event:
                        self._last_event = event
                        log.info("chess ledger event=%s %s", event, self._ledger.summary())
                    last_url = url or last_url

                if board.is_game_over():
                    outcome = board.outcome()
                    msg = "Game over."
                    if outcome and outcome.winner is not None:
                        if outcome.winner == self._color:
                            msg = "Checkmate — we win."
                        else:
                            msg = "Checkmate — we lost."
                    elif outcome and outcome.winner is None:
                        msg = "Draw."
                    self._announce(msg)
                    try:
                        path = self._ledger.finish(result=msg)
                        if path:
                            log.info("chess game saved %s", path)
                    except Exception:
                        log.exception("chess ledger finish failed")
                    between_games = True
                    hub.set_status(
                        mode="playing",
                        message="Game over — waiting for the next game…",
                    )
                    time.sleep(1.0)
                    continue

                if between_games:
                    time.sleep(0.8)
                    continue

                if board.turn != self._color or event in {"wait", "opponent_moved"}:
                    idle_turns = 0
                    self._same_attempt_count = 0
                    time.sleep(0.3)
                    continue

                if board.status() != chess.STATUS_VALID:
                    log.warning("chess skipping illegal board %s", board.fen())
                    time.sleep(0.6)
                    continue

                try:
                    choice = self._engine.choose_move(
                        board,
                        movetime_ms=movetime,
                        multipv=3,
                        min_memory_depth=min_depth,
                        use_memory=True,
                    )
                    engine_fail_streak = 0
                except Exception:
                    engine_fail_streak += 1
                    if engine_fail_streak <= 2 or engine_fail_streak % 10 == 0:
                        log.exception(
                            "engine choose failed fen=%s streak=%s",
                            board.fen(),
                            engine_fail_streak,
                        )
                    try:
                        self._engine.close()
                        if self._alive(gen):
                            self._engine.ensure()
                    except Exception:
                        pass
                    time.sleep(min(2.0, 0.5 * engine_fail_streak))
                    continue

                if not self._alive(gen):
                    break

                # Don't spam the same click when the board hasn't moved — that
                # was crashing Stockfish (0xC0000005) and taking Opus with it.
                attempt_key = choice.move.uci()
                if (
                    attempt_key == self._last_attempt_uci
                    and placement == self._last_attempt_placement
                ):
                    self._same_attempt_count += 1
                    if self._same_attempt_count >= 2:
                        log.info(
                            "chess waiting for board to accept %s (attempt %s)",
                            attempt_key,
                            self._same_attempt_count,
                        )
                        hub.set_status(
                            mode="playing",
                            message=f"Waiting for {choice.san} to land…",
                        )
                        time.sleep(min(3.0, 0.8 * self._same_attempt_count))
                        continue
                else:
                    self._same_attempt_count = 0
                self._last_attempt_uci = attempt_key
                self._last_attempt_placement = placement

                rect = snapshot.get("board_rect")
                if not isinstance(rect, dict) or not rect.get("width"):
                    time.sleep(0.8)
                    continue

                think_s = _human_think_delay(board, choice, settings)
                if think_s > 0.05:
                    hub.set_status(
                        mode="playing",
                        message=f"Thinking… ({think_s:.1f}s) — {choice.san}"[:120],
                    )
                    log.info(
                        "chess think %.1fs before %s (%s)",
                        think_s,
                        choice.san,
                        choice.reason,
                    )
                    deadline = time.monotonic() + think_s
                    while self._alive(gen) and time.monotonic() < deadline:
                        time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
                    if not self._alive(gen):
                        break

                self._ledger.mark_our_move_pending(choice.move)
                ok = self._play_move(
                    board,
                    choice.move,
                    rect,
                    orientation,
                    prefer_os_click=(
                        self._mode == "vision" or snapshot.get("source") == "vision"
                    ),
                )
                if not ok:
                    time.sleep(0.4)
                    continue

                time.sleep(0.45)
                confirm = read_board_from_browser()
                if confirm.get("ok"):
                    _board2, event2 = self._ledger.sync_from_observation(
                        str(confirm.get("fen") or ""),
                        orientation=str(confirm.get("orientation") or orientation),
                        site=str(confirm.get("source") or site),
                        url=str(confirm.get("url") or url),
                    )
                    if event2 == "wait" and self._ledger.pending_uci():
                        snap = self._ledger.apply_our_move(choice.move, note=choice.reason)
                        if snap:
                            log.info("chess applied pending %s", snap.move_san)
                    self._last_event = event2
                    confirm_placement = str(confirm.get("fen") or "").split()[0]
                    if confirm_placement != placement:
                        self._same_attempt_count = 0
                        self._last_attempt_uci = ""
                else:
                    self._ledger.apply_our_move(choice.move, note=choice.reason)
                    self._same_attempt_count = 0
                    self._last_attempt_uci = ""

                self._moves += 1
                idle_turns = 0
                known = position_memory.count()
                status = (
                    f"Chess ply {self._ledger.ply}: {choice.san} — {choice.reason} "
                    f"[{known} remembered]"
                )[:120]
                hub.set_status(mode="playing", message=status)
                log.info("chess moved %s (%s)", choice.san, choice.reason)
                time.sleep(0.4)
        except Exception:
            log.exception("chess player crashed gen=%s", gen)
            if hub.status.get("mode") == "playing":
                hub.set_status(mode="idle", message="Chess stopped on error.")
        finally:
            with self._lock:
                if self._generation == gen and self._thread is threading.current_thread():
                    self._running = False
                    self._thread = None

    def _read_board(self, settings: Settings) -> dict:
        try:
            snapshot = read_board_from_browser()
        except Exception:
            log.exception("chess browser read failed")
            snapshot = {"ok": False, "error": "browser read exception"}
        if snapshot.get("ok"):
            self._bridge_misses = 0
            self._mode = "bridge"
            return snapshot

        err = str(snapshot.get("error") or "")
        bridge_up = hub.browser_connected()
        if bridge_up and "not connected" not in err.lower():
            self._mode = "bridge"
            if not self._told_fallback:
                self._told_fallback = True
                log.info("chess bridge scrape miss (keeping bridge): %s", err)
            return snapshot

        self._bridge_misses += 1
        if self._bridge_misses >= 2:
            if not self._told_fallback:
                self._told_fallback = True
                log.info("chess falling back to screen vision (%s)", err)
                self._announce("Bridge is offline. Trying screen vision.")
            try:
                vision = read_board_from_screen(settings)
            except Exception as exc:
                log.exception("chess vision fallback failed")
                return {"ok": False, "error": str(exc)}
            if vision.get("ok"):
                self._mode = "vision"
                return vision
            return vision
        return snapshot

    def _play_move(
        self,
        board: chess.Board,
        move: chess.Move,
        board_rect: dict,
        orientation: str,
        *,
        prefer_os_click: bool = False,
    ) -> bool:
        uci = move.uci()
        if not prefer_os_click:
            result = hub.browser_command(
                "chess_move",
                {
                    "from": uci[:2],
                    "to": uci[2:4],
                    "promotion": uci[4:] if len(uci) > 4 else "",
                    "orientation": orientation,
                },
                timeout=15.0,
            )
            if result.get("ok"):
                return True
            log.info("chess_move bridge failed: %s", result.get("error"))
            if hub.browser_connected() or not _foreground_looks_like_chess():
                return False

        try:
            x1, y1 = square_to_viewport(move.from_square, board_rect, orientation)
            x2, y2 = square_to_viewport(move.to_square, board_rect, orientation)
            controls.click(int(x1), int(y1))
            time.sleep(random.uniform(0.22, 0.55))
            controls.click(int(x2), int(y2))
            if move.promotion:
                time.sleep(random.uniform(0.2, 0.4))
                controls.click(int(x2), int(y2) - 12)
            return True
        except Exception:
            log.exception("chess click failed")
            return False


def _is_starting_placement(placement: str) -> bool:
    return placement == chess.STARTING_BOARD_FEN.split()[0]


def _looks_like_fresh_game(placement: str, prior_ply: int) -> bool:
    if prior_ply < 4:
        return False
    if placement in {
        "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR",
        "rnbqkbnr/pppppppp/8/8/3P4/8/PPP1PPPP/RNBQKBNR",
        "rnbqkbnr/pppppppp/8/8/8/5N2/PPPPPPPP/RNBQKB1R",
    }:
        return True
    pieces = sum(1 for ch in placement if ch.isalpha())
    return pieces >= 30 and prior_ply >= 10


def _foreground_looks_like_chess() -> bool:
    try:
        from opus.tools.screen import foreground_window

        title = str(foreground_window().get("title") or "").lower()
    except Exception:
        return False
    if "discord" in title:
        return False
    return any(n in title for n in ("chess.com", "lichess", "play chess", "chess"))


def _human_think_delay(board: chess.Board, choice: EngineMove, settings: Settings) -> float:
    min_s = float(settings.get("chess_think_min_s") or 1.2)
    max_s = float(settings.get("chess_think_max_s") or 14.0)
    if max_s < min_s:
        max_s = min_s

    legal = board.legal_moves.count()
    difficulty = 0.12 * min(legal, 45)

    if board.is_check():
        difficulty += 1.8
    if choice.mate is not None and 0 < choice.mate <= 2:
        difficulty *= 0.2
        difficulty += 0.4
    elif choice.mate is not None and choice.mate > 0:
        difficulty += 0.8

    if choice.material_delta <= -80:
        difficulty += 2.8
    elif choice.is_capture:
        difficulty += 0.35
    if choice.gives_check:
        difficulty += 0.55

    abs_cp = abs(int(choice.score_cp or 0))
    if abs_cp >= 600:
        difficulty *= 0.5
    elif abs_cp <= 70:
        difficulty += 2.0

    reason = (choice.reason or "").lower()
    if "opening" in reason or "perfect recall" in reason:
        difficulty *= 0.4

    pieces = len(board.piece_map())
    if pieces <= 10:
        difficulty += 1.4
    elif pieces >= 28 and board.fullmove_number <= 10:
        difficulty *= 0.65

    delay = min_s + difficulty
    delay = max(min_s, min(max_s, delay))
    delay *= random.uniform(0.88, 1.18)
    return round(delay, 2)


def _parse_color(value: str) -> chess.Color | None:
    text = (value or "auto").strip().lower()
    if text in {"w", "white"}:
        return chess.WHITE
    if text in {"b", "black"}:
        return chess.BLACK
    return None


chess_player = ChessPlayer()
