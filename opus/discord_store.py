from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from threading import Lock

from opus.settings import appdata_dir

_db_lock = Lock()


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
        conn = sqlite3.connect(self.path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
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
                """
            )

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
