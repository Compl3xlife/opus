from __future__ import annotations

import re
from typing import Any

import chess

from opus.hub import hub
from opus.logutil import get_logger

log = get_logger()

# Injected into the active tab to scrape Lichess / Chess.com / generic piece boards.
_SCRAPE_JS = r"""
() => {
  const files = "abcdefgh";
  const empty = Array.from({ length: 8 }, () => Array(8).fill(null));

  function sqToRC(sq) {
    if (!sq || sq.length < 2) return null;
    const f = files.indexOf(sq[0].toLowerCase());
    const r = 8 - parseInt(sq[1], 10);
    if (f < 0 || r < 0 || r > 7) return null;
    return [r, f];
  }

  function put(grid, row, col, piece) {
    if (row == null || col == null) return;
    grid[row][col] = piece;
  }

  function pieceFromLichess(el) {
    const cls = el.className || "";
    const color = cls.includes("white") ? "w" : cls.includes("black") ? "b" : "";
    let kind = "";
    if (cls.includes("king")) kind = "k";
    else if (cls.includes("queen")) kind = "q";
    else if (cls.includes("rook")) kind = "r";
    else if (cls.includes("bishop")) kind = "b";
    else if (cls.includes("knight")) kind = "n";
    else if (cls.includes("pawn")) kind = "p";
    if (!color || !kind) return null;
    return color === "w" ? kind.toUpperCase() : kind;
  }

  function pieceFromChessCom(el) {
    const cls = (el.className || "").toLowerCase();
    const m = cls.match(/\b([wb])([prnbqk])\b/);
    if (!m) return null;
    return m[1] === "w" ? m[2].toUpperCase() : m[2];
  }

  function chessComSquare(el) {
    const cls = el.className || "";
    const m = cls.match(/square-(\d)(\d)/);
    if (!m) return null;
    // chess.com: square-11 = a1, square-18 = a8, square-81 = h1
    const file = parseInt(m[1], 10) - 1;
    const rank = parseInt(m[2], 10);
    return sqToRC(files[file] + String(rank));
  }

  function isTransientPiece(el) {
    const cls = String(el.className || "");
    return /dragging|animating|moving|being-dragged/i.test(cls);
  }

  const grid = empty.map((row) => row.slice());
  let source = "";
  let orientation = "white";
  let unstable = false;

  // Lichess
  const liPieces = document.querySelectorAll("cg-board piece, .cg-board piece");
  if (liPieces.length) {
    source = "lichess";
    const board = document.querySelector("cg-board, .cg-board, .cg-wrap");
    if (board && (board.className || "").includes("orientation-black")) orientation = "black";
    const wrap = document.querySelector(".cg-wrap");
    if (wrap && (wrap.className || "").includes("orientation-black")) orientation = "black";
    liPieces.forEach((el) => {
      if (isTransientPiece(el)) {
        unstable = true;
        return;
      }
      const piece = pieceFromLichess(el);
      const sq = (el.className.match(/square-([a-h][1-8])/) || [])[1];
      const rc = sqToRC(sq);
      if (piece && rc) {
        if (grid[rc[0]][rc[1]]) unstable = true;
        put(grid, rc[0], rc[1], piece);
      }
    });
  }

  // Chess.com uses <div class="piece wp square-52"> — not <piece> tags.
  if (!source) {
    const ccPieces = document.querySelectorAll(
      "chess-board .piece, wc-chess-board .piece, .board .piece, div.piece, .piece[class*='square-']"
    );
    if (ccPieces.length) {
      source = "chesscom";
      const boardEl = document.querySelector("chess-board, wc-chess-board, .board");
      if (boardEl) {
        const flipped = boardEl.getAttribute("flipped") === "true" ||
          (boardEl.className || "").includes("flipped");
        if (flipped) orientation = "black";
      }
      ccPieces.forEach((el) => {
        if (isTransientPiece(el)) {
          unstable = true;
          return;
        }
        const piece = pieceFromChessCom(el);
        const rc = chessComSquare(el);
        if (piece && rc) {
          if (grid[rc[0]][rc[1]]) unstable = true;
          put(grid, rc[0], rc[1], piece);
        }
      });
    }
  }

  function gridToFen(g) {
    const rows = [];
    for (let r = 0; r < 8; r++) {
      let emptyCount = 0;
      let row = "";
      for (let c = 0; c < 8; c++) {
        const p = g[r][c];
        if (!p) emptyCount += 1;
        else {
          if (emptyCount) row += String(emptyCount);
          emptyCount = 0;
          row += p;
        }
      }
      if (emptyCount) row += String(emptyCount);
      rows.push(row);
    }
    return rows.join("/");
  }

  const pieceCount = grid.flat().filter(Boolean).length;
  if (!source || pieceCount < 2) {
    return { ok: false, error: "No chess board pieces found on this page." };
  }

  // Side to move heuristics
  let turn = "w";
  const turnHints = [
    document.querySelector(".positions .playing"),
    document.querySelector(".clock-turn"),
    document.querySelector(".move-turn"),
  ].filter(Boolean);
  const bodyText = (document.body && document.body.innerText || "").slice(0, 2000).toLowerCase();
  if (bodyText.includes("black to play") || bodyText.includes("black to move")) turn = "b";
  if (bodyText.includes("white to play") || bodyText.includes("white to move")) turn = "w";

  // Whose pieces are at bottom? orientation tells player color for click mapping.
  const fenBoard = gridToFen(grid);
  if (!(fenBoard.includes("K") && fenBoard.includes("k"))) unstable = true;
  const fen = `${fenBoard} ${turn} - - 0 1`;

  // Board rectangle for click mapping (viewport coords)
  let rect = null;
  const boardNode =
    document.querySelector("cg-board") ||
    document.querySelector("chess-board") ||
    document.querySelector("wc-chess-board") ||
    document.querySelector(".cg-board") ||
    document.querySelector(".board");
  if (boardNode) {
    const r = boardNode.getBoundingClientRect();
    rect = { x: r.x, y: r.y, width: r.width, height: r.height, top: r.top, left: r.left };
  }

  return {
    ok: true,
    fen,
    source,
    orientation,
    piece_count: pieceCount,
    unstable,
    board_rect: rect,
    url: location.href,
    title: document.title,
  };
}
"""


def read_board_from_browser() -> dict[str, Any]:
    result = hub.browser_command("chess_read", {}, timeout=10.0)
    if not result.get("ok"):
        return {"ok": False, "error": result.get("error") or "Browser chess read failed."}
    data = result.get("result")
    if isinstance(data, dict):
        return data
    return {"ok": False, "error": "Invalid chess_read response."}


def board_from_fen(fen: str) -> chess.Board:
    # Sites often omit castling rights; repair from piece placement.
    try:
        board = chess.Board(fen)
    except ValueError:
        parts = fen.split()
        if len(parts) >= 1:
            board = chess.Board(f"{parts[0]} {parts[1] if len(parts) > 1 else 'w'} - - 0 1")
        else:
            raise
    return _repair_castling(board)


def _repair_castling(board: chess.Board) -> chess.Board:
    """Infer castling rights from king/rook start squares when FEN omitted them."""
    rights = ""
    if board.piece_at(chess.E1) == chess.Piece.from_symbol("K"):
        if board.piece_at(chess.H1) == chess.Piece.from_symbol("R"):
            rights += "K"
        if board.piece_at(chess.A1) == chess.Piece.from_symbol("R"):
            rights += "Q"
    if board.piece_at(chess.E8) == chess.Piece.from_symbol("k"):
        if board.piece_at(chess.H8) == chess.Piece.from_symbol("r"):
            rights += "k"
        if board.piece_at(chess.A8) == chess.Piece.from_symbol("r"):
            rights += "q"
    if not rights:
        return board
    # Only upgrade empty castling field.
    if board.castling_rights:
        return board
    parts = board.fen().split()
    parts[2] = rights
    try:
        return chess.Board(" ".join(parts))
    except ValueError:
        return board


_FEN_RE = re.compile(
    r"([rnbqkpRNBQKP1-8]+\/){7}[rnbqkpRNBQKP1-8]+(?:\s+[wb](?:\s+[a-hA-HKkq-]+(?:\s+[a-h1-8-]+(?:\s+\d+\s+\d+)?)?)?)?"
)


def extract_fen_from_text(text: str) -> str | None:
    match = _FEN_RE.search(text or "")
    if not match:
        return None
    fen = match.group(0).strip()
    try:
        board_from_fen(fen)
        return fen
    except ValueError:
        return None


def square_to_viewport(square: chess.Square, board_rect: dict, orientation: str = "white") -> tuple[float, float]:
    """Map a chess square to viewport x/y using the board element's rect."""
    file_index = chess.square_file(square)
    rank_index = chess.square_rank(square)
    if (orientation or "white").lower().startswith("b"):
        file_index = 7 - file_index
        rank_index = 7 - rank_index
    left = float(board_rect.get("left", board_rect.get("x", 0)))
    top = float(board_rect.get("top", board_rect.get("y", 0)))
    width = float(board_rect.get("width") or 0)
    height = float(board_rect.get("height") or 0)
    cell_w = width / 8.0
    cell_h = height / 8.0
    # rank 7 is top of white orientation
    x = left + (file_index + 0.5) * cell_w
    y = top + ((7 - rank_index) + 0.5) * cell_h
    return x, y
