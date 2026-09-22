from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from threading import RLock

from opus.settings import appdata_dir

_db_lock = RLock()
CHIP_START = 100
OPUS_BANK_ID = "opus"
OPUS_BANK_START = 100_000_000
OPUS_DAILY_ALLOWANCE = 100_000_000
OPUS_DAILY_WAIT = 24 * 60 * 60
MAX_BET = 100_000_000


def _parse_ts(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        dt = datetime.fromisoformat(raw)
        return dt.strftime("%b %d, %Y %I:%M %p")
    except ValueError:
        return value


def _collect_image_urls(message) -> list[str]:
    urls: list[str] = []
    for att in getattr(message, "attachments", []) or []:
        content_type = getattr(att, "content_type", None) or ""
        if content_type.startswith("image") or str(getattr(att, "filename", "")).lower().endswith(
            (".png", ".jpg", ".jpeg", ".gif", ".webp")
        ):
            url = getattr(att, "url", "")
            if url:
                urls.append(url)
    for embed in getattr(message, "embeds", []) or []:
        image = getattr(embed, "image", None)
        if image and getattr(image, "url", None):
            urls.append(image.url)
    return urls


class DiscordStore:
    def __init__(self) -> None:
        self.path = appdata_dir() / "discord_messages.db"
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self) -> None:
        with _db_lock, self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    message_id TEXT PRIMARY KEY,
                    guild_id TEXT NOT NULL,
                    channel_id TEXT NOT NULL,
                    channel_name TEXT,
                    author_id TEXT NOT NULL,
                    author_name TEXT,
                    content TEXT,
                    created_at TEXT NOT NULL,
                    image_urls TEXT DEFAULT '[]',
                    has_image INTEGER DEFAULT 0,
                    jump_url TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_messages_guild ON messages(guild_id);
                CREATE INDEX IF NOT EXISTS idx_messages_author_id ON messages(author_id);
                CREATE INDEX IF NOT EXISTS idx_messages_author_name ON messages(author_name);
                CREATE INDEX IF NOT EXISTS idx_messages_created ON messages(created_at);
                CREATE INDEX IF NOT EXISTS idx_messages_has_image ON messages(has_image);
                CREATE TABLE IF NOT EXISTS reaction_roles (
                    guild_id TEXT NOT NULL,
                    channel_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    emoji_key TEXT NOT NULL,
                    emoji TEXT NOT NULL,
                    role_id TEXT NOT NULL,
                    PRIMARY KEY (guild_id, message_id, emoji_key)
                );
                CREATE INDEX IF NOT EXISTS idx_reaction_roles_message
                    ON reaction_roles(guild_id, message_id);
                CREATE TABLE IF NOT EXISTS chip_wallets (
                    guild_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    user_name TEXT DEFAULT '',
                    balance INTEGER NOT NULL DEFAULT 100,
                    games_won INTEGER NOT NULL DEFAULT 0,
                    games_played INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (guild_id, user_id)
                );
                CREATE TABLE IF NOT EXISTS chip_polls (
                    guild_id TEXT NOT NULL,
                    poll_message_id TEXT NOT NULL,
                    channel_id TEXT NOT NULL,
                    question TEXT,
                    options TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    settled INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (guild_id, poll_message_id)
                );
                CREATE INDEX IF NOT EXISTS idx_chip_polls_due
                    ON chip_polls(settled, expires_at);
                CREATE TABLE IF NOT EXISTS chip_poll_bets (
                    guild_id TEXT NOT NULL,
                    poll_message_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    user_name TEXT DEFAULT '',
                    option_idx INTEGER NOT NULL,
                    amount INTEGER NOT NULL,
                    PRIMARY KEY (guild_id, poll_message_id, user_id)
                );
                CREATE TABLE IF NOT EXISTS chip_challenges (
                    challenge_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id TEXT NOT NULL,
                    channel_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    challenger_id TEXT NOT NULL,
                    opponent_id TEXT NOT NULL,
                    amount INTEGER NOT NULL,
                    payload TEXT DEFAULT '{}',
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS chip_cooldowns (
                    guild_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    until_at REAL NOT NULL,
                    PRIMARY KEY (guild_id, user_id, kind)
                );
                CREATE TABLE IF NOT EXISTS chip_inventory (
                    guild_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    item_id TEXT NOT NULL,
                    qty INTEGER NOT NULL DEFAULT 0,
                    expires_at REAL NOT NULL DEFAULT 0,
                    PRIMARY KEY (guild_id, user_id, item_id)
                );
                CREATE TABLE IF NOT EXISTS chip_holds (
                    hold_id TEXT PRIMARY KEY,
                    guild_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    user_name TEXT DEFAULT '',
                    amount INTEGER NOT NULL DEFAULT 0,
                    items TEXT NOT NULL DEFAULT '[]',
                    created_at REAL NOT NULL
                );
                """
            )

    def save_reaction_role(
        self,
        *,
        guild_id: str,
        channel_id: str,
        message_id: str,
        emoji_key: str,
        emoji: str,
        role_id: str,
    ) -> None:
        with _db_lock, self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO reaction_roles
                    (guild_id, channel_id, message_id, emoji_key, emoji, role_id)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (str(guild_id), str(channel_id), str(message_id), str(emoji_key), str(emoji), str(role_id)),
            )

    def delete_reaction_message(self, guild_id: str, message_id: str) -> int:
        with _db_lock, self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM reaction_roles WHERE guild_id = ? AND message_id = ?",
                (str(guild_id), str(message_id)),
            )
            return int(cur.rowcount or 0)

    def lookup_reaction_role(self, guild_id: str, message_id: str, emoji_key: str) -> str:
        with _db_lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT role_id FROM reaction_roles
                WHERE guild_id = ? AND message_id = ? AND emoji_key = ?
                """,
                (str(guild_id), str(message_id), str(emoji_key)),
            ).fetchone()
            return str(row["role_id"]) if row else ""

    def list_reaction_roles(self, guild_id: str) -> list[dict]:
        with _db_lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT channel_id, message_id, emoji, role_id
                FROM reaction_roles
                WHERE guild_id = ?
                ORDER BY message_id, emoji
                """,
                (str(guild_id),),
            ).fetchall()
            return [dict(row) for row in rows]

    def _wallet_row(self, conn: sqlite3.Connection, guild_id: str, user_id: str, user_name: str = "") -> sqlite3.Row:
        uid = str(user_id)
        start = OPUS_BANK_START if uid == OPUS_BANK_ID else CHIP_START
        label = "Opus" if uid == OPUS_BANK_ID else str(user_name or "")
        conn.execute(
            """
            INSERT OR IGNORE INTO chip_wallets (guild_id, user_id, user_name, balance)
            VALUES (?, ?, ?, ?)
            """,
            (str(guild_id), uid, label, start),
        )
        if user_name:
            conn.execute(
                "UPDATE chip_wallets SET user_name = ? WHERE guild_id = ? AND user_id = ? AND (user_name IS NULL OR user_name = '')",
                (str(user_name), str(guild_id), str(user_id)),
            )
            conn.execute(
                "UPDATE chip_wallets SET user_name = ? WHERE guild_id = ? AND user_id = ?",
                (str(user_name), str(guild_id), str(user_id)),
            )
        row = conn.execute(
            "SELECT * FROM chip_wallets WHERE guild_id = ? AND user_id = ?",
            (str(guild_id), str(user_id)),
        ).fetchone()
        return row

    def wallet(self, guild_id: str, user_id: str, user_name: str = "") -> dict:
        with _db_lock, self._connect() as conn:
            return dict(self._wallet_row(conn, guild_id, user_id, user_name))

    def try_spend(self, guild_id: str, user_id: str, amount: int, user_name: str = "") -> tuple[bool, int]:
        amount = int(amount)
        if amount <= 0:
            return False, 0
        with _db_lock, self._connect() as conn:
            row = self._wallet_row(conn, guild_id, user_id, user_name)
            balance = int(row["balance"] or 0)
            if balance < amount:
                return False, balance
            conn.execute(
                "UPDATE chip_wallets SET balance = balance - ? WHERE guild_id = ? AND user_id = ?",
                (amount, str(guild_id), str(user_id)),
            )
            return True, balance - amount

    def grant_opus_allowance(self, guild_id: str) -> int:
        if self.cooldown_left(guild_id, OPUS_BANK_ID, "allowance") > 0:
            return 0
        self.set_cooldown(guild_id, OPUS_BANK_ID, "allowance", OPUS_DAILY_WAIT)
        self.add_chips(guild_id, OPUS_BANK_ID, OPUS_DAILY_ALLOWANCE, user_name="Opus")
        return OPUS_DAILY_ALLOWANCE

    def pay_from_bank(self, guild_id: str, amount: int) -> bool:
        self.grant_opus_allowance(guild_id)
        amount = int(amount)
        if amount <= 0:
            return True
        ok, balance = self.try_spend(guild_id, OPUS_BANK_ID, amount, user_name="Opus")
        if ok:
            return True
        short = max(0, amount - int(balance or 0))
        if short:
            self.add_chips(guild_id, OPUS_BANK_ID, short, user_name="Opus")
        ok, _left = self.try_spend(guild_id, OPUS_BANK_ID, amount, user_name="Opus")
        return ok

    def add_chips(
        self,
        guild_id: str,
        user_id: str,
        amount: int,
        *,
        user_name: str = "",
        played: bool = False,
        won: bool = False,
    ) -> int:
        with _db_lock, self._connect() as conn:
            self._wallet_row(conn, guild_id, user_id, user_name)
            conn.execute(
                """
                UPDATE chip_wallets
                SET balance = balance + ?,
                    games_played = games_played + ?,
                    games_won = games_won + ?
                WHERE guild_id = ? AND user_id = ?
                """,
                (int(amount), 1 if played else 0, 1 if won else 0, str(guild_id), str(user_id)),
            )
            row = conn.execute(
                "SELECT balance FROM chip_wallets WHERE guild_id = ? AND user_id = ?",
                (str(guild_id), str(user_id)),
            ).fetchone()
            return int(row["balance"] if row else 0)

    def record_game(self, guild_id: str, user_id: str, *, won: bool, user_name: str = "") -> None:
        self.add_chips(guild_id, user_id, 0, user_name=user_name, played=True, won=won)

    def cooldown_left(self, guild_id: str, user_id: str, kind: str) -> float:
        import time

        with _db_lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT until_at FROM chip_cooldowns
                WHERE guild_id = ? AND user_id = ? AND kind = ?
                """,
                (str(guild_id), str(user_id), str(kind)),
            ).fetchone()
        if not row:
            return 0.0
        return max(0.0, float(row["until_at"]) - time.time())

    def set_cooldown(self, guild_id: str, user_id: str, kind: str, seconds: float) -> None:
        import time

        with _db_lock, self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO chip_cooldowns (guild_id, user_id, kind, until_at)
                VALUES (?, ?, ?, ?)
                """,
                (str(guild_id), str(user_id), str(kind), time.time() + max(0.0, float(seconds))),
            )

    def player_wallets(self, guild_id: str) -> list[dict]:
        with _db_lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT user_id, user_name, balance, games_won, games_played
                FROM chip_wallets
                WHERE guild_id = ? AND user_id != ?
                """,
                (str(guild_id), OPUS_BANK_ID),
            ).fetchall()
            return [dict(row) for row in rows]

    def leaderboard(self, guild_id: str, *, kind: str = "balance", limit: int = 10) -> list[dict]:
        rows = [self._with_chip_stats(row) for row in self.player_wallets(guild_id)]
        if kind == "wins":
            rows.sort(key=lambda row: (int(row["games_won"]), int(row["balance"])), reverse=True)
        elif kind == "losses":
            rows.sort(key=lambda row: (int(row["losses"]), int(row["games_played"])), reverse=True)
        elif kind == "ratio":
            rows = [row for row in rows if int(row["games_played"]) >= 3]
            rows.sort(
                key=lambda row: (float(row["ratio"]), int(row["games_won"]), int(row["balance"])),
                reverse=True,
            )
        else:
            rows.sort(key=lambda row: (int(row["balance"]), int(row["games_won"])), reverse=True)
        return rows[: max(1, int(limit))]

    def chip_row(self, guild_id: str, user_id: str) -> dict | None:
        with _db_lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM chip_wallets WHERE guild_id = ? AND user_id = ?",
                (str(guild_id), str(user_id)),
            ).fetchone()
            return dict(row) if row else None

    def chip_profile(self, guild_id: str, user_id: str, user_name: str = "") -> dict:
        raw = self.chip_row(guild_id, user_id) or {
            "user_id": str(user_id),
            "user_name": str(user_name or ""),
            "balance": 0,
            "games_won": 0,
            "games_played": 0,
        }
        mine = self._with_chip_stats(raw)
        players = [self._with_chip_stats(row) for row in self.player_wallets(guild_id)]
        if not any(str(row["user_id"]) == str(user_id) for row in players):
            players.append(mine)
        total = len(players)

        def rank_of(key: str, *, reverse: bool = True, played_min: int = 0) -> tuple[int | None, int]:
            pool = [row for row in players if int(row["games_played"]) >= played_min]
            ordered = sorted(
                pool,
                key=lambda row: (row[key], int(row["games_won"]), int(row["balance"])),
                reverse=reverse,
            )
            for idx, row in enumerate(ordered, start=1):
                if str(row["user_id"]) == str(user_id):
                    return idx, len(pool)
            return None, len(pool)

        chips_rank, chips_of = rank_of("balance")
        wins_rank, wins_of = rank_of("games_won")
        losses_rank, losses_of = rank_of("losses")
        ratio_rank, ratio_of = rank_of("ratio", played_min=3)
        mine.update(
            {
                "chips_rank": chips_rank,
                "chips_of": chips_of or total,
                "wins_rank": wins_rank,
                "wins_of": wins_of or total,
                "losses_rank": losses_rank,
                "losses_of": losses_of or total,
                "ratio_rank": ratio_rank,
                "ratio_of": ratio_of,
            }
        )
        return mine

    @staticmethod
    def _with_chip_stats(row: dict) -> dict:
        item = dict(row)
        won = int(item.get("games_won") or 0)
        played = int(item.get("games_played") or 0)
        losses = max(0, played - won)
        item["wins"] = won
        item["losses"] = losses
        item["played"] = played
        item["ratio"] = (won / losses) if losses else (float("inf") if won else 0.0)
        item["win_rate"] = (won / played) if played else 0.0
        return item

    def save_chip_poll(
        self,
        *,
        guild_id: str,
        poll_message_id: str,
        channel_id: str,
        question: str,
        options: list[str],
        expires_at: float,
    ) -> None:
        with _db_lock, self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO chip_polls
                    (guild_id, poll_message_id, channel_id, question, options, expires_at, settled)
                VALUES (?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    str(guild_id),
                    str(poll_message_id),
                    str(channel_id),
                    str(question or ""),
                    json.dumps(list(options)),
                    float(expires_at),
                ),
            )

    def chip_poll(self, guild_id: str, poll_message_id: str) -> dict | None:
        with _db_lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM chip_polls WHERE guild_id = ? AND poll_message_id = ?",
                (str(guild_id), str(poll_message_id)),
            ).fetchone()
            if not row:
                return None
            item = dict(row)
            item["options"] = json.loads(item.get("options") or "[]")
            return item

    def latest_chip_poll(self, guild_id: str, channel_id: str) -> dict | None:
        with _db_lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM chip_polls
                WHERE guild_id = ? AND channel_id = ? AND settled = 0
                ORDER BY expires_at DESC
                LIMIT 1
                """,
                (str(guild_id), str(channel_id)),
            ).fetchone()
            if not row:
                return None
            item = dict(row)
            item["options"] = json.loads(item.get("options") or "[]")
            return item

    def due_chip_polls(self, now: float) -> list[dict]:
        with _db_lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM chip_polls
                WHERE settled = 0 AND expires_at <= ?
                ORDER BY expires_at ASC
                LIMIT 20
                """,
                (float(now),),
            ).fetchall()
            out = []
            for row in rows:
                item = dict(row)
                item["options"] = json.loads(item.get("options") or "[]")
                out.append(item)
            return out

    def mark_poll_settled(self, guild_id: str, poll_message_id: str) -> None:
        with _db_lock, self._connect() as conn:
            conn.execute(
                "UPDATE chip_polls SET settled = 1 WHERE guild_id = ? AND poll_message_id = ?",
                (str(guild_id), str(poll_message_id)),
            )

    def place_poll_bet(
        self,
        *,
        guild_id: str,
        poll_message_id: str,
        user_id: str,
        user_name: str,
        option_idx: int,
        amount: int,
    ) -> tuple[str | None, int]:
        amount = int(amount)
        if amount <= 0:
            return "Bet at least 1 chip.", 0
        with _db_lock, self._connect() as conn:
            poll = conn.execute(
                "SELECT settled FROM chip_polls WHERE guild_id = ? AND poll_message_id = ?",
                (str(guild_id), str(poll_message_id)),
            ).fetchone()
            if not poll:
                return "There's no open poll to bet on.", 0
            if int(poll["settled"] or 0):
                return "That poll already paid out.", 0
            existing = conn.execute(
                """
                SELECT amount FROM chip_poll_bets
                WHERE guild_id = ? AND poll_message_id = ? AND user_id = ?
                """,
                (str(guild_id), str(poll_message_id), str(user_id)),
            ).fetchone()
            extra = amount
            if existing:
                extra = amount - int(existing["amount"] or 0)
            wallet = self._wallet_row(conn, guild_id, user_id, user_name)
            balance = int(wallet["balance"] or 0)
            if extra > 0 and balance < extra:
                return f"You only have {balance} chips.", balance
            if extra:
                conn.execute(
                    "UPDATE chip_wallets SET balance = balance - ? WHERE guild_id = ? AND user_id = ?",
                    (extra, str(guild_id), str(user_id)),
                )
            elif extra < 0:
                conn.execute(
                    "UPDATE chip_wallets SET balance = balance + ? WHERE guild_id = ? AND user_id = ?",
                    (-extra, str(guild_id), str(user_id)),
                )
            conn.execute(
                """
                INSERT OR REPLACE INTO chip_poll_bets
                    (guild_id, poll_message_id, user_id, user_name, option_idx, amount)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (str(guild_id), str(poll_message_id), str(user_id), str(user_name or ""), int(option_idx), amount),
            )
            row = conn.execute(
                "SELECT balance FROM chip_wallets WHERE guild_id = ? AND user_id = ?",
                (str(guild_id), str(user_id)),
            ).fetchone()
            return None, int(row["balance"] if row else 0)

    def poll_bets(self, guild_id: str, poll_message_id: str) -> list[dict]:
        with _db_lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT user_id, user_name, option_idx, amount
                FROM chip_poll_bets
                WHERE guild_id = ? AND poll_message_id = ?
                """,
                (str(guild_id), str(poll_message_id)),
            ).fetchall()
            return [dict(row) for row in rows]

    def refund_poll_bets(self, guild_id: str, poll_message_id: str) -> list[dict]:
        bets = self.poll_bets(guild_id, poll_message_id)
        with _db_lock, self._connect() as conn:
            for bet in bets:
                self._wallet_row(conn, guild_id, bet["user_id"], bet.get("user_name") or "")
                conn.execute(
                    "UPDATE chip_wallets SET balance = balance + ? WHERE guild_id = ? AND user_id = ?",
                    (int(bet["amount"]), str(guild_id), str(bet["user_id"])),
                )
            conn.execute(
                "DELETE FROM chip_poll_bets WHERE guild_id = ? AND poll_message_id = ?",
                (str(guild_id), str(poll_message_id)),
            )
        return bets

    def create_challenge(
        self,
        *,
        guild_id: str,
        channel_id: str,
        kind: str,
        challenger_id: str,
        opponent_id: str,
        amount: int,
        payload: dict | None = None,
    ) -> int:
        import time

        with _db_lock, self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO chip_challenges
                    (guild_id, channel_id, kind, status, challenger_id, opponent_id, amount, payload, created_at)
                VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?)
                """,
                (
                    str(guild_id),
                    str(channel_id),
                    str(kind),
                    str(challenger_id),
                    str(opponent_id),
                    int(amount),
                    json.dumps(payload or {}),
                    time.time(),
                ),
            )
            return int(cur.lastrowid)

    def challenge(self, challenge_id: int) -> dict | None:
        with _db_lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM chip_challenges WHERE challenge_id = ?",
                (int(challenge_id),),
            ).fetchone()
            if not row:
                return None
            item = dict(row)
            item["payload"] = json.loads(item.get("payload") or "{}")
            return item

    def set_challenge(self, challenge_id: int, *, status: str | None = None, payload: dict | None = None) -> None:
        with _db_lock, self._connect() as conn:
            if status is not None:
                conn.execute(
                    "UPDATE chip_challenges SET status = ? WHERE challenge_id = ?",
                    (str(status), int(challenge_id)),
                )
            if payload is not None:
                conn.execute(
                    "UPDATE chip_challenges SET payload = ? WHERE challenge_id = ?",
                    (json.dumps(payload), int(challenge_id)),
                )

    def update_challenge_status(self, challenge_id: int, *, from_status: str, to_status: str) -> dict | None:
        with _db_lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM chip_challenges WHERE challenge_id = ? AND status = ?",
                (int(challenge_id), str(from_status)),
            ).fetchone()
            if not row:
                return None
            conn.execute(
                "UPDATE chip_challenges SET status = ? WHERE challenge_id = ?",
                (str(to_status), int(challenge_id)),
            )
            item = dict(row)
            item["payload"] = json.loads(item.get("payload") or "{}")
            item["status"] = to_status
            return item

    def open_challenge_count(self, guild_id: str, user_id: str) -> int:
        import time

        cutoff = time.time() - 12 * 60
        with _db_lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS n FROM chip_challenges
                WHERE guild_id = ? AND status IN ('pending', 'picking', 'active')
                  AND created_at >= ?
                  AND (challenger_id = ? OR opponent_id = ?)
                """,
                (str(guild_id), cutoff, str(user_id), str(user_id)),
            ).fetchone()
            return int(row["n"] if row else 0)

    def _expire_challenge_row(self, challenge_id: int, from_status: str, *, refund: str) -> dict | None:
        item = self.update_challenge_status(challenge_id, from_status=from_status, to_status="expired")
        if not item:
            return None
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        amount = int(item.get("amount") or 0)
        if amount <= 0:
            return item
        if refund == "hold" and payload.get("hold", True):
            self.add_chips(item["guild_id"], item["challenger_id"], amount)
        elif refund == "both":
            self.add_chips(item["guild_id"], item["challenger_id"], amount)
            self.add_chips(item["guild_id"], item["opponent_id"], amount)
        return item

    def expire_challenges(self, older_than: float) -> list[dict]:
        import time

        now = time.time()
        expired = []
        with _db_lock, self._connect() as conn:
            pending = conn.execute(
                """
                SELECT challenge_id FROM chip_challenges
                WHERE status = 'pending' AND created_at <= ?
                """,
                (float(older_than),),
            ).fetchall()
            picking = conn.execute(
                """
                SELECT challenge_id FROM chip_challenges
                WHERE status = 'picking' AND created_at <= ?
                """,
                (now - 120,),
            ).fetchall()
            active = conn.execute(
                """
                SELECT challenge_id FROM chip_challenges
                WHERE status = 'active' AND created_at <= ?
                """,
                (now - 12 * 60,),
            ).fetchall()
        for row in pending:
            item = self._expire_challenge_row(int(row["challenge_id"]), "pending", refund="hold")
            if item:
                expired.append(item)
        for row in picking:
            item = self._expire_challenge_row(int(row["challenge_id"]), "picking", refund="both")
            if item:
                expired.append(item)
        for row in active:
            item = self._expire_challenge_row(int(row["challenge_id"]), "active", refund="none")
            if item:
                expired.append(item)
        return expired

    def inventory(self, guild_id: str, user_id: str) -> list[dict]:
        with _db_lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT item_id, qty, expires_at FROM chip_inventory
                WHERE guild_id = ? AND user_id = ? AND qty > 0
                ORDER BY item_id
                """,
                (str(guild_id), str(user_id)),
            ).fetchall()
            return [dict(row) for row in rows]

    def item_qty(self, guild_id: str, user_id: str, item_id: str, *, now: float | None = None) -> int:
        import time

        stamp = time.time() if now is None else float(now)
        with _db_lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT qty, expires_at FROM chip_inventory
                WHERE guild_id = ? AND user_id = ? AND item_id = ?
                """,
                (str(guild_id), str(user_id), str(item_id)),
            ).fetchone()
            if not row or int(row["qty"] or 0) <= 0:
                return 0
            expires = float(row["expires_at"] or 0)
            if expires and expires < stamp:
                return 0
            return int(row["qty"] or 0)

    def add_item(
        self,
        guild_id: str,
        user_id: str,
        item_id: str,
        *,
        qty: int = 1,
        expires_at: float = 0.0,
    ) -> int:
        with _db_lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO chip_inventory (guild_id, user_id, item_id, qty, expires_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(guild_id, user_id, item_id) DO UPDATE SET
                    qty = chip_inventory.qty + excluded.qty,
                    expires_at = CASE
                        WHEN excluded.expires_at > chip_inventory.expires_at THEN excluded.expires_at
                        ELSE chip_inventory.expires_at
                    END
                """,
                (str(guild_id), str(user_id), str(item_id), max(1, int(qty)), float(expires_at or 0)),
            )
            row = conn.execute(
                "SELECT qty FROM chip_inventory WHERE guild_id = ? AND user_id = ? AND item_id = ?",
                (str(guild_id), str(user_id), str(item_id)),
            ).fetchone()
            return int(row["qty"] if row else 0)

    def consume_item(self, guild_id: str, user_id: str, item_id: str, *, qty: int = 1) -> bool:
        import time

        need = max(1, int(qty))
        stamp = time.time()
        with _db_lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT qty, expires_at FROM chip_inventory
                WHERE guild_id = ? AND user_id = ? AND item_id = ?
                """,
                (str(guild_id), str(user_id), str(item_id)),
            ).fetchone()
            if not row:
                return False
            expires = float(row["expires_at"] or 0)
            have = int(row["qty"] or 0)
            if have < need:
                return False
            if expires and expires < stamp:
                conn.execute(
                    "DELETE FROM chip_inventory WHERE guild_id = ? AND user_id = ? AND item_id = ?",
                    (str(guild_id), str(user_id), str(item_id)),
                )
                return False
            left = have - need
            if left <= 0:
                conn.execute(
                    "DELETE FROM chip_inventory WHERE guild_id = ? AND user_id = ? AND item_id = ?",
                    (str(guild_id), str(user_id), str(item_id)),
                )
            else:
                conn.execute(
                    """
                    UPDATE chip_inventory SET qty = ? WHERE guild_id = ? AND user_id = ? AND item_id = ?
                    """,
                    (left, str(guild_id), str(user_id), str(item_id)),
                )
            return True

    def _add_item_conn(
        self,
        conn: sqlite3.Connection,
        guild_id: str,
        user_id: str,
        item_id: str,
        *,
        qty: int = 1,
        expires_at: float = 0.0,
    ) -> None:
        conn.execute(
            """
            INSERT INTO chip_inventory (guild_id, user_id, item_id, qty, expires_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, user_id, item_id) DO UPDATE SET
                qty = chip_inventory.qty + excluded.qty,
                expires_at = CASE
                    WHEN excluded.expires_at > chip_inventory.expires_at THEN excluded.expires_at
                    ELSE chip_inventory.expires_at
                END
            """,
            (str(guild_id), str(user_id), str(item_id), max(1, int(qty)), float(expires_at or 0)),
        )

    def _refund_hold_row(self, conn: sqlite3.Connection, row) -> None:
        gid = str(row["guild_id"])
        uid = str(row["user_id"])
        name = str(row["user_name"] or "")
        amount = int(row["amount"] or 0)
        if amount > 0:
            self._wallet_row(conn, gid, uid, name)
            conn.execute(
                "UPDATE chip_wallets SET balance = balance + ? WHERE guild_id = ? AND user_id = ?",
                (amount, gid, uid),
            )
        try:
            items = json.loads(row["items"] or "[]")
        except Exception:
            items = []
        if not isinstance(items, list):
            items = []
        for item_id in items:
            key = str(item_id or "").strip()
            if key:
                self._add_item_conn(conn, gid, uid, key)

    def place_hold(
        self,
        guild_id: str,
        user_id: str,
        amount: int,
        *,
        hold_id: str,
        items: list[str] | None = None,
        user_name: str = "",
    ) -> None:
        import time

        token = str(hold_id or "").strip()
        if not token:
            return
        payload = [str(item) for item in (items or []) if str(item).strip()]
        with _db_lock, self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO chip_holds
                    (hold_id, guild_id, user_id, user_name, amount, items, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    token,
                    str(guild_id),
                    str(user_id),
                    str(user_name or ""),
                    max(0, int(amount)),
                    json.dumps(payload),
                    time.time(),
                ),
            )

    def release_hold(self, hold_id: str | None, *, refund: bool = False) -> dict | None:
        token = str(hold_id or "").strip()
        if not token:
            return None
        with _db_lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM chip_holds WHERE hold_id = ?",
                (token,),
            ).fetchone()
            if not row:
                return None
            item = dict(row)
            if refund:
                self._refund_hold_row(conn, row)
            conn.execute("DELETE FROM chip_holds WHERE hold_id = ?", (token,))
            return item

    def restore_open_holds(self) -> int:
        with _db_lock, self._connect() as conn:
            rows = conn.execute("SELECT * FROM chip_holds").fetchall()
            restored = 0
            for row in rows:
                self._refund_hold_row(conn, row)
                conn.execute("DELETE FROM chip_holds WHERE hold_id = ?", (str(row["hold_id"]),))
                restored += 1
            return restored

    def close_challenge(self, challenge_id: int, *, refund: str | None = None) -> dict | None:
        item = self.challenge(challenge_id)
        if not item:
            return None
        status = str(item.get("status") or "")
        if status not in {"pending", "picking", "active"}:
            return None
        if refund is None:
            refund = "hold" if status == "pending" else "both"
        return self._expire_challenge_row(int(challenge_id), status, refund=refund)

    def abandon_live_challenges(self) -> int:
        with _db_lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT challenge_id FROM chip_challenges
                WHERE status IN ('pending', 'picking', 'active')
                """
            ).fetchall()
        closed = 0
        for row in rows:
            if self.close_challenge(int(row["challenge_id"])):
                closed += 1
        return closed

    def checkpoint(self) -> None:
        with _db_lock, self._connect() as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def upsert_message(
        self,
        *,
        message_id: str,
        guild_id: str,
        channel_id: str,
        channel_name: str,
        author_id: str,
        author_name: str,
        content: str,
        created_at: str,
        image_urls: list[str],
        jump_url: str,
    ) -> None:
        payload = json.dumps(image_urls[:8])
        with _db_lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO messages (
                    message_id, guild_id, channel_id, channel_name, author_id, author_name,
                    content, created_at, image_urls, has_image, jump_url
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(message_id) DO UPDATE SET
                    channel_name=excluded.channel_name,
                    author_name=excluded.author_name,
                    content=excluded.content,
                    image_urls=excluded.image_urls,
                    has_image=excluded.has_image,
                    jump_url=excluded.jump_url
                """,
                (
                    message_id,
                    guild_id,
                    channel_id,
                    channel_name,
                    author_id,
                    author_name,
                    content or "",
                    created_at,
                    payload,
                    1 if image_urls else 0,
                    jump_url,
                ),
            )

    def index_discord_message(self, message) -> None:
        if not message.guild:
            return
        image_urls = _collect_image_urls(message)
        created = message.created_at.isoformat() if message.created_at else datetime.utcnow().isoformat()
        jump = f"https://discord.com/channels/{message.guild.id}/{message.channel.id}/{message.id}"
        author = message.author
        self.upsert_message(
            message_id=str(message.id),
            guild_id=str(message.guild.id),
            channel_id=str(message.channel.id),
            channel_name=getattr(message.channel, "name", "") or "",
            author_id=str(author.id),
            author_name=str(getattr(author, "display_name", "") or getattr(author, "name", "") or ""),
            content=(message.content or "").strip(),
            created_at=created,
            image_urls=image_urls,
            jump_url=jump,
        )

    def _author_clause(self, author_hint: str) -> tuple[str, list[str]]:
        hint = (author_hint or "").strip().lower()
        if not hint or hint in {"someone", "anyone", "people", "everyone"}:
            return "", []
        like = f"%{hint}%"
        return (
            " AND (lower(author_name) LIKE ? OR lower(author_id) LIKE ?)",
            [like, like],
        )

    def search_word(
        self,
        word: str,
        guild_id: str,
        author_hint: str = "",
        limit: int = 50,
    ) -> list[dict]:
        token = (word or "").strip().lower()
        if not token or not guild_id:
            return []
        author_sql, author_args = self._author_clause(author_hint)
        query = f"""
            SELECT message_id, channel_name, author_name, content, created_at, jump_url
            FROM messages
            WHERE guild_id = ?
              AND lower(content) LIKE ?
              {author_sql}
            ORDER BY created_at DESC
            LIMIT ?
        """
        args = [guild_id, f"%{token}%", *author_args, limit]
        with _db_lock, self._connect() as conn:
            rows = conn.execute(query, args).fetchall()
        return [dict(row) for row in rows]

    def search_images(
        self,
        guild_id: str,
        author_hint: str = "",
        limit: int = 5,
    ) -> list[dict]:
        if not guild_id:
            return []
        author_sql, author_args = self._author_clause(author_hint)
        query = f"""
            SELECT message_id, channel_name, author_name, content, created_at, image_urls, jump_url
            FROM messages
            WHERE guild_id = ?
              AND has_image = 1
              {author_sql}
            ORDER BY created_at DESC
            LIMIT ?
        """
        args = [guild_id, *author_args, limit]
        with _db_lock, self._connect() as conn:
            rows = conn.execute(query, args).fetchall()
        results: list[dict] = []
        for row in rows:
            item = dict(row)
            try:
                item["image_urls"] = json.loads(item.get("image_urls") or "[]")
            except json.JSONDecodeError:
                item["image_urls"] = []
            results.append(item)
        return results

    def guild_stats(self, guild_id: str) -> dict:
        if not guild_id:
            return {"messages": 0, "channels": 0, "images": 0, "oldest": "", "newest": ""}
        with _db_lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    COUNT(*) AS messages,
                    COUNT(DISTINCT channel_id) AS channels,
                    SUM(CASE WHEN has_image = 1 THEN 1 ELSE 0 END) AS images,
                    MIN(created_at) AS oldest,
                    MAX(created_at) AS newest
                FROM messages
                WHERE guild_id = ?
                """,
                (guild_id,),
            ).fetchone()
        if not row:
            return {"messages": 0, "channels": 0, "images": 0, "oldest": "", "newest": ""}
        return {
            "messages": int(row["messages"] or 0),
            "channels": int(row["channels"] or 0),
            "images": int(row["images"] or 0),
            "oldest": _parse_ts(row["oldest"] or ""),
            "newest": _parse_ts(row["newest"] or ""),
        }

    def format_word_report(
        self,
        word: str,
        guild_id: str,
        author_hint: str = "",
        mode: str = "count",
    ) -> str:
        rows = self.search_word(word, guild_id, author_hint=author_hint, limit=50)
        if not rows:
            who = f"{author_hint} " if author_hint else ""
            return f"I couldn't find any messages where {who}said \"{word}\"."

        count = len(rows)
        who = author_hint or rows[0].get("author_name") or "they"
        header = f"\"{word}\" shows up {count} time{'s' if count != 1 else ''}"
        if author_hint:
            header += f" from {author_hint}"
        header += "."

        if mode == "count" and count <= 5:
            mode = "detail"

        lines = [header]
        for row in rows[:12]:
            when = _parse_ts(row.get("created_at") or "")
            channel = row.get("channel_name") or "unknown-channel"
            snippet = (row.get("content") or "").replace("\n", " ").strip()
            if len(snippet) > 140:
                snippet = snippet[:137] + "..."
            lines.append(f"- #{channel}, {when}: \"{snippet}\"")
        if count > 12:
            lines.append(f"- ...and {count - 12} more.")
        return "\n".join(lines)

    def image_context_for_question(
        self,
        guild_id: str,
        author_hint: str = "",
        limit: int = 3,
    ) -> tuple[list[str], str]:
        rows = self.search_images(guild_id, author_hint=author_hint, limit=limit)
        if not rows:
            return [], ""
        urls: list[str] = []
        lines = ["Indexed images from the server:"]
        for row in rows:
            when = _parse_ts(row.get("created_at") or "")
            channel = row.get("channel_name") or "unknown-channel"
            author = row.get("author_name") or "someone"
            lines.append(f"- {author} in #{channel} at {when}")
            for url in row.get("image_urls") or []:
                if url not in urls:
                    urls.append(url)
        return urls[:4], "\n".join(lines)

    def messages_for_profile(
        self,
        guild_id: str,
        hints: list[str],
        limit: int = 120,
        author_only: bool = False,
        content_min: int = 6,
    ) -> list[dict]:
        tokens = []
        seen = set()
        for hint in hints or []:
            token = (hint or "").strip().lower()
            if len(token) < 2 or token in seen:
                continue
            seen.add(token)
            tokens.append(token)
        if not tokens:
            return []
        clauses: list[str] = []
        args: list[str] = []
        if guild_id:
            clauses.append("guild_id = ?")
            args.append(guild_id)
        name_parts: list[str] = []
        for token in tokens[:12]:
            like = f"%{token}%"
            search_content = (
                not author_only
                and len(token) >= int(content_min)
                and " " not in token
            )
            if search_content:
                name_parts.append(
                    "(lower(author_name) LIKE ? OR lower(author_id) LIKE ? OR lower(content) LIKE ?)"
                )
                args.extend([like, like, like])
            else:
                name_parts.append("(lower(author_name) LIKE ? OR lower(author_id) LIKE ?)")
                args.extend([like, like])
        clauses.append("(" + " OR ".join(name_parts) + ")")
        where = " AND ".join(clauses)
        query = f"""
            SELECT message_id, channel_name, author_name, author_id, content, created_at, jump_url
            FROM messages
            WHERE {where}
              AND length(trim(content)) > 0
            ORDER BY created_at DESC
            LIMIT ?
        """
        args.append(int(limit))
        with _db_lock, self._connect() as conn:
            rows = conn.execute(query, args).fetchall()
        return [dict(row) for row in rows]


def resolve_author_hint(hint: str, member_roster: str = "") -> str:
    raw = (hint or "").strip().strip('"\'')
    if not raw:
        return ""
    lowered = raw.lower()
    if lowered in {"someone", "anyone", "people", "everyone", "he", "she", "they"}:
        return ""
    for line in (member_roster or "").splitlines():
        chunk = line.lower()
        if lowered in chunk:
            for part in line.split(","):
                part = part.strip()
                if part.lower().startswith("display="):
                    return part.split("=", 1)[1].strip()
                if part.lower().startswith("username="):
                    return part.split("=", 1)[1].strip()
    return raw


_WORD_CLEAN_RE = re.compile(r"^(?:the word|word)\s+", re.IGNORECASE)


def clean_search_word(raw: str) -> str:
    word = (raw or "").strip().strip("\"'“”‘’")
    word = _WORD_CLEAN_RE.sub("", word).strip()
    return word


COUNT_WORD_RE = re.compile(
    r"how many times (?:did|has|have|does|do)\s+(.+?)\s+(?:said|say|use|used)\s+(.+?)(?:\?|\.|!|$)",
    re.IGNORECASE,
)
COUNT_WORD_ANY_RE = re.compile(
    r"how many times (?:has|have|was|were)\s+(.+?)\s+(?:been\s+)?(?:said|used)(?:\?|\.|!|$)",
    re.IGNORECASE,
)
COUNT_SIMPLE_RE = re.compile(
    r"count how many times (.+?) (?:said|say|used|use) (.+?)(?:\?|\.|!|$)",
    re.IGNORECASE,
)
WHERE_WORD_RE = re.compile(
    r"where (?:did|has|have|does|do)\s+(.+?)\s+(?:said|say)\s+(.+?)(?:\?|\.|!|$)",
    re.IGNORECASE,
)
WHEN_WORD_RE = re.compile(
    r"when (?:did|has|have|does|do)\s+(.+?)\s+(?:said|say)\s+(.+?)(?:\?|\.|!|$)",
    re.IGNORECASE,
)
IMAGE_QUESTION_RE = re.compile(
    r"\b(?:what (?:was|is|did)|describe|explain|tell me about)\b.*\b(?:image|picture|photo|pic)\b",
    re.IGNORECASE,
)
IMAGE_FROM_RE = re.compile(
    r"\b(?:image|picture|photo|pic)\b.*\b(?:from|by|that|sent|posted|shared)\s+(.+?)(?:\?|\.|!|$)",
    re.IGNORECASE,
)


def parse_word_search(question: str) -> dict | None:
    text = (question or "").strip()
    if not text:
        return None
    for pattern, mode in (
        (COUNT_WORD_RE, "count"),
        (COUNT_SIMPLE_RE, "count"),
        (WHERE_WORD_RE, "where"),
        (WHEN_WORD_RE, "when"),
    ):
        match = pattern.search(text)
        if match:
            return {
                "mode": mode,
                "author": match.group(1).strip(),
                "word": clean_search_word(match.group(2)),
            }
    match = COUNT_WORD_ANY_RE.search(text)
    if match:
        return {"mode": "count", "author": "", "word": clean_search_word(match.group(1))}
    return None


def parse_image_author(question: str, member_roster: str = "") -> str:
    text = (question or "").strip()
    if not IMAGE_QUESTION_RE.search(text):
        return ""
    match = IMAGE_FROM_RE.search(text)
    if match:
        return resolve_author_hint(match.group(1), member_roster)
    match = re.search(
        r"\b(?:what (?:was|is)|describe).*(?:image|picture|photo|pic).*(?:from|by|that)\s+(.+?)(?:\?|\.|!|$)",
        text,
        re.IGNORECASE,
    )
    if match:
        return resolve_author_hint(match.group(1), member_roster)
    match = re.search(r"\b(.+?)(?:'s|s)\s+(?:image|picture|photo|pic)\b", text, re.IGNORECASE)
    if match:
        return resolve_author_hint(match.group(1), member_roster)
    return ""
