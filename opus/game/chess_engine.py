from __future__ import annotations

import os
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

import chess
import chess.engine

from opus.logutil import get_logger
from opus.settings import appdata_dir
from opus.game.chess_memory import position_key, position_memory

log = get_logger()

# Centipawn values for material accounting (Stockfish-ish).
_PIECE_CP = {
    chess.PAWN: 100,
    chess.KNIGHT: 320,
    chess.BISHOP: 330,
    chess.ROOK: 500,
    chess.QUEEN: 900,
    chess.KING: 0,
}


@dataclass
class EngineMove:
    move: chess.Move
    score_cp: int
    mate: int | None
    material_delta: int
    gives_check: bool
    is_capture: bool
    san: str
    reason: str


def stockfish_path() -> Path:
    engines = appdata_dir() / "engines"
    engines.mkdir(parents=True, exist_ok=True)
    bundled = engines / "stockfish.exe"
    if bundled.exists():
        return bundled
    which = shutil.which("stockfish") or shutil.which("stockfish.exe")
    if which:
        return Path(which)
    return bundled


class StrengthChessEngine:
    """Full-strength Stockfish. Sacrifices only when the eval says they are best."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._engine: chess.engine.SimpleEngine | None = None
        self._path: Path | None = None

    def available(self) -> bool:
        path = stockfish_path()
        return path.exists()

    def ensure(self) -> chess.engine.SimpleEngine:
        with self._lock:
            return self._ensure_unlocked()

    def _ensure_unlocked(self) -> chess.engine.SimpleEngine:
        if self._engine is not None:
            return self._engine
        return self._spawn_unlocked()

    def _spawn_unlocked(self) -> chess.engine.SimpleEngine:
        path = stockfish_path()
        if not path.exists():
            raise FileNotFoundError(
                f"Stockfish not found at {path}. Place stockfish.exe there."
            )
        self._path = path
        kwargs = {}
        if os.name == "nt":
            # Avoid console-window spawn under pythonw, which can hard-kill Opus.
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        self._engine = chess.engine.SimpleEngine.popen_uci(str(path), **kwargs)
        # Threads > 1 has segfaulted under pythonw; keep one thread, give it more hash.
        try:
            self._engine.configure({"Threads": 1, "Hash": 128})
        except Exception:
            log.exception("stockfish configure failed")
        for key, value in (("Skill Level", 20), ("UCI_LimitStrength", False)):
            try:
                self._engine.configure({key: value})
            except Exception:
                pass
        log.info("stockfish ready path=%s", path)
        return self._engine

    def _reset_unlocked(self) -> None:
        if self._engine is not None:
            try:
                self._engine.quit()
            except Exception:
                pass
            self._engine = None

    def close(self) -> None:
        with self._lock:
            self._reset_unlocked()

    def choose_move(
        self,
        board: chess.Board,
        *,
        movetime_ms: int = 4000,
        multipv: int = 3,
        sacrifice_window_cp: int = 0,
        min_memory_depth: int = 12,
        use_memory: bool = True,
    ) -> EngineMove:
        """Play the strongest Stockfish move. Give material only when that line is best."""
        _ = sacrifice_window_cp
        if board.is_game_over():
            raise ValueError("Game is already over.")
        status = board.status()
        if status != chess.STATUS_VALID:
            raise ValueError(f"Illegal board for engine ({status!r}): {board.fen()}")

        if use_memory:
            remembered = position_memory.lookup(board, min_depth=min_memory_depth)
            if remembered and "aggressive sac" not in (remembered.reason or "").lower():
                try:
                    move = chess.Move.from_uci(remembered.best_uci)
                except ValueError:
                    move = None
                if move is not None and move in board.legal_moves:
                    pick = EngineMove(
                        move=move,
                        score_cp=remembered.score_cp,
                        mate=remembered.mate,
                        material_delta=0,
                        gives_check=board.gives_check(move),
                        is_capture=board.is_capture(move),
                        san=board.san(move),
                        reason=(
                            remembered.reason
                            if "opening" in remembered.reason
                            else f"perfect recall (depth {remembered.depth}, {position_memory.count()} positions known)"
                        ),
                    )
                    if remembered.mate is not None and remembered.mate > 0:
                        pick.reason = f"forced mate in {remembered.mate} (from memory)"
                    return pick

        # SimpleEngine is not thread-safe — serialize all UCI traffic.
        # MultiPV lets us refuse a sacrifice that is only equal to a keeping-pieces line.
        multipv = max(1, min(3, int(multipv)))
        think_s = max(2.4, min(float(movetime_ms) / 1000.0, 8.0))
        limit = chess.engine.Limit(time=think_s)
        with self._lock:
            infos = self._analyse_unlocked(board, limit, multipv=multipv)
        if isinstance(infos, dict):
            infos = [infos]

        before_mat = _material_balance(board, board.turn)
        our_color = board.turn
        candidates: list[EngineMove] = []
        candidate_rows: list[dict] = []
        best_depth = 0
        best_pv: list[chess.Move] = []
        for info in infos:
            pv = info.get("pv") or []
            if not pv:
                continue
            move = pv[0]
            if move not in board.legal_moves:
                continue
            score = info.get("score")
            if score is None:
                continue
            depth = int(info.get("depth") or 0)
            best_depth = max(best_depth, depth)
            pov = score.pov(board.turn)
            mate = pov.mate()
            cp = pov.score(mate_score=100000) or 0
            probe = board.copy(stack=False)
            is_capture = board.is_capture(move)
            gives_check = board.gives_check(move)
            probe.push(move)
            material_delta = _material_balance(probe, our_color) - before_mat
            pv_floor = material_delta
            look = probe.copy(stack=False)
            for ply_move in pv[1:6]:
                if ply_move not in look.legal_moves:
                    break
                look.push(ply_move)
                pv_floor = min(pv_floor, _material_balance(look, our_color) - before_mat)
            hanging = 0
            moved = probe.piece_at(move.to_square)
            if moved and moved.color == our_color and probe.is_attacked_by(not our_color, move.to_square):
                hanging = -_PIECE_CP.get(moved.piece_type, 0)
            material_delta = min(material_delta, pv_floor, material_delta + hanging)
            san = board.san(move)
            cand = EngineMove(
                move=move,
                score_cp=int(cp),
                mate=mate,
                material_delta=material_delta,
                gives_check=gives_check,
                is_capture=is_capture,
                san=san,
                reason="",
            )
            candidates.append(cand)
            candidate_rows.append(
                {
                    "uci": move.uci(),
                    "san": san,
                    "score_cp": int(cp),
                    "mate": mate,
                    "material_delta": cand.material_delta,
                    "pv": [m.uci() for m in pv[:8]],
                }
            )
            if not best_pv:
                best_pv = list(pv)

        if not candidates:
            move = next(iter(board.legal_moves))
            return EngineMove(
                move=move,
                score_cp=0,
                mate=None,
                material_delta=0,
                gives_check=board.gives_check(move),
                is_capture=board.is_capture(move),
                san=board.san(move),
                reason="fallback legal move",
            )

        mates = [c for c in candidates if c.mate is not None and c.mate > 0]
        if mates:
            mates.sort(key=lambda c: (c.mate, -c.score_cp, -c.material_delta))
            pick = mates[0]
            pick.reason = f"forced mate in {pick.mate}"
            self._store_pick(board, pick, best_depth, best_pv, candidate_rows)
            return pick

        best_cp = max(c.score_cp for c in candidates)
        # Near-equal evals: keep pieces. A sac only wins the tie-break if it is clearly better.
        equality_cp = 18
        pool = [c for c in candidates if c.score_cp >= best_cp - equality_cp]
        if not pool:
            pool = candidates

        def strength_key(c: EngineMove) -> tuple:
            # Highest eval first. If evals are essentially equal, refuse to dump material.
            return (c.score_cp, c.material_delta)

        pool.sort(key=strength_key, reverse=True)
        pick = pool[0]
        if pick.material_delta <= -80 and pick.score_cp >= best_cp - 8:
            pick.reason = (
                f"necessary sac ({pick.material_delta}cp material) eval {pick.score_cp} "
                f"(best {best_cp})"
            )
        else:
            pick.reason = f"best line eval {pick.score_cp}"
        for row in candidate_rows:
            if row["uci"] == pick.move.uci():
                best_pv = [chess.Move.from_uci(u) for u in row.get("pv") or [pick.move.uci()]]
                break
        self._store_pick(board, pick, best_depth, best_pv, candidate_rows)
        return pick

    def _analyse_unlocked(
        self,
        board: chess.Board,
        limit: chess.engine.Limit,
        *,
        multipv: int,
    ):
        """Run MultiPV analyse; restart Stockfish once if the process died."""
        engine = self._ensure_unlocked()
        try:
            return engine.analyse(board, limit, multipv=multipv)
        except (chess.engine.EngineTerminatedError, chess.engine.EngineError, OSError) as exc:
            log.warning("stockfish analyse failed (%s) — restarting engine", exc)
            self._reset_unlocked()
            engine = self._spawn_unlocked()
            return engine.analyse(board, limit, multipv=multipv)

    def _store_pick(
        self,
        board: chess.Board,
        pick: EngineMove,
        depth: int,
        pv: list[chess.Move],
        candidates: list[dict],
    ) -> None:
        try:
            position_memory.remember(
                board,
                best_uci=pick.move.uci(),
                score_cp=pick.score_cp,
                mate=pick.mate,
                depth=max(depth, 1),
                pv_uci=[m.uci() for m in pv] or [pick.move.uci()],
                candidates=candidates,
                reason=pick.reason,
            )
            if pv:
                position_memory.remember_tree(
                    board,
                    pv,
                    depth=max(depth, 1),
                    score_cp=pick.score_cp,
                )
        except Exception:
            log.exception("failed storing chess memory for %s", position_key(board))

    def prefetch(self, board: chess.Board, *, movetime_ms: int = 600) -> None:
        """Analyze this position into memory so the reply is already known."""
        try:
            if board.is_game_over():
                return
            self.choose_move(
                board,
                movetime_ms=movetime_ms,
                multipv=3,
                min_memory_depth=99,  # force fresh calc into memory
                use_memory=False,
            )
        except Exception:
            log.exception("prefetch failed")


def _material_for_side(board: chess.Board, color: chess.Color) -> int:
    total = 0
    for piece_type, value in _PIECE_CP.items():
        total += len(board.pieces(piece_type, color)) * value
    return total


def _material_balance(board: chess.Board, color: chess.Color) -> int:
    return _material_for_side(board, color) - _material_for_side(board, not color)


chess_engine = StrengthChessEngine()
AggressiveChessEngine = StrengthChessEngine
