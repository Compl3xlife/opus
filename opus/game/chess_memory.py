from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import chess

from opus.logutil import get_logger
from opus.settings import appdata_dir

log = get_logger()

# Positions are keyed without halfmove/fullmove clocks so transpositions collide correctly.
def position_key(board: chess.Board) -> str:
    fen = board.fen().split()
    # board, turn, castling, ep
    return " ".join(fen[:4])


def memory_db_path() -> Path:
    folder = appdata_dir() / "chess"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / "position_memory.sqlite3"


@dataclass
class RememberedLine:
    key: str
    best_uci: str
    score_cp: int
    mate: int | None
    depth: int
    pv_uci: list[str]
    candidates_json: str
    reason: str
    hits: int
    updated_at: float


class PositionMemory:
    """
    Persistent memory of every position Opus has analyzed.

    Chess has far too many positions to precompute literally all of them.
    This stores every state Opus actually reaches (and prefetched replies),
    so the next time that exact state appears the best move is instant and known.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._path = memory_db_path()
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS positions (
                key TEXT PRIMARY KEY,
                best_uci TEXT NOT NULL,
                score_cp INTEGER NOT NULL,
                mate INTEGER,
                depth INTEGER NOT NULL,
                pv_uci TEXT NOT NULL,
                candidates_json TEXT NOT NULL,
                reason TEXT NOT NULL,
                hits INTEGER NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS openings (
                key TEXT PRIMARY KEY,
                best_uci TEXT NOT NULL,
                name TEXT NOT NULL,
                weight INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        self._conn.commit()
        self._seed_openings()

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass

    def count(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) FROM positions").fetchone()
            return int(row[0] if row else 0)

    def lookup(self, board: chess.Board, *, min_depth: int = 0) -> RememberedLine | None:
        key = position_key(board)
        with self._lock:
            row = self._conn.execute(
                "SELECT key, best_uci, score_cp, mate, depth, pv_uci, candidates_json, reason, hits, updated_at "
                "FROM positions WHERE key = ?",
                (key,),
            ).fetchone()
            if not row:
                # Opening book fallback (treated as infinite depth theory).
                open_row = self._conn.execute(
                    "SELECT best_uci, name, weight FROM openings WHERE key = ? ORDER BY weight DESC LIMIT 1",
                    (key,),
                ).fetchone()
                if open_row:
                    uci, name, _weight = open_row
                    move = chess.Move.from_uci(uci)
                    if move in board.legal_moves:
                        return RememberedLine(
                            key=key,
                            best_uci=uci,
                            score_cp=25,
                            mate=None,
                            depth=99,
                            pv_uci=[uci],
                            candidates_json="[]",
                            reason=f"opening book - {name}",
                            hits=0,
                            updated_at=time.time(),
                        )
                return None
            line = RememberedLine(
                key=row[0],
                best_uci=row[1],
                score_cp=int(row[2]),
                mate=row[3],
                depth=int(row[4]),
                pv_uci=json.loads(row[5] or "[]"),
                candidates_json=row[6] or "[]",
                reason=row[7] or "",
                hits=int(row[8] or 0),
                updated_at=float(row[9] or 0),
            )
            if line.depth < min_depth:
                return None
            move = chess.Move.from_uci(line.best_uci)
            if move not in board.legal_moves:
                return None
            self._conn.execute(
                "UPDATE positions SET hits = hits + 1 WHERE key = ?",
                (key,),
            )
            self._conn.commit()
            return line

    def remember(
        self,
        board: chess.Board,
        *,
        best_uci: str,
        score_cp: int,
        mate: int | None,
        depth: int,
        pv_uci: list[str],
        candidates: list[dict],
        reason: str,
    ) -> None:
        key = position_key(board)
        with self._lock:
            existing = self._conn.execute(
                "SELECT depth FROM positions WHERE key = ?",
                (key,),
            ).fetchone()
            if existing and int(existing[0]) > depth:
                # Keep the deeper analysis.
                return
            self._conn.execute(
                """
                INSERT INTO positions (key, best_uci, score_cp, mate, depth, pv_uci, candidates_json, reason, hits, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
                ON CONFLICT(key) DO UPDATE SET
                    best_uci=excluded.best_uci,
                    score_cp=excluded.score_cp,
                    mate=excluded.mate,
                    depth=excluded.depth,
                    pv_uci=excluded.pv_uci,
                    candidates_json=excluded.candidates_json,
                    reason=excluded.reason,
                    updated_at=excluded.updated_at
                WHERE excluded.depth >= positions.depth
                """,
                (
                    key,
                    best_uci,
                    int(score_cp),
                    mate,
                    int(depth),
                    json.dumps(pv_uci),
                    json.dumps(candidates),
                    reason,
                    time.time(),
                ),
            )
            self._conn.commit()

    def remember_tree(self, board: chess.Board, pv: list[chess.Move], *, depth: int, score_cp: int) -> None:
        """Store the principal variation as known future states."""
        probe = board.copy(stack=False)
        remaining = list(pv)
        while remaining:
            move = remaining[0]
            if move not in probe.legal_moves:
                break
            self.remember(
                probe,
                best_uci=move.uci(),
                score_cp=score_cp,
                mate=None,
                depth=max(1, depth - (len(pv) - len(remaining))),
                pv_uci=[m.uci() for m in remaining],
                candidates=[{"uci": move.uci(), "score_cp": score_cp}],
                reason="pv memory",
            )
            probe.push(move)
            remaining = remaining[1:]

    def _seed_openings(self) -> None:
        """Compact master repertoire seeds — perfect recall for common starts."""
        # (name, uci moves from start)
        lines: list[tuple[str, list[str]]] = [
            ("King's Pawn", ["e2e4"]),
            ("Queen's Pawn", ["d2d4"]),
            ("English", ["c2c4"]),
            ("Reti", ["g1f3"]),
            ("Sicilian", ["e2e4", "c7c5"]),
            ("Sicilian Open", ["e2e4", "c7c5", "g1f3"]),
            ("French", ["e2e4", "e7e6"]),
            ("Caro-Kann", ["e2e4", "c7c6"]),
            ("1...e5", ["e2e4", "e7e5"]),
            ("Italian", ["e2e4", "e7e5", "g1f3", "b8c6", "f1c4"]),
            ("Ruy Lopez", ["e2e4", "e7e5", "g1f3", "b8c6", "f1b5"]),
            ("Scotch", ["e2e4", "e7e5", "g1f3", "b8c6", "d2d4"]),
            ("Queen's Gambit", ["d2d4", "d7d5", "c2c4"]),
            ("London", ["d2d4", "d7d5", "c1f4"]),
            ("King's Indian setup", ["d2d4", "g8f6", "c2c4", "g7g6"]),
            ("Nimzo path", ["d2d4", "g8f6", "c2c4", "e7e6"]),
            ("Fried Liver path", ["e2e4", "e7e5", "g1f3", "b8c6", "f1c4", "g8f6", "f3g5"]),
            ("Evans idea", ["e2e4", "e7e5", "g1f3", "b8c6", "f1c4", "f8c5", "b2b4"]),
        ]
        with self._lock:
            for name, ucis in lines:
                board = chess.Board()
                for i, uci in enumerate(ucis):
                    move = chess.Move.from_uci(uci)
                    if move not in board.legal_moves:
                        break
                    key = position_key(board)
                    # Prefer short named roots at the start; deeper lines win later nodes.
                    weight = 200 - i if len(ucis) == 1 else (20 + i * 5)
                    self._conn.execute(
                        """
                        INSERT INTO openings (key, best_uci, name, weight)
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT(key) DO UPDATE SET
                            best_uci=excluded.best_uci,
                            name=excluded.name,
                            weight=excluded.weight
                        WHERE excluded.weight > openings.weight
                        """,
                        (key, uci, name, weight),
                    )
                    board.push(move)
            self._conn.commit()


position_memory = PositionMemory()
