from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import chess

from opus.logutil import get_logger
from opus.settings import appdata_dir

log = get_logger()


@dataclass
class BoardSnapshot:
    ply: int
    fen: str
    move_uci: str = ""
    move_san: str = ""
    by: str = ""  # opus | opponent | resync | start
    source: str = ""  # ledger | browser | inferred
    note: str = ""
    ts: float = 0.0


@dataclass
class GameRecord:
    game_id: str
    started_at: str
    our_color: str
    site: str = ""
    url: str = ""
    states: list[BoardSnapshot] = field(default_factory=list)
    ended_at: str = ""
    result: str = ""


def _placement(fen: str) -> str:
    return (fen or "").split()[0]


def _piece_chars(placement: str) -> str:
    return "".join(ch for ch in (placement or "") if ch.isalpha())


def _has_both_kings(placement: str) -> bool:
    chars = _piece_chars(placement)
    return "K" in chars and "k" in chars


def _scrape_looks_sane(observed: str, ledger: str) -> bool:
    """Reject mid-animation frames (missing kings, extra ghosts, too many pieces vanishing)."""
    if not _has_both_kings(observed):
        return False
    seen = len(_piece_chars(observed))
    known = len(_piece_chars(ledger))
    if seen > known:
        return False
    if known - seen > 1:
        return False
    return True


def _is_start_placement(placement: str) -> bool:
    return placement == chess.STARTING_BOARD_FEN.split()[0]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class BoardLedger:
    """
    Canonical chess state for Opus.

    Browser scrapes only give piece placement (and a turn guess). This ledger
    keeps the full legal game: every FEN, every move, castling, en passant,
    and clocks — so Stockfish always sees the true position.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.game_id = ""
        self.board = chess.Board()
        self.our_color: chess.Color = chess.WHITE
        self.states: list[BoardSnapshot] = []
        self.site = ""
        self.url = ""
        self._path: Path | None = None
        self._pending_our_uci = ""
        self._hold_obs = ""
        self._hold_hits = 0

    def reset(self, our_color: chess.Color, *, site: str = "", url: str = "") -> None:
        with self._lock:
            self.game_id = uuid.uuid4().hex[:12]
            self.board = chess.Board()
            self.our_color = our_color
            self.states = []
            self.site = site
            self.url = url
            self._pending_our_uci = ""
            self._hold_obs = ""
            self._hold_hits = 0
            folder = appdata_dir() / "chess" / "games"
            folder.mkdir(parents=True, exist_ok=True)
            self._path = folder / f"{self.game_id}.jsonl"
            snap = BoardSnapshot(
                ply=0,
                fen=self.board.fen(),
                by="start",
                source="ledger",
                note="new game",
                ts=time.time(),
            )
            self.states.append(snap)
            self._append_disk(snap)
            self._write_meta()

    @property
    def ply(self) -> int:
        return len(self.board.move_stack)

    def fen(self) -> str:
        return self.board.fen()

    def history_sans(self) -> list[str]:
        temp = chess.Board()
        sans: list[str] = []
        for move in self.board.move_stack:
            sans.append(temp.san(move))
            temp.push(move)
        return sans

    def pgn_moves(self) -> str:
        temp = chess.Board()
        parts: list[str] = []
        for i, move in enumerate(self.board.move_stack):
            if i % 2 == 0:
                parts.append(f"{i // 2 + 1}.")
            parts.append(temp.san(move))
            temp.push(move)
        return " ".join(parts)

    def summary(self) -> str:
        with self._lock:
            color = "white" if self.our_color == chess.WHITE else "black"
            return (
                f"game {self.game_id} | we={color} | ply={self.ply} | "
                f"turn={'white' if self.board.turn == chess.WHITE else 'black'} | "
                f"fen={self.board.fen()}"
            )

    def pending_uci(self) -> str:
        with self._lock:
            return self._pending_our_uci

    def mark_our_move_pending(self, move: chess.Move) -> None:
        with self._lock:
            self._pending_our_uci = move.uci()

    def apply_our_move(self, move: chess.Move, *, note: str = "") -> BoardSnapshot | None:
        with self._lock:
            if move not in self.board.legal_moves:
                return None
            san = self.board.san(move)
            self.board.push(move)
            self._pending_our_uci = ""
            snap = BoardSnapshot(
                ply=self.ply,
                fen=self.board.fen(),
                move_uci=move.uci(),
                move_san=san,
                by="opus",
                source="ledger",
                note=note or "our move",
                ts=time.time(),
            )
            self.states.append(snap)
            self._append_disk(snap)
            return snap

    def sync_from_observation(
        self,
        observed_fen: str,
        *,
        orientation: str = "white",
        site: str = "",
        url: str = "",
        turn_hint: str = "",
        unstable: bool = False,
    ) -> tuple[chess.Board, str]:
        """
        Align ledger with a scraped board.

        Returns (board_to_analyze, event) where event is one of:
        unchanged | our_turn | opponent_moved | resync | wait
        """
        with self._lock:
            if site:
                self.site = site
            if url:
                self.url = url

            obs_placement = _placement(observed_fen)
            led_placement = _placement(self.board.fen())

            if obs_placement == led_placement:
                # Placement matches — trust ledger turn/castling/ep entirely.
                if self.board.turn == self.our_color:
                    return self.board.copy(stack=True), "our_turn"
                return self.board.copy(stack=True), "wait"

            # Did our pending move land?
            if self._pending_our_uci:
                try:
                    pending = chess.Move.from_uci(self._pending_our_uci)
                except ValueError:
                    pending = None
                if pending and pending in self.board.legal_moves:
                    probe = self.board.copy(stack=False)
                    probe.push(pending)
                    if _placement(probe.fen()) == obs_placement:
                        san = self.board.san(pending)
                        self.board.push(pending)
                        self._pending_our_uci = ""
                        snap = BoardSnapshot(
                            ply=self.ply,
                            fen=self.board.fen(),
                            move_uci=pending.uci(),
                            move_san=san,
                            by="opus",
                            source="inferred",
                            note="confirmed on board",
                            ts=time.time(),
                        )
                        self.states.append(snap)
                        self._append_disk(snap)
                        return self.board.copy(stack=True), "wait"

            # Find legal move(s) that produce the scraped placement.
            matches = self._moves_to_placement(obs_placement)
            chosen: list[chess.Move] | None = None
            if len(matches) == 1:
                chosen = matches
            elif len(matches) > 1:
                hint = (turn_hint or "").strip().lower()
                narrowed = []
                for move in matches:
                    probe = self.board.copy(stack=False)
                    probe.push(move)
                    if hint.startswith("w") and probe.turn == chess.WHITE:
                        narrowed.append(move)
                    elif hint.startswith("b") and probe.turn == chess.BLACK:
                        narrowed.append(move)
                if len(narrowed) == 1:
                    chosen = narrowed
                elif self._pending_our_uci:
                    pend = [m for m in matches if m.uci() == self._pending_our_uci]
                    if len(pend) == 1:
                        chosen = pend

            if chosen is None:
                # Only search a short gap (scrape lag). Deep BFS is combinatorial
                # and will freeze the chess loop when joining a mid-game board.
                chain = self._chain_to_placement(obs_placement, max_plies=2)
                if chain:
                    chosen = chain

            if chosen:
                last_by = ""
                self._hold_obs = ""
                self._hold_hits = 0
                for move in chosen:
                    san = self.board.san(move)
                    by = "opus" if self.board.turn == self.our_color else "opponent"
                    last_by = by
                    self.board.push(move)
                    if by == "opus":
                        self._pending_our_uci = ""
                    snap = BoardSnapshot(
                        ply=self.ply,
                        fen=self.board.fen(),
                        move_uci=move.uci(),
                        move_san=san,
                        by=by,
                        source="inferred",
                        note="matched legal move to scrape",
                        ts=time.time(),
                    )
                    self.states.append(snap)
                    self._append_disk(snap)
                if self.board.turn == self.our_color:
                    return self.board.copy(stack=True), "our_turn"
                return self.board.copy(stack=True), "opponent_moved" if last_by == "opponent" else "wait"

            # Chess.com animations produce partial boards. Adopting those is how
            # Opus "blunders" — Stockfish plays a fake position and we get mated.
            if unstable:
                log.info("chess ignoring animated scrape obs=%s", obs_placement)
                return self.board.copy(stack=True), "wait"

            joining = self.ply == 0 and _is_start_placement(led_placement)
            sane = _scrape_looks_sane(obs_placement, led_placement)
            if obs_placement == self._hold_obs:
                self._hold_hits += 1
            else:
                self._hold_obs = obs_placement
                self._hold_hits = 1

            if joining and _has_both_kings(obs_placement) and self._hold_hits >= 3:
                log.info("chess ledger joining mid-game board=%s", obs_placement)
                self._hold_obs = ""
                self._hold_hits = 0
                return self._commit_adopt(observed_fen, turn_hint, orientation)

            if not sane or self._hold_hits < 4:
                log.info(
                    "chess ignoring unstable scrape hits=%s sane=%s obs=%s ledger=%s",
                    self._hold_hits,
                    sane,
                    obs_placement,
                    led_placement,
                )
                return self.board.copy(stack=True), "wait"

            log.warning(
                "chess ledger resync: ledger=%s observed=%s matches=%s hits=%s",
                led_placement,
                obs_placement,
                len(matches),
                self._hold_hits,
            )
            self._hold_obs = ""
            self._hold_hits = 0
            return self._commit_adopt(observed_fen, turn_hint, orientation)

    def _commit_adopt(self, observed_fen: str, turn_hint: str, orientation: str) -> tuple[chess.Board, str]:
        adopted = self._adopt_fen(observed_fen, turn_hint, orientation)
        snap = BoardSnapshot(
            ply=self.ply,
            fen=adopted.fen(),
            by="resync",
            source="browser",
            note="stable unmatched scrape; adopted board",
            ts=time.time(),
        )
        self.states.append(snap)
        self._append_disk(snap)
        self._write_meta()
        if adopted.turn == self.our_color:
            return adopted, "resync"
        return adopted, "wait"

    def _adopt_fen(self, fen: str, turn_hint: str, orientation: str) -> chess.Board:
        from opus.game.chess_board import board_from_fen

        board = board_from_fen(fen)
        # Prefer explicit turn hint, else orientation bottom clock already baked into fen.
        hint = (turn_hint or "").strip().lower()
        if hint.startswith("w"):
            board.turn = chess.WHITE
        elif hint.startswith("b"):
            board.turn = chess.BLACK
        self.board = board
        # Drop move stack — we only know placement from here.
        self.board = chess.Board(board.fen())
        return self.board.copy(stack=True)

    def _moves_to_placement(self, placement: str) -> list[chess.Move]:
        found: list[chess.Move] = []
        for move in self.board.legal_moves:
            probe = self.board.copy(stack=False)
            probe.push(move)
            if _placement(probe.fen()) == placement:
                found.append(move)
        return found

    def _chain_to_placement(self, placement: str, max_plies: int = 2) -> list[chess.Move] | None:
        """BFS for a short legal move chain reaching the observed placement."""
        from collections import deque

        max_plies = max(1, min(int(max_plies), 2))
        start_fen = self.board.fen()
        queue: deque[tuple[str, list[chess.Move]]] = deque([(start_fen, [])])
        seen = {_placement(start_fen)}
        nodes = 0
        limit = 8000  # hard cap so mid-game joins never freeze Opus
        while queue:
            fen, path = queue.popleft()
            if len(path) >= max_plies:
                continue
            node = chess.Board(fen)
            for move in node.legal_moves:
                nodes += 1
                if nodes > limit:
                    return None
                probe = node.copy(stack=False)
                probe.push(move)
                place = _placement(probe.fen())
                new_path = path + [move]
                if place == placement:
                    return new_path
                if place not in seen and len(new_path) < max_plies:
                    seen.add(place)
                    queue.append((probe.fen(), new_path))
        return None

    def finish(self, result: str = "") -> Path | None:
        with self._lock:
            try:
                self._write_meta(ended=True, result=result)
            except Exception:
                log.exception("chess meta write failed during finish")
            return self._path

    def _append_disk(self, snap: BoardSnapshot) -> None:
        if not self._path:
            return
        try:
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(asdict(snap), ensure_ascii=False) + "\n")
        except OSError:
            log.exception("failed writing chess state")

    def _write_meta(self, *, ended: bool = False, result: str = "") -> None:
        if not self._path:
            return
        meta_path = self._path.with_suffix(".meta.json")
        try:
            pgn = self.pgn_moves()
        except Exception:
            pgn = ""
        payload = {
            "game_id": self.game_id,
            "started_at": _now_iso() if not meta_path.exists() else None,
            "our_color": "white" if self.our_color == chess.WHITE else "black",
            "site": self.site,
            "url": self.url,
            "ply": self.ply,
            "fen": self.board.fen(),
            "pgn": pgn,
            "states": len(self.states),
        }
        if meta_path.exists():
            try:
                old = json.loads(meta_path.read_text(encoding="utf-8"))
                payload["started_at"] = old.get("started_at") or _now_iso()
            except (OSError, json.JSONDecodeError):
                payload["started_at"] = _now_iso()
        else:
            payload["started_at"] = _now_iso()
        if ended:
            payload["ended_at"] = _now_iso()
            payload["result"] = result
        try:
            meta_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except OSError:
            log.exception("failed writing chess meta")
