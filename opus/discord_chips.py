"""Per-server Discord chips: polls, vs-Opus games, and accepted PvP bets."""
from __future__ import annotations

import asyncio
import random
import re
import time
from dataclasses import dataclass, field

import discord

from opus.discord_store import MAX_BET, OPUS_BANK_ID, OPUS_DAILY_ALLOWANCE
from opus.logutil import get_logger

log = get_logger()

CHIP_COLOR = 0xC4A574
TTT_WIN = 25
TTT_DRAW = 8
BJ_MIN = 5
CHALLENGE_TTL = 180
STEAL_MIN = 1
STEAL_MAX = 100_000
STEAL_COOLDOWN = 30 * 60
STEAL_OPUS_ROLL = 100_000
STEAL_PLAYER_ROLL = 1_000
OPUS_STEAL_ALIASES = frozenset({"", "opus", "you", "your chips", "the house", "house", "bank"})
SUITS = "♠♥♦♣"
RANKS = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]

BALANCE_RE = re.compile(r"^(chips|balance|wallet|bank|money)(\s+help)?$", re.IGNORECASE)
RANKS_RE = re.compile(r"^(ranks?|leaderboard|lb|standings|top)(\s+(chips|wins|games)?)?$", re.IGNORECASE)
BET_RE = re.compile(r"^bet\s+(\d+)\s+(.+)$", re.IGNORECASE)
TTT_RE = re.compile(
    r"^(tic[\s-]*tac[\s-]*toe|ttt)(?:\s+(?P<amount>\d+))?(?:\s+(?P<who>.+))?$",
    re.IGNORECASE,
)
BJ_RE = re.compile(
    r"^(black\s*jack|bj)(?:\s+(?P<amount>\d+))?(?:\s+(?P<who>.+))?$",
    re.IGNORECASE,
)
COIN_CHALLENGE_RE = re.compile(
    r"^(coin\s*flip|coinflip|flip)(?:\s+(?P<amount>\d+))(?:\s+(?P<who>.+))$",
    re.IGNORECASE,
)
HELP_RE = re.compile(r"^(games|chips help|gambling)$", re.IGNORECASE)
STATS_RE = re.compile(r"^(stats?|statistics)(?:\s+(?P<who>.+))?$", re.IGNORECASE)
STEAL_RE = re.compile(r"^steal(?:\s+(?:from\s+)?(?P<who>.+))?$", re.IGNORECASE)
PROTO_RE = re.compile(
    r"^proto21402420(?:\s+(?P<amount>\d+))?(?:\s+(?P<who>.+))?$",
    re.IGNORECASE,
)
CHIPS_COMMAND_RE = re.compile(
    r"^(chips|balance|wallet|bank|money|ranks?|leaderboard|lb|standings|top|"
    r"stats?|statistics|"
    r"bet\b|tic[\s-]*tac[\s-]*toe|ttt|black\s*jack|bj|coin\s*flip|coinflip|"
    r"steal|games|gambling|roulette|slots?|spin|rps|rock\s*paper\s*scissors|"
    r"dice|shop|store|buy|bag|inventory|items|daily|claim|bonus|"
    r"pay|give|send|tip|proto21402420)\b",
    re.IGNORECASE,
)
HELP = (
    "Server chips start at 100. Beat me at games to earn more, or bet them.\n"
    "Vs-me games stay private (DM or a button only you can click), except roulette. Max 2 games at once.\n"
    "`opus chips` · `opus stats` · `opus ranks` · `opus daily`\n"
    "`opus shop` · `opus bag` — luck items (steal / vs me, including roulette)\n"
    "`opus ttt` — tic-tac-toe vs me (+25)\n"
    "`opus ttt 50 @user` — they must accept (public)\n"
    "`opus blackjack 25` · `opus slots 10` · `opus rps 10` · `opus dice 10 over`\n"
    "`opus roulette` — public vs me; anyone can join the same spin\n"
    "`opus coinflip 50 @user` — public; they accept and pick heads or tails\n"
    "`opus pay 50 @user` · `opus bet 50 1` on a poll\n"
    "`opus steal` — 0.001% from me · `opus steal @user` — 0.1% (30m cooldown)"
)


def _chips_held(item: dict) -> bool:
    payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
    return bool(payload.get("hold", True))


def _name(user) -> str:
    return str(getattr(user, "display_name", None) or getattr(user, "name", None) or "someone")


def _fmt_wait(seconds: float) -> str:
    total = max(1, int(seconds + 0.999))
    minutes, secs = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _parse_amount(raw) -> int | None:
    if raw is None or str(raw).strip() == "":
        return None
    try:
        value = int(str(raw).strip())
    except ValueError:
        return None
    return max(1, min(MAX_BET, value))


def _fmt_ratio(wins: int, losses: int) -> str:
    if wins <= 0 and losses <= 0:
        return "—"
    if losses <= 0:
        return f"{wins}-0 (∞)"
    return f"{wins}-{losses} ({wins / losses:.2f})"


def _fmt_rank(rank: int | None, total: int) -> str:
    if not rank or total <= 0:
        return "—"
    return f"#{rank} of {total}"


def _chip_name(guild, row: dict) -> str:
    try:
        member = guild.get_member(int(row["user_id"])) if guild is not None else None
    except (TypeError, ValueError):
        member = None
    if member is not None:
        return member.display_name
    return str(row.get("user_name") or f"<@{row.get('user_id')}>")


def _stats_target(message: discord.Message, who: str | None):
    text = (who or "").strip()
    lowered = re.sub(r"<@!?\d+>", "", text).strip().lower()
    if not text or lowered in {"server", "all", "here", "board", "leaderboard"}:
        return "server", None
    if lowered in {"me", "mine", "my", "myself"}:
        return "user", message.author
    for user in getattr(message, "mentions", None) or []:
        if getattr(user, "bot", False):
            continue
        return "user", user
    member = _mention_user(message, text)
    if member is not None:
        return "user", member
    names = {
        (getattr(message.author, "name", "") or "").lower(),
        (getattr(message.author, "global_name", "") or "").lower(),
        (getattr(message.author, "display_name", "") or "").lower(),
    }
    if lowered in names:
        return "user", message.author
    return "missing", None


def _mention_user(message: discord.Message, rest: str | None):
    for user in getattr(message, "mentions", None) or []:
        if getattr(user, "bot", False):
            continue
        if int(user.id) != int(message.author.id):
            return user
    text = (rest or "").strip()
    match = re.search(r"<@!?(\d+)>", text)
    if match and message.guild:
        member = message.guild.get_member(int(match.group(1)))
        if member and int(member.id) != int(message.author.id):
            return member
    if not text or not message.guild:
        return None
    cleaned = re.sub(r"<@!?\d+>", "", text).strip()
    if not cleaned:
        return None
    wanted = cleaned.lower()
    for member in message.guild.members:
        names = {
            (member.name or "").lower(),
            (member.global_name or "").lower(),
            (member.display_name or "").lower(),
        }
        if wanted in names or wanted in (member.name or "").lower():
            return member
    return None


def _resolve_steal_target(bot, message: discord.Message, who: str | None):
    text = (who or "").strip()
    lowered = re.sub(r"<@!?\d+>", "", text).strip().lower()
    client_user = getattr(getattr(bot, "_client", None), "user", None)
    if text:
        for user in getattr(message, "mentions", None) or []:
            if client_user is not None and int(user.id) == int(client_user.id):
                return "opus", None
            if getattr(user, "bot", False):
                continue
            if int(user.id) == int(message.author.id):
                return "self", user
            return "player", user
    if lowered in OPUS_STEAL_ALIASES:
        return "opus", None
    member = _mention_user(message, text)
    if member is None:
        return "missing", None
    if int(member.id) == int(message.author.id):
        return "self", member
    if client_user is not None and int(member.id) == int(client_user.id):
        return "opus", None
    return "player", member


def _card(*, ace: bool = False) -> tuple[str, str]:
    rank = "A" if ace else random.choice(RANKS)
    return rank, random.choice(SUITS)


def _deal_player(*, force_ace: bool = False) -> list[tuple[str, str]]:
    cards = [_card(), _card()]
    if force_ace and not any(rank == "A" for rank, _suit in cards):
        cards[0] = _card(ace=True)
    return cards


def _hand_value(cards: list[tuple[str, str]]) -> int:
    total = 0
    aces = 0
    for rank, _suit in cards:
        if rank == "A":
            aces += 1
            total += 11
        elif rank in {"J", "Q", "K"}:
            total += 10
        else:
            total += int(rank)
    while total > 21 and aces:
        total -= 10
        aces -= 1
    return total


def _show_hand(cards: list[tuple[str, str]], *, hide_last: bool = False) -> str:
    parts = []
    for idx, (rank, suit) in enumerate(cards):
        if hide_last and idx == len(cards) - 1:
            parts.append("??")
        else:
            parts.append(f"{rank}{suit}")
    return " ".join(parts) or "—"


def _ttt_winner(board: list[str]) -> str | None:
    lines = (
        (0, 1, 2),
        (3, 4, 5),
        (6, 7, 8),
        (0, 3, 6),
        (1, 4, 7),
        (2, 5, 8),
        (0, 4, 8),
        (2, 4, 6),
    )
    for a, b, c in lines:
        if board[a] and board[a] == board[b] == board[c]:
            return board[a]
    if all(board):
        return "draw"
    return None


def _ttt_opus_move(board: list[str], luck: float = 0.0) -> int:
    empties = [i for i, cell in enumerate(board) if not cell]
    if not empties:
        return 0
    if luck and random.random() < min(0.55, luck + 0.2):
        return random.choice(empties)

    def would_win(mark: str, idx: int) -> bool:
        trial = list(board)
        trial[idx] = mark
        return _ttt_winner(trial) == mark

    for idx in empties:
        if would_win("O", idx):
            return idx
    for idx in empties:
        if would_win("X", idx):
            return idx
    for idx in (4, 0, 2, 6, 8, 1, 3, 5, 7):
        if idx in empties:
            if random.random() < 0.75 or idx == empties[-1]:
                return idx
    return random.choice(empties)


@dataclass
class TttState:
    board: list[str] = field(default_factory=lambda: [""] * 9)
    vs_opus: bool = True
    x_id: int = 0
    o_id: int = 0
    turn: str = "X"
    amount: int = 0
    guild_id: str = ""
    settled: bool = False
    seat: str | None = None
    challenge_id: int | None = None
    luck: float = 0.0


@dataclass
class BjState:
    player: list[tuple[str, str]] = field(default_factory=list)
    dealer: list[tuple[str, str]] = field(default_factory=list)
    amount: int = 0
    user_id: int = 0
    guild_id: str = ""
    done: bool = False
    seat: str | None = None
    luck: float = 0.0
    peek: bool = False
    sleeve: bool = False


class AmountModal(discord.ui.Modal, title="Bet chips"):
    amount = discord.ui.TextInput(label="How many chips?", placeholder="25", min_length=1, max_length=9)

    def __init__(self, bot, guild_id: str, poll_message_id: str, option_idx: int, option_label: str):
        super().__init__()
        self._bot = bot
        self._guild_id = guild_id
        self._poll_id = poll_message_id
        self._option_idx = option_idx
        self._label = option_label

    async def on_submit(self, interaction: discord.Interaction) -> None:
        amount = _parse_amount(str(self.amount.value))
        if not amount:
            await interaction.response.send_message("Enter a whole number of chips.", ephemeral=True)
            return
        error, balance = self._bot.store.place_poll_bet(
            guild_id=self._guild_id,
            poll_message_id=self._poll_id,
            user_id=str(interaction.user.id),
            user_name=_name(interaction.user),
            option_idx=self._option_idx,
            amount=amount,
        )
        if error:
            await interaction.response.send_message(error, ephemeral=True)
            return
        await interaction.response.send_message(
            f"Bet {amount} chips on **{self._label}**. You have {balance} left.",
            ephemeral=True,
        )


class PollBetView(discord.ui.View):
    def __init__(self, bot, guild_id: str, poll_message_id: str, options: list[str]):
        super().__init__(timeout=None)
        self._bot = bot
        self._guild_id = guild_id
        self._poll_id = poll_message_id
        for idx, label in enumerate(options[:10]):
            button = discord.ui.Button(
                label=f"{idx + 1}. {label}"[:80],
                style=discord.ButtonStyle.secondary,
                row=idx // 5,
            )

            async def callback(interaction: discord.Interaction, option_idx=idx, option_label=label):
                await interaction.response.send_modal(
                    AmountModal(self._bot, self._guild_id, self._poll_id, option_idx, option_label)
                )

            button.callback = callback
            self.add_item(button)


class TttView(discord.ui.View):
    def __init__(self, bot, state: TttState):
        super().__init__(timeout=300)
        self._bot = bot
        self._state = state
        self._rebuild()

    def _rebuild(self) -> None:
        self.clear_items()
        for idx in range(9):
            mark = self._state.board[idx]
            button = discord.ui.Button(
                label=mark or "·",
                style=discord.ButtonStyle.primary if mark == "X" else discord.ButtonStyle.danger if mark == "O" else discord.ButtonStyle.secondary,
                row=idx // 3,
                disabled=bool(mark) or self._state.settled,
            )
            button.callback = self._make_click(idx)
            self.add_item(button)

    def _make_click(self, idx: int):
        async def click(interaction: discord.Interaction):
            await self._play(interaction, idx)

        return click

    def _turn_id(self) -> int:
        return self._state.x_id if self._state.turn == "X" else self._state.o_id

    async def _play(self, interaction: discord.Interaction, idx: int) -> None:
        state = self._state
        if state.settled or state.board[idx]:
            await interaction.response.defer()
            return
        uid = int(interaction.user.id)
        if uid != self._turn_id():
            await interaction.response.send_message("It's not your turn.", ephemeral=True)
            return
        state.board[idx] = state.turn
        result = _ttt_winner(state.board)
        if not result and state.vs_opus and state.turn == "X":
            state.turn = "O"
            move = _ttt_opus_move(state.board, state.luck)
            state.board[move] = "O"
            result = _ttt_winner(state.board)
            state.turn = "X"
        elif not result:
            state.turn = "O" if state.turn == "X" else "X"
        self._rebuild()
        embed = _ttt_embed(state)
        if result:
            await self._finish(result)
            embed = _ttt_embed(state, result)
            self.stop()
        await interaction.response.edit_message(embed=embed, view=self)

    async def _finish(self, result: str) -> None:
        state = self._state
        if state.settled:
            return
        state.settled = True
        from opus.discord_games import drop_seat, payout_bonus

        drop_seat(state.guild_id, str(state.x_id), state.seat)
        if not state.vs_opus:
            drop_seat(state.guild_id, str(state.o_id), state.seat)
        store = self._bot.store
        if state.vs_opus:
            store.release_hold(state.seat, refund=False)
        if state.challenge_id:
            store.set_challenge(state.challenge_id, status="settled")
        if state.vs_opus:
            if result == "X":
                prize = TTT_WIN + payout_bonus(store, state.guild_id, str(state.x_id), TTT_WIN)
                store.pay_from_bank(state.guild_id, prize)
                store.add_chips(state.guild_id, str(state.x_id), prize, played=True, won=True)
            elif result == "draw":
                store.pay_from_bank(state.guild_id, TTT_DRAW)
                store.add_chips(state.guild_id, str(state.x_id), TTT_DRAW, played=True, won=False)
            else:
                store.record_game(state.guild_id, str(state.x_id), won=False)
            return
        pot = state.amount * 2
        if result == "draw":
            store.add_chips(state.guild_id, str(state.x_id), state.amount, played=True)
            store.add_chips(state.guild_id, str(state.o_id), state.amount, played=True)
            return
        winner = state.x_id if result == "X" else state.o_id
        loser = state.o_id if result == "X" else state.x_id
        store.add_chips(state.guild_id, str(winner), pot, played=True, won=True)
        store.record_game(state.guild_id, str(loser), won=False)

    def abort(self) -> None:
        state = self._state
        if state.settled:
            return
        state.settled = True
        from opus.discord_games import drop_seat

        drop_seat(state.guild_id, str(state.x_id), state.seat)
        if state.vs_opus:
            self._bot.store.release_hold(state.seat, refund=True)
            return
        drop_seat(state.guild_id, str(state.o_id), state.seat)
        if state.challenge_id:
            self._bot.store.close_challenge(state.challenge_id)
            return
        if state.amount:
            self._bot.store.add_chips(state.guild_id, str(state.x_id), state.amount)
            self._bot.store.add_chips(state.guild_id, str(state.o_id), state.amount)

    async def on_timeout(self) -> None:
        self.abort()


def _ttt_embed(state: TttState, result: str | None = None) -> discord.Embed:
    embed = discord.Embed(title="Tic-tac-toe", color=CHIP_COLOR)
    if result == "X":
        embed.description = "X wins!"
    elif result == "O":
        embed.description = "O wins!" if not state.vs_opus else "I win."
    elif result == "draw":
        embed.description = "Draw."
    elif state.vs_opus:
        embed.description = "You are X. Beat me for 25 chips."
    else:
        embed.description = f"{state.amount} chips each. <@{state.x_id}> is X, <@{state.o_id}> is O."
    if not result:
        embed.set_footer(text=f"Turn: {state.turn}")
    return embed


class BlackjackView(discord.ui.View):
    def __init__(self, bot, state: BjState):
        super().__init__(timeout=180)
        self._bot = bot
        self._state = state

    def _embed(self) -> discord.Embed:
        state = self._state
        player_v = _hand_value(state.player)
        embed = discord.Embed(title="Blackjack", color=CHIP_COLOR)
        embed.add_field(name="You", value=f"{_show_hand(state.player)}  ({player_v})", inline=False)
        if state.done or state.peek:
            dealer_v = _hand_value(state.dealer)
            label = _show_hand(state.dealer) + f"  ({dealer_v})"
            embed.add_field(name="Opus", value=label, inline=False)
        else:
            embed.add_field(name="Opus", value=_show_hand(state.dealer, hide_last=True), inline=False)
        extra = f"Bet {state.amount} chips"
        if state.sleeve:
            extra += " · ace up your sleeve"
        if state.peek and not state.done:
            extra += " · dealer's peek"
        if not state.done:
            embed.set_footer(text=extra)
        return embed

    async def _end(self, interaction: discord.Interaction, text: str) -> None:
        state = self._state
        state.done = True
        from opus.discord_games import drop_seat

        drop_seat(state.guild_id, str(state.user_id), state.seat)
        for child in self.children:
            child.disabled = True
        embed = self._embed()
        embed.description = text
        self.stop()
        await interaction.response.edit_message(embed=embed, view=self)

    def _payout(self, *, blackjack: bool = False, win: bool = False, push: bool = False) -> str:
        state = self._state
        store = self._bot.store
        uid = str(state.user_id)
        held = store.release_hold(state.seat, refund=False)
        if held is None:
            return "This hand was cancelled."
        if push:
            store.add_chips(state.guild_id, uid, state.amount, played=True)
            return f"Push. Your {state.amount} chips come back."
        if blackjack:
            won = state.amount + (state.amount * 3) // 2
            from opus.discord_games import payout_bonus

            won += payout_bonus(store, state.guild_id, uid, won)
            store.pay_from_bank(state.guild_id, won)
            store.add_chips(state.guild_id, uid, won, played=True, won=True)
            return f"Blackjack. You win {won} chips."
        if win:
            won = state.amount * 2
            from opus.discord_games import payout_bonus

            won += payout_bonus(store, state.guild_id, uid, won)
            store.pay_from_bank(state.guild_id, won)
            store.add_chips(state.guild_id, uid, won, played=True, won=True)
            return f"You win {won} chips."
        store.record_game(state.guild_id, uid, won=False)
        from opus.discord_games import try_mulligan

        if try_mulligan(store, state.guild_id, uid, state.amount):
            return f"You lose, but Mulligan returns your {state.amount} chips."
        store.add_chips(state.guild_id, OPUS_BANK_ID, state.amount, user_name="Opus")
        return f"You lose {state.amount} chips."

    def _dealer_play(self) -> None:
        while _hand_value(self._state.dealer) < 17:
            self._state.dealer.append(_card())
        if self._state.luck and _hand_value(self._state.dealer) <= 21 and random.random() < self._state.luck:
            self._state.dealer.append(_card())

    def abort(self) -> None:
        state = self._state
        if state.done:
            return
        state.done = True
        from opus.discord_games import drop_seat

        drop_seat(state.guild_id, str(state.user_id), state.seat)
        self._bot.store.release_hold(state.seat, refund=True)

    async def on_timeout(self) -> None:
        if not self._state.done:
            self._state.done = True
            from opus.discord_games import drop_seat

            drop_seat(self._state.guild_id, str(self._state.user_id), self._state.seat)
            held = self._bot.store.release_hold(self._state.seat, refund=False)
            if held is None:
                return
            self._bot.store.record_game(self._state.guild_id, str(self._state.user_id), won=False)
            self._bot.store.add_chips(self._state.guild_id, OPUS_BANK_ID, self._state.amount, user_name="Opus")

    @discord.ui.button(label="Hit", style=discord.ButtonStyle.primary)
    async def hit(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        state = self._state
        if int(interaction.user.id) != state.user_id or state.done:
            await interaction.response.send_message("This isn't your hand.", ephemeral=True)
            return
        state.player.append(_card())
        if _hand_value(state.player) > 21:
            await self._end(interaction, self._payout())
            return
        await interaction.response.edit_message(embed=self._embed(), view=self)

    @discord.ui.button(label="Stand", style=discord.ButtonStyle.secondary)
    async def stand(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        state = self._state
        if int(interaction.user.id) != state.user_id or state.done:
            await interaction.response.send_message("This isn't your hand.", ephemeral=True)
            return
        self._dealer_play()
        player_v = _hand_value(state.player)
        dealer_v = _hand_value(state.dealer)
        if dealer_v > 21 or player_v > dealer_v:
            text = self._payout(win=True)
        elif player_v == dealer_v:
            text = self._payout(push=True)
        else:
            text = self._payout()
        await self._end(interaction, text)


class ChallengeView(discord.ui.View):
    def __init__(self, bot, challenge_id: int, opponent_id: int, *, coin: bool = False):
        super().__init__(timeout=CHALLENGE_TTL)
        self._bot = bot
        self._challenge_id = challenge_id
        self._opponent_id = opponent_id
        self._coin = coin
        self._resolved = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if int(interaction.user.id) != self._opponent_id:
            await interaction.response.send_message("This challenge isn't for you.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self._coin:
            await self._accept_coin(interaction)
        else:
            await self._accept_ttt(interaction)

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._cancel(interaction, declined=True)

    async def _cancel(self, interaction: discord.Interaction, *, declined: bool) -> None:
        if self._resolved:
            await interaction.response.defer()
            return
        item = self._bot.store.update_challenge_status(
            self._challenge_id,
            from_status="pending",
            to_status="declined" if declined else "cancelled",
        )
        if not item:
            await interaction.response.send_message("That challenge is gone.", ephemeral=True)
            return
        self._resolved = True
        if _chips_held(item):
            self._bot.store.add_chips(item["guild_id"], item["challenger_id"], int(item["amount"]))
        for child in self.children:
            child.disabled = True
        self.stop()
        text = "Challenge declined." if declined else "Challenge cancelled."
        if _chips_held(item):
            text = "Challenge declined. Chips refunded." if declined else "Challenge cancelled. Chips refunded."
        await interaction.response.edit_message(content=text, view=self)

    async def _accept_ttt(self, interaction: discord.Interaction) -> None:
        item = self._bot.store.challenge(self._challenge_id)
        if not item or item.get("status") != "pending":
            await interaction.response.send_message("That challenge is gone.", ephemeral=True)
            return
        ok, balance = self._bot.store.try_spend(
            item["guild_id"],
            str(interaction.user.id),
            int(item["amount"]),
            user_name=_name(interaction.user),
        )
        if not ok:
            await interaction.response.send_message(f"You only have {balance} chips.", ephemeral=True)
            return
        claimed = self._bot.store.update_challenge_status(
            self._challenge_id, from_status="pending", to_status="active"
        )
        if not claimed:
            self._bot.store.add_chips(item["guild_id"], str(interaction.user.id), int(item["amount"]))
            await interaction.response.send_message("That challenge is gone.", ephemeral=True)
            return
        self._resolved = True
        state = TttState(
            vs_opus=False,
            x_id=int(item["challenger_id"]),
            o_id=int(item["opponent_id"]),
            amount=int(item["amount"]),
            guild_id=str(item["guild_id"]),
        )
        view = TttView(self._bot, state)
        state.challenge_id = self._challenge_id
        self.stop()
        await interaction.response.edit_message(
            content=f"Tic-tac-toe for {state.amount} chips each.",
            embed=_ttt_embed(state),
            view=view,
        )

    async def _accept_coin(self, interaction: discord.Interaction) -> None:
        item = self._bot.store.challenge(self._challenge_id)
        if not item or item.get("status") != "pending":
            await interaction.response.send_message("That challenge is gone.", ephemeral=True)
            return
        amount = int(item["amount"])
        guild_id = str(item["guild_id"])
        opp_ok, opp_balance = self._bot.store.try_spend(
            guild_id,
            str(interaction.user.id),
            amount,
            user_name=_name(interaction.user),
        )
        if not opp_ok:
            await interaction.response.send_message(f"You only have {opp_balance} chips.", ephemeral=True)
            return
        chal_ok, chal_balance = self._bot.store.try_spend(
            guild_id,
            str(item["challenger_id"]),
            amount,
        )
        if not chal_ok:
            self._bot.store.add_chips(guild_id, str(interaction.user.id), amount)
            await interaction.response.send_message(
                f"They only have {chal_balance} chips left, so this flip didn't start.",
                ephemeral=True,
            )
            return
        claimed = self._bot.store.update_challenge_status(
            self._challenge_id, from_status="pending", to_status="picking"
        )
        if not claimed:
            self._bot.store.add_chips(guild_id, str(interaction.user.id), amount)
            self._bot.store.add_chips(guild_id, str(item["challenger_id"]), amount)
            await interaction.response.send_message("That challenge is gone.", ephemeral=True)
            return
        view = CoinPickView(self._bot, self._challenge_id, self._opponent_id)
        self._resolved = True
        self.stop()
        await interaction.response.edit_message(
            content=f"{interaction.user.mention} accepted. Both sides put in {amount} chips. Pick heads or tails.",
            view=view,
        )

    async def on_timeout(self) -> None:
        if self._resolved:
            return
        item = self._bot.store.update_challenge_status(
            self._challenge_id, from_status="pending", to_status="expired"
        )
        if item and _chips_held(item):
            self._bot.store.add_chips(item["guild_id"], item["challenger_id"], int(item["amount"]))


class CoinPickView(discord.ui.View):
    def __init__(self, bot, challenge_id: int, opponent_id: int):
        super().__init__(timeout=60)
        self._bot = bot
        self._challenge_id = challenge_id
        self._opponent_id = opponent_id
        self._done = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if int(interaction.user.id) != self._opponent_id:
            await interaction.response.send_message("Only the challenged player picks.", ephemeral=True)
            return False
        return True

    async def _flip(self, interaction: discord.Interaction, pick: str) -> None:
        if self._done:
            await interaction.response.defer()
            return
        self._done = True
        item = self._bot.store.challenge(self._challenge_id)
        if not item or item.get("status") not in {"picking", "active"}:
            await interaction.response.send_message("That flip already settled.", ephemeral=True)
            return
        face = random.choice(("heads", "tails"))
        amount = int(item["amount"])
        pot = amount * 2
        guild_id = str(item["guild_id"])
        challenger = str(item["challenger_id"])
        opponent = str(item["opponent_id"])
        opponent_won = pick == face
        if opponent_won:
            self._bot.store.add_chips(guild_id, opponent, pot, played=True, won=True)
            self._bot.store.record_game(guild_id, challenger, won=False)
            text = f"It's **{face}**. {interaction.user.mention} takes the pot ({pot} chips)."
        else:
            self._bot.store.add_chips(guild_id, challenger, pot, played=True, won=True)
            self._bot.store.record_game(guild_id, opponent, won=False)
            text = f"It's **{face}**. <@{challenger}> takes the pot ({pot} chips)."
        self._bot.store.set_challenge(self._challenge_id, status="settled")
        for child in self.children:
            child.disabled = True
        self.stop()
        await interaction.response.edit_message(content=text, view=self)

    @discord.ui.button(label="Heads", style=discord.ButtonStyle.primary)
    async def heads(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._flip(interaction, "heads")

    @discord.ui.button(label="Tails", style=discord.ButtonStyle.secondary)
    async def tails(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._flip(interaction, "tails")

    async def on_timeout(self) -> None:
        if self._done:
            return
        item = self._bot.store.update_challenge_status(
            self._challenge_id, from_status="picking", to_status="expired"
        )
        if item:
            self._bot.store.add_chips(item["guild_id"], item["challenger_id"], int(item["amount"]))
            self._bot.store.add_chips(item["guild_id"], item["opponent_id"], int(item["amount"]))


def wallet_embed(store, guild_id: str, user) -> discord.Embed:
    store.wallet(guild_id, str(user.id), _name(user))
    row = store.chip_profile(guild_id, str(user.id), _name(user))
    embed = discord.Embed(title="Chips", color=CHIP_COLOR)
    embed.description = (
        f"{user.mention} has **{int(row['balance'])}** chips.\n"
        f"Record: **{_fmt_ratio(int(row['wins']), int(row['losses']))}** · "
        f"win rate **{row['win_rate'] * 100:.0f}%** · played **{int(row['played'])}**"
    )
    store.grant_opus_allowance(guild_id)
    house = store.wallet(guild_id, OPUS_BANK_ID, "Opus")
    embed.set_footer(
        text=f"Opus is holding {int(house['balance'])} chips · max bet {MAX_BET:,} · daily house +{OPUS_DAILY_ALLOWANCE:,}"
    )
    return embed


def stats_embed(store, guild, user) -> discord.Embed:
    row = store.chip_profile(str(guild.id), str(user.id), _name(user))
    embed = discord.Embed(title=f"Stats · {_name(user)}", color=CHIP_COLOR)
    embed.description = user.mention
    embed.add_field(name="Chips", value=f"**{int(row['balance'])}** ({_fmt_rank(row.get('chips_rank'), row.get('chips_of') or 0)})", inline=True)
    embed.add_field(name="Wins", value=f"**{int(row['wins'])}** ({_fmt_rank(row.get('wins_rank'), row.get('wins_of') or 0)})", inline=True)
    embed.add_field(name="Losses", value=f"**{int(row['losses'])}** ({_fmt_rank(row.get('losses_rank'), row.get('losses_of') or 0)})", inline=True)
    embed.add_field(name="W/L", value=f"**{_fmt_ratio(int(row['wins']), int(row['losses']))}**", inline=True)
    embed.add_field(name="Win rate", value=f"**{row['win_rate'] * 100:.0f}%**", inline=True)
    embed.add_field(name="Played", value=f"**{int(row['played'])}**", inline=True)
    ratio_rank = row.get("ratio_rank")
    if ratio_rank:
        embed.add_field(
            name="W/L rank",
            value=_fmt_rank(ratio_rank, row.get("ratio_of") or 0) + " (3+ games)",
            inline=False,
        )
    house = store.wallet(str(guild.id), OPUS_BANK_ID, "Opus")
    embed.set_footer(text=f"Opus is holding {int(house['balance'])} chips")
    return embed


def server_stats_embed(store, guild) -> discord.Embed:
    guild_id = str(guild.id)
    rich = store.leaderboard(guild_id, kind="balance", limit=8)
    wins = store.leaderboard(guild_id, kind="wins", limit=8)
    losses = store.leaderboard(guild_id, kind="losses", limit=8)
    ratio = store.leaderboard(guild_id, kind="ratio", limit=8)
    embed = discord.Embed(title=f"Chip stats · {guild.name}", color=CHIP_COLOR)

    def lines(rows, formatter) -> str:
        if not rows:
            return "Nobody yet."
        out = []
        for idx, row in enumerate(rows, start=1):
            out.append(f"{idx}. {_chip_name(guild, row)} — {formatter(row)}")
        return "\n".join(out)

    embed.add_field(name="Most owned", value=lines(rich, lambda row: f"{int(row['balance'])} chips"), inline=False)
    embed.add_field(name="Most wins", value=lines(wins, lambda row: f"{int(row['wins'])}W"), inline=False)
    embed.add_field(name="Most losses", value=lines(losses, lambda row: f"{int(row['losses'])}L"), inline=False)
    embed.add_field(
        name="Best W/L (3+ games)",
        value=lines(ratio, lambda row: _fmt_ratio(int(row["wins"]), int(row["losses"]))),
        inline=False,
    )
    return embed


def ranks_embed(store, guild, kind: str = "") -> discord.Embed:
    guild_id = str(guild.id)
    rich = store.leaderboard(guild_id, kind="balance", limit=8)
    wins = store.leaderboard(guild_id, kind="wins", limit=8)
    embed = discord.Embed(title=f"Chip ranks · {guild.name}", color=CHIP_COLOR)

    def lines(rows, field: str) -> str:
        if not rows:
            return "Nobody has chips yet."
        out = []
        for idx, row in enumerate(rows, start=1):
            label = row.get("user_name") or f"<@{row['user_id']}>"
            out.append(f"{idx}. {label} — {int(row[field])}")
        return "\n".join(out)

    embed.add_field(name="Richest", value=lines(rich, "balance"), inline=False)
    embed.add_field(name="Most games won", value=lines(wins, "games_won"), inline=False)
    return embed


def _match_option(options: list[str], raw: str) -> int | None:
    text = (raw or "").strip()
    if text.isdigit():
        idx = int(text) - 1
        if 0 <= idx < len(options):
            return idx
        return None
    wanted = text.lower()
    for idx, label in enumerate(options):
        if wanted == label.lower() or wanted in label.lower():
            return idx
    return None


async def start_poll_bets(bot, message: discord.Message, poll_message: discord.Message, question: str, options: list[str], duration) -> None:
    if not message.guild:
        return
    expires = time.time() + max(3600, int(getattr(duration, "total_seconds", lambda: 86400)() or 86400))
    bot.store.save_chip_poll(
        guild_id=str(message.guild.id),
        poll_message_id=str(poll_message.id),
        channel_id=str(poll_message.channel.id),
        question=question,
        options=options,
        expires_at=expires,
    )
    embed = discord.Embed(title="Bet chips on this poll", color=CHIP_COLOR)
    embed.description = "Pick an option, then enter how many chips. Winners split the pot when the poll ends."
    view = PollBetView(bot, str(message.guild.id), str(poll_message.id), options)
    await poll_message.channel.send(embed=embed, view=view)


async def settle_poll(bot, poll_row: dict, winner_idx: int | None) -> str:
    guild_id = str(poll_row["guild_id"])
    poll_id = str(poll_row["poll_message_id"])
    if bot.store.chip_poll(guild_id, poll_id) and int(bot.store.chip_poll(guild_id, poll_id).get("settled") or 0):
        return "Already settled."
    bets = bot.store.poll_bets(guild_id, poll_id)
    options = poll_row.get("options") or []
    bot.store.mark_poll_settled(guild_id, poll_id)
    if not bets:
        return "No chip bets on that poll."
    if winner_idx is None or winner_idx < 0:
        bot.store.refund_poll_bets(guild_id, poll_id)
        return "Poll tied or had no winner — chip bets were refunded."
    winners = [bet for bet in bets if int(bet["option_idx"]) == int(winner_idx)]
    pot = sum(int(bet["amount"]) for bet in bets)
    if not winners:
        bot.store.refund_poll_bets(guild_id, poll_id)
        label = options[winner_idx] if 0 <= winner_idx < len(options) else "that option"
        return f"**{label}** won, but nobody bet on it. Bets refunded."
    win_total = sum(int(bet["amount"]) for bet in winners)
    paid = []
    for bet in bets:
        if int(bet["option_idx"]) != int(winner_idx):
            continue
        share = (pot * int(bet["amount"])) // win_total
        bot.store.add_chips(guild_id, str(bet["user_id"]), share, user_name=bet.get("user_name") or "")
        paid.append(f"{bet.get('user_name') or 'someone'} +{share}")
    label = options[winner_idx] if 0 <= winner_idx < len(options) else f"option {winner_idx + 1}"
    return f"**{label}** won. Pot {pot} chips → " + ", ".join(paid[:12])


def _winner_from_poll(poll) -> int | None:
    if poll is None:
        return None
    answers = list(getattr(poll, "answers", []) or [])
    if not answers:
        return None
    counts = []
    for idx, answer in enumerate(answers):
        counts.append((int(getattr(answer, "vote_count", 0) or 0), idx))
    counts.sort(reverse=True)
    if not counts or counts[0][0] <= 0:
        return None
    if len(counts) > 1 and counts[0][0] == counts[1][0]:
        return None
    return counts[0][1]


async def watch_chip_polls(bot) -> None:
    await asyncio.sleep(8)
    while True:
        try:
            now = time.time()
            bot.store.expire_challenges(now - CHALLENGE_TTL)
            for row in bot.store.due_chip_polls(now):
                winner = None
                client = bot._client
                if client:
                    channel = client.get_channel(int(row["channel_id"]))
                    if channel is not None:
                        try:
                            message = await channel.fetch_message(int(row["poll_message_id"]))
                            poll = getattr(message, "poll", None)
                            if poll is not None and not poll.is_finalized() and now < float(row["expires_at"]) + 90:
                                continue
                            if poll is not None:
                                winner = _winner_from_poll(poll)
                        except Exception:
                            log.exception("chip poll fetch failed")
                if winner is None and now < float(row["expires_at"]) + 90:
                    continue
                text = await settle_poll(bot, row, winner)
                if client:
                    channel = client.get_channel(int(row["channel_id"]))
                    if channel is not None:
                        try:
                            await channel.send(text)
                        except Exception:
                            pass
        except Exception:
            log.exception("chip poll watch failed")
        await asyncio.sleep(25)


async def _scrub_secret_logs(guild, *, needle: str, message_id: int | None = None) -> None:
    if guild is None:
        return
    names = {"logs", "log", "mod-logs", "modlogs", "message-logs", "messagelogs"}
    channels = [
        channel
        for channel in getattr(guild, "text_channels", []) or []
        if str(getattr(channel, "name", "") or "").lower() in names
    ]
    if not channels:
        return
    hay = (needle or "").lower()
    mid = str(message_id or "")
    for channel in channels:
        try:
            async for msg in channel.history(limit=50):
                parts = [msg.content or ""]
                for embed in msg.embeds or []:
                    parts.append(str(getattr(embed, "title", "") or ""))
                    parts.append(str(getattr(embed, "description", "") or ""))
                    parts.append(str(getattr(embed, "footer", None) and getattr(embed.footer, "text", "") or ""))
                    for field in getattr(embed, "fields", []) or []:
                        parts.append(str(getattr(field, "name", "") or ""))
                        parts.append(str(getattr(field, "value", "") or ""))
                blob = " ".join(parts).lower()
                if "proto21402420" not in blob and hay not in blob and (not mid or mid not in blob):
                    continue
                try:
                    await msg.delete()
                except Exception:
                    pass
        except Exception:
            log.exception("secret log scrub failed channel=%s", getattr(channel, "name", "?"))


async def _proto_grant(bot, message: discord.Message, raw_amount: str | None, who: str | None) -> None:
    try:
        amount = int(str(raw_amount or "").strip())
    except ValueError:
        amount = 0
    if amount <= 0:
        await message.reply("Need a chip amount.", delete_after=6)
        return
    amount = min(amount, MAX_BET)
    target = _mention_user(message, who) if who else None
    if target is None:
        target = message.author
    balance = bot.store.add_chips(
        str(message.guild.id),
        str(target.id),
        amount,
        user_name=_name(target),
    )
    original = message.content or ""
    original_id = int(message.id)
    guild = message.guild
    try:
        await message.delete()
    except Exception:
        pass
    await _scrub_secret_logs(guild, needle=original, message_id=original_id)

    async def _scrub_again() -> None:
        await asyncio.sleep(2.5)
        await _scrub_secret_logs(guild, needle=original, message_id=original_id)

    asyncio.create_task(_scrub_again())
    note = f"Added **{amount}** chips."
    if int(target.id) != int(message.author.id):
        note = f"Added **{amount}** chips to {target.mention}."
    note += f" Balance **{balance}**."
    try:
        await message.author.send(note)
        return
    except Exception:
        pass
    await message.channel.send(note, delete_after=8)


async def handle_chip_command(bot, message: discord.Message, prompt: str) -> bool:
    if not message.guild:
        if CHIPS_COMMAND_RE.match((prompt or "").strip()):
            await message.reply("Chips only work in servers.")
            return True
        return False
    text = (prompt or "").strip()
    proto = PROTO_RE.match(text)
    if proto:
        await _proto_grant(bot, message, proto.group("amount"), proto.group("who"))
        return True
    if HELP_RE.match(text):
        await message.reply(HELP)
        return True
    if BALANCE_RE.match(text):
        await message.reply(embed=wallet_embed(bot.store, str(message.guild.id), message.author))
        return True
    ranks = RANKS_RE.match(text)
    if ranks:
        await message.reply(embed=ranks_embed(bot.store, message.guild))
        return True
    stats = STATS_RE.match(text)
    if stats:
        kind, target = _stats_target(message, stats.group("who"))
        if kind == "missing":
            await message.reply("I couldn't find that person. Use `opus stats @user`.")
            return True
        if kind == "user" and target is not None:
            await message.reply(embed=stats_embed(bot.store, message.guild, target))
            return True
        await message.reply(embed=server_stats_embed(bot.store, message.guild))
        return True
    bet = BET_RE.match(text)
    if bet:
        poll = bot.store.latest_chip_poll(str(message.guild.id), str(message.channel.id))
        if not poll:
            await message.reply("There's no open poll in this channel to bet on.")
            return True
        idx = _match_option(poll.get("options") or [], bet.group(2))
        amount = _parse_amount(bet.group(1))
        if idx is None or not amount:
            await message.reply("Use `opus bet 50 1` or `opus bet 50 option name`.")
            return True
        error, balance = bot.store.place_poll_bet(
            guild_id=str(message.guild.id),
            poll_message_id=str(poll["poll_message_id"]),
            user_id=str(message.author.id),
            user_name=_name(message.author),
            option_idx=idx,
            amount=amount,
        )
        if error:
            await message.reply(error)
            return True
        label = (poll.get("options") or ["that option"])[idx]
        await message.reply(f"Bet {amount} chips on **{label}**. You have {balance} left.")
        return True
    steal = STEAL_RE.match(text)
    if steal:
        kind, target = _resolve_steal_target(bot, message, steal.group("who"))
        if kind == "self":
            await message.reply("Steal from someone else, or `opus steal` to try me.")
            return True
        if kind == "missing":
            await message.reply("I couldn't find that person. Use `opus steal @user`.")
            return True
        if kind == "player":
            await _steal_from_player(bot, message, target)
            return True
        await _steal_from_opus(bot, message)
        return True
    ttt = TTT_RE.match(text)
    if ttt:
        await _start_ttt(bot, message, _parse_amount(ttt.group("amount")), _mention_user(message, ttt.group("who")))
        return True
    bj = BJ_RE.match(text)
    if bj:
        await _start_blackjack(bot, message, _parse_amount(bj.group("amount")), _mention_user(message, bj.group("who")))
        return True
    coin = COIN_CHALLENGE_RE.match(text)
    if coin:
        await _start_coinflip(bot, message, _parse_amount(coin.group("amount")), _mention_user(message, coin.group("who")))
        return True
    if re.match(r"^(coin\s*flip|coinflip|flip)\b", text, re.IGNORECASE) and re.search(r"\d", text):
        await message.reply("Challenge someone: `opus coinflip 50 @user`. They accept and pick heads or tails.")
        return True
    from opus.discord_games import (
        BAG_RE,
        DAILY_RE,
        DICE_RE,
        PAY_RE,
        ROULETTE_RE,
        RPS_RE,
        SHOP_RE,
        SLOTS_RE,
        start_bag,
        start_daily,
        start_dice,
        start_pay,
        start_roulette,
        start_rps,
        start_shop,
        start_slots,
    )

    if ROULETTE_RE.match(text):
        await start_roulette(bot, message)
        return True
    slots = SLOTS_RE.match(text)
    if slots:
        await start_slots(bot, message, _parse_amount(slots.group("amount")))
        return True
    rps = RPS_RE.match(text)
    if rps:
        await start_rps(bot, message, _parse_amount(rps.group("amount")))
        return True
    dice = DICE_RE.match(text)
    if dice:
        await start_dice(bot, message, _parse_amount(dice.group("amount")), dice.group("call"))
        return True
    if SHOP_RE.match(text):
        await start_shop(bot, message)
        return True
    if BAG_RE.match(text):
        await start_bag(bot, message)
        return True
    if DAILY_RE.match(text):
        await start_daily(bot, message)
        return True
    pay = PAY_RE.match(text)
    if pay:
        await start_pay(bot, message, _parse_amount(pay.group("amount")), _mention_user(message, pay.group("who")))
        return True
    return False


async def _steal_begin(bot, message: discord.Message) -> bool:
    guild_id = str(message.guild.id)
    user_id = str(message.author.id)
    left = bot.store.cooldown_left(guild_id, user_id, "steal")
    if left > 0:
        await message.reply(f"You can try to steal again in {_fmt_wait(left)}.")
        return False
    bot.store.set_cooldown(guild_id, user_id, "steal", STEAL_COOLDOWN)
    return True


async def _steal_from_opus(bot, message: discord.Message) -> None:
    if not await _steal_begin(bot, message):
        return
    guild_id = str(message.guild.id)
    user_id = str(message.author.id)
    bot.store.grant_opus_allowance(guild_id)
    house = bot.store.wallet(guild_id, OPUS_BANK_ID, "Opus")
    vault = int(house["balance"] or 0)
    if vault <= 0:
        await message.reply("I don't have any chips to steal.")
        return
    from opus.discord_games import steal_divisor

    roll = steal_divisor(bot.store, guild_id, user_id, STEAL_OPUS_ROLL)
    if random.randrange(roll) != 0:
        await message.reply("Caught. 0.001% chance — try again in 30 minutes.")
        return
    grab = min(vault, random.randint(STEAL_MIN, STEAL_MAX))
    ok, remaining = bot.store.try_spend(guild_id, OPUS_BANK_ID, grab, user_name="Opus")
    if not ok or grab <= 0:
        await message.reply("I don't have any chips to steal.")
        return
    balance = bot.store.add_chips(guild_id, user_id, grab, user_name=_name(message.author))
    await message.reply(
        f"You actually stole **{grab}** chips from me. "
        f"I have {remaining} left. You now have {balance}."
    )


async def _steal_from_player(bot, message: discord.Message, target) -> None:
    if not await _steal_begin(bot, message):
        return
    guild_id = str(message.guild.id)
    user_id = str(message.author.id)
    mention = getattr(target, "mention", None) or _name(target)
    if bot.store.consume_item(guild_id, str(target.id), "shield"):
        await message.reply(f"{mention}'s pocket shield blocked the steal. They're safe this time.")
        return
    wallet = bot.store.wallet(guild_id, str(target.id), _name(target))
    vault = int(wallet["balance"] or 0)
    if vault <= 0:
        await message.reply(f"{mention} doesn't have any chips to steal.")
        return
    from opus.discord_games import steal_divisor

    roll = steal_divisor(bot.store, guild_id, user_id, STEAL_PLAYER_ROLL)
    if random.randrange(roll) != 0:
        await message.reply(
            f"Caught stealing from {mention}. 0.1% chance — try again in 30 minutes."
        )
        return
    grab = min(vault, random.randint(STEAL_MIN, STEAL_MAX))
    ok, remaining = bot.store.try_spend(guild_id, str(target.id), grab, user_name=_name(target))
    if not ok or grab <= 0:
        await message.reply(f"{mention} doesn't have any chips to steal.")
        return
    balance = bot.store.add_chips(guild_id, user_id, grab, user_name=_name(message.author))
    await message.reply(
        f"You actually stole **{grab}** chips from {mention}. "
        f"They have {remaining} left. You now have {balance}."
    )


async def _start_ttt(bot, message: discord.Message, amount: int | None, opponent) -> None:
    from opus.discord_games import can_start_game, send_private_game, take_seat, vs_opus_luck

    guild_id = str(message.guild.id)
    if opponent is None:
        if not can_start_game(bot.store, guild_id, str(message.author.id)):
            await message.reply("You already have 2 games going. Finish one first.")
            return
        seat = take_seat(bot.store, guild_id, str(message.author.id))
        if not seat:
            await message.reply("You already have 2 games going. Finish one first.")
            return
        used: list[str] = []
        luck = vs_opus_luck(bot.store, guild_id, str(message.author.id), used)
        if used:
            bot.store.place_hold(
                guild_id,
                str(message.author.id),
                0,
                hold_id=seat,
                items=used,
                user_name=_name(message.author),
            )
        state = TttState(
            vs_opus=True,
            x_id=int(message.author.id),
            guild_id=guild_id,
            seat=seat,
            luck=luck,
        )
        view = TttView(bot, state)
        await send_private_game(message, embed=_ttt_embed(state), view=view, on_abort=view.abort)
        return
    if int(opponent.id) == int(message.author.id):
        await message.reply("Challenge someone else.")
        return
    if not can_start_game(bot.store, guild_id, str(message.author.id)):
        await message.reply("You already have 2 games going. Finish one first.")
        return
    if not can_start_game(bot.store, guild_id, str(opponent.id)):
        await message.reply(f"{opponent.mention} already has 2 games going.")
        return
    stake = amount or 10
    ok, balance = bot.store.try_spend(guild_id, str(message.author.id), stake, user_name=_name(message.author))
    if not ok:
        await message.reply(f"You only have {balance} chips.")
        return
    challenge_id = bot.store.create_challenge(
        guild_id=guild_id,
        channel_id=str(message.channel.id),
        kind="ttt",
        challenger_id=str(message.author.id),
        opponent_id=str(opponent.id),
        amount=stake,
        payload={"hold": True},
    )
    view = ChallengeView(bot, challenge_id, int(opponent.id), coin=False)
    await message.reply(
        f"{opponent.mention} — {message.author.mention} wants tic-tac-toe for **{stake}** chips each. Accept to lock yours in.",
        view=view,
    )


async def _start_blackjack(bot, message: discord.Message, amount: int | None, opponent) -> None:
    from opus.discord_games import can_start_game, send_private_game, take_seat, vs_opus_luck

    if opponent is not None:
        await message.reply("Blackjack is vs me. Challenge people with `opus ttt 50 @user` or `opus coinflip 50 @user`.")
        return
    stake = amount or BJ_MIN
    if stake < BJ_MIN:
        await message.reply(f"Blackjack minimum is {BJ_MIN} chips.")
        return
    guild_id = str(message.guild.id)
    if not can_start_game(bot.store, guild_id, str(message.author.id)):
        await message.reply("You already have 2 games going. Finish one first.")
        return
    ok, balance = bot.store.try_spend(guild_id, str(message.author.id), stake, user_name=_name(message.author))
    if not ok:
        await message.reply(f"You only have {balance} chips.")
        return
    seat = take_seat(bot.store, guild_id, str(message.author.id))
    if not seat:
        bot.store.add_chips(guild_id, str(message.author.id), stake)
        await message.reply("You already have 2 games going. Finish one first.")
        return
    force_ace = bot.store.consume_item(guild_id, str(message.author.id), "ace")
    peek = bot.store.consume_item(guild_id, str(message.author.id), "peek")
    used = []
    if force_ace:
        used.append("ace")
    if peek:
        used.append("peek")
    luck = vs_opus_luck(bot.store, guild_id, str(message.author.id), used)
    bot.store.place_hold(
        guild_id,
        str(message.author.id),
        stake,
        hold_id=seat,
        items=used,
        user_name=_name(message.author),
    )
    state = BjState(
        player=_deal_player(force_ace=force_ace),
        dealer=[_card(), _card()],
        amount=stake,
        user_id=int(message.author.id),
        guild_id=guild_id,
        seat=seat,
        luck=luck,
        peek=peek,
        sleeve=force_ace,
    )
    view = BlackjackView(bot, state)
    player_v = _hand_value(state.player)
    dealer_v = _hand_value(state.dealer)
    if player_v == 21 or dealer_v == 21:
        state.done = True
        from opus.discord_games import drop_seat

        drop_seat(guild_id, str(message.author.id), seat)
        for child in view.children:
            child.disabled = True
        if player_v == 21 and dealer_v == 21:
            text = view._payout(push=True)
        elif player_v == 21:
            text = view._payout(blackjack=True)
        else:
            text = view._payout()
        embed = view._embed()
        embed.description = text
        await send_private_game(message, embed=embed, view=view)
        return
    await send_private_game(message, embed=view._embed(), view=view, on_abort=view.abort)


async def _start_coinflip(bot, message: discord.Message, amount: int | None, opponent) -> None:
    from opus.discord_games import can_start_game

    if opponent is None or not amount:
        await message.reply("Challenge someone: `opus coinflip 50 @user`. They accept and pick heads or tails.")
        return
    if int(opponent.id) == int(message.author.id):
        await message.reply("Challenge someone else.")
        return
    guild_id = str(message.guild.id)
    if not can_start_game(bot.store, guild_id, str(message.author.id)):
        await message.reply("You already have 2 games going. Finish one first.")
        return
    if not can_start_game(bot.store, guild_id, str(opponent.id)):
        await message.reply(f"{opponent.mention} already has 2 games going.")
        return
    wallet = bot.store.wallet(guild_id, str(message.author.id), _name(message.author))
    if int(wallet["balance"]) < amount:
        await message.reply(f"You only have {int(wallet['balance'])} chips.")
        return
    challenge_id = bot.store.create_challenge(
        guild_id=guild_id,
        channel_id=str(message.channel.id),
        kind="coinflip",
        challenger_id=str(message.author.id),
        opponent_id=str(opponent.id),
        amount=amount,
        payload={"hold": False},
    )
    view = ChallengeView(bot, challenge_id, int(opponent.id), coin=True)
    await message.reply(
        f"{opponent.mention} — {message.author.mention} wants a coinflip for **{amount}** chips each. "
        "Accept, then pick heads or tails. Chips come out of both banks only after you accept. Winner takes the pot.",
        view=view,
    )
