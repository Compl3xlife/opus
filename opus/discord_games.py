"""Private vs-Opus games, public roulette vs Opus, shop, daily, and pay."""
from __future__ import annotations

import asyncio
import random
import re
import time
import uuid
from dataclasses import dataclass, field

import discord

from opus.discord_store import MAX_BET, OPUS_BANK_ID
from opus.logutil import get_logger

log = get_logger()

CHIP_COLOR = 0xC4A574
MAX_GAMES = 2
SEAT_TTL = 8 * 60
DAILY_CHIPS = 40
DAILY_WAIT = 20 * 60 * 60
ROULETTE_WAIT = 40
RED_NUMBERS = frozenset({1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36})
SLOT_SYMBOLS = ("🍒", "🍋", "⭐", "💎", "7️⃣")
SLOT_WEIGHTS = (32, 28, 20, 14, 6)

SHOP_ITEMS = (
    {
        "id": "charm",
        "name": "Lucky Charm",
        "price": 400,
        "desc": "Your next steal is 8× more likely.",
    },
    {
        "id": "clover",
        "name": "Four-leaf Clover",
        "price": 750,
        "desc": "15 minutes of extra luck in vs-Opus games.",
        "seconds": 15 * 60,
    },
    {
        "id": "loaded",
        "name": "Loaded Dice",
        "price": 900,
        "desc": "Next 5 games vs me tilt slightly your way.",
        "charges": 5,
    },
    {
        "id": "shield",
        "name": "Pocket Shield",
        "price": 500,
        "desc": "The next steal against you fails.",
    },
    {
        "id": "payout",
        "name": "House Cut",
        "price": 1200,
        "desc": "Your next win vs me pays +25%.",
    },
    {
        "id": "coffee",
        "name": "Coffee",
        "price": 150,
        "desc": "Your next daily claim is +50 chips.",
    },
    {
        "id": "ace",
        "name": "Ace up your sleeve",
        "price": 10000,
        "desc": "Your next blackjack hand is guaranteed at least one ace.",
    },
    {
        "id": "peek",
        "name": "Dealer's Peek",
        "price": 2500,
        "desc": "See my hole card on your next blackjack.",
    },
    {
        "id": "mulligan",
        "name": "Mulligan",
        "price": 4500,
        "desc": "Your next loss vs me refunds the stake.",
    },
    {
        "id": "seven",
        "name": "Lucky Seven",
        "price": 6500,
        "desc": "Your next slots pull has at least one 7️⃣.",
    },
)
SHOP_BY_ID = {item["id"]: item for item in SHOP_ITEMS}

ROULETTE_RE = re.compile(r"^roulette(\s+(?P<amount>\d+))?$", re.IGNORECASE)
SLOTS_RE = re.compile(r"^(slots?|spin)(?:\s+(?P<amount>\d+))?$", re.IGNORECASE)
RPS_RE = re.compile(r"^(rps|rock\s*paper\s*scissors)(?:\s+(?P<amount>\d+))?$", re.IGNORECASE)
DICE_RE = re.compile(
    r"^dice(?:\s+(?P<amount>\d+))?(?:\s+(?P<call>over|under|seven|7))?$",
    re.IGNORECASE,
)
SHOP_RE = re.compile(r"^(shop|store|buy)$", re.IGNORECASE)
BAG_RE = re.compile(r"^(bag|inventory|items)$", re.IGNORECASE)
DAILY_RE = re.compile(r"^(daily|claim|bonus)$", re.IGNORECASE)
PAY_RE = re.compile(r"^(pay|give|send|tip)\s+(?P<amount>\d+)(?:\s+(?P<who>.+))?$", re.IGNORECASE)

_seats: dict[tuple[str, str], dict[str, float]] = {}
_roulette: dict[tuple[str, str], RouletteTable] = {}


def _name(user) -> str:
    return str(getattr(user, "display_name", None) or getattr(user, "name", None) or "someone")


def _parse_amount(raw) -> int | None:
    if raw is None or str(raw).strip() == "":
        return None
    try:
        value = int(str(raw).strip())
    except ValueError:
        return None
    return max(1, min(MAX_BET, value))


def prune_seats(store=None) -> None:
    now = time.time()
    for key, tokens in list(_seats.items()):
        for token, started in list(tokens.items()):
            if now - started > SEAT_TTL:
                tokens.pop(token, None)
        if not tokens:
            _seats.pop(key, None)
    for key, table in list(_roulette.items()):
        if table.spinning:
            continue
        if now < table.closes_at + 30:
            continue
        for bet in list(table.bets.values()):
            drop_seat(table.guild_id, bet.user_id, bet.seat)
            if store is not None:
                try:
                    store.release_hold(bet.seat, refund=True)
                except Exception:
                    pass
        _roulette.pop(key, None)
    if store is not None:
        try:
            store.expire_challenges(now - 180)
        except Exception:
            log.exception("expire stale chip games failed")


def take_seat(store, guild_id: str, user_id: str) -> str | None:
    prune_seats(store)
    gid, uid = str(guild_id), str(user_id)
    used = len(_seats.get((gid, uid), {})) + store.open_challenge_count(gid, uid)
    if used >= MAX_GAMES:
        return None
    token = uuid.uuid4().hex
    _seats.setdefault((gid, uid), {})[token] = time.time()
    return token


def drop_seat(guild_id: str, user_id: str, token: str | None) -> None:
    if not token:
        return
    key = (str(guild_id), str(user_id))
    slots = _seats.get(key)
    if not slots:
        return
    slots.pop(token, None)
    if not slots:
        _seats.pop(key, None)


def close_seat(store, guild_id: str, user_id: str, token: str | None, *, refund: bool = False) -> bool:
    drop_seat(guild_id, user_id, token)
    if store is None or not token:
        return False
    return store.release_hold(token, refund=refund) is not None


def can_start_game(store, guild_id: str, user_id: str) -> bool:
    prune_seats(store)
    gid, uid = str(guild_id), str(user_id)
    return len(_seats.get((gid, uid), {})) + store.open_challenge_count(gid, uid) < MAX_GAMES


def vs_opus_luck(store, guild_id: str, user_id: str, used: list[str] | None = None) -> float:
    if store.item_qty(guild_id, user_id, "clover") > 0:
        return 0.22
    if store.consume_item(guild_id, user_id, "loaded"):
        if used is not None:
            used.append("loaded")
        return 0.18
    return 0.0


def payout_bonus(store, guild_id: str, user_id: str, amount: int) -> int:
    extra = 0
    if store.consume_item(guild_id, user_id, "payout"):
        extra = max(1, amount // 4)
    return extra


def steal_divisor(store, guild_id: str, user_id: str, base: int) -> int:
    if store.consume_item(guild_id, user_id, "charm"):
        return max(1, base // 8)
    if store.item_qty(guild_id, user_id, "clover") > 0:
        return max(1, base // 2)
    return base


def try_mulligan(store, guild_id: str, user_id: str, amount: int) -> bool:
    if amount <= 0 or not store.consume_item(guild_id, user_id, "mulligan"):
        return False
    store.add_chips(guild_id, user_id, int(amount))
    return True


class RevealView(discord.ui.View):
    def __init__(self, user_id: int, content: str | None, embed, game_view, on_abort):
        super().__init__(timeout=90)
        self._user_id = int(user_id)
        self._content = content
        self._embed = embed
        self._game_view = game_view
        self._on_abort = on_abort
        self._opened = False

    @discord.ui.button(label="Open private game", style=discord.ButtonStyle.primary, emoji="🔒")
    async def open_game(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if int(interaction.user.id) != self._user_id:
            await interaction.response.send_message("That's not your game.", ephemeral=True)
            return
        if self._opened:
            await interaction.response.send_message("Already opened.", ephemeral=True)
            return
        self._opened = True
        for child in self.children:
            child.disabled = True
        await interaction.response.send_message(
            content=self._content or None,
            embed=self._embed,
            view=self._game_view,
            ephemeral=True,
        )
        try:
            await interaction.message.edit(content="Private game opened.", view=self)
        except Exception:
            pass
        self.stop()

    async def on_timeout(self) -> None:
        if not self._opened:
            try:
                self._on_abort()
            except Exception:
                log.exception("private game abort failed")


async def send_private_game(message: discord.Message, *, embed, view=None, content: str | None = None, on_abort=None) -> None:
    try:
        await message.author.send(content=content, embed=embed, view=view)
        try:
            await message.add_reaction("🔒")
        except Exception:
            pass
        return
    except Exception:
        log.info("private game dm failed user=%s", getattr(message.author, "id", "?"))
    if view is None:
        try:
            await message.reply(embed=embed, delete_after=20)
        except Exception:
            pass
        return
    reveal = RevealView(int(message.author.id), content, embed, view, on_abort or (lambda: None))
    await message.reply("Only you can see this game. Click to open it.", view=reveal, delete_after=90)


def shop_embed(store, guild_id: str, user) -> discord.Embed:
    embed = discord.Embed(title="Chip shop", color=0x38BDF8)
    wallet = store.wallet(guild_id, str(user.id), _name(user))
    embed.description = (
        f"You have **{int(wallet['balance'])}** chips. Luck items help vs me "
        "(including the public roulette table) and steal — not player-vs-player pots."
    )
    for item in SHOP_ITEMS:
        have = store.item_qty(guild_id, str(user.id), item["id"])
        owned = f" · owned {have}" if have else ""
        embed.add_field(
            name=f"{item['name']} — {item['price']} chips",
            value=item["desc"] + owned,
            inline=False,
        )
    return embed


def bag_embed(store, guild_id: str, user) -> discord.Embed:
    embed = discord.Embed(title="Inventory", color=0x38BDF8)
    rows = store.inventory(guild_id, str(user.id))
    if not rows:
        embed.description = "Empty. `opus shop` to buy luck items."
        return embed
    lines = []
    now = time.time()
    for row in rows:
        item = SHOP_BY_ID.get(str(row["item_id"]))
        if not item:
            continue
        qty = int(row["qty"] or 0)
        expires = float(row["expires_at"] or 0)
        extra = ""
        if expires > now:
            extra = f" · {max(1, int((expires - now) / 60))}m left"
        elif expires:
            continue
        lines.append(f"**{item['name']}** ×{qty}{extra}\n{item['desc']}")
    embed.description = "\n\n".join(lines) or "Empty."
    return embed


class ShopView(discord.ui.View):
    def __init__(self, bot, guild_id: str, user_id: int):
        super().__init__(timeout=180)
        self._bot = bot
        self._guild_id = str(guild_id)
        self._user_id = int(user_id)
        options = [
            discord.SelectOption(
                label=f"{item['name']} ({item['price']})",
                value=item["id"],
                description=item["desc"][:100],
            )
            for item in SHOP_ITEMS
        ]
        select = discord.ui.Select(placeholder="Buy an item", options=options)
        select.callback = self._buy
        self.add_item(select)

    async def _buy(self, interaction: discord.Interaction) -> None:
        if int(interaction.user.id) != self._user_id:
            await interaction.response.send_message("This shop isn't yours.", ephemeral=True)
            return
        item_id = ""
        data = getattr(interaction, "data", None) or {}
        values = data.get("values") if isinstance(data, dict) else None
        if values:
            item_id = str(values[0])
        item = SHOP_BY_ID.get(item_id)
        if not item:
            await interaction.response.send_message("Unknown item.", ephemeral=True)
            return
        ok, balance = self._bot.store.try_spend(
            self._guild_id,
            str(interaction.user.id),
            int(item["price"]),
            user_name=_name(interaction.user),
        )
        if not ok:
            await interaction.response.send_message(f"You only have {balance} chips.", ephemeral=True)
            return
        expires = time.time() + int(item["seconds"]) if item.get("seconds") else 0.0
        qty = int(item.get("charges") or 1)
        self._bot.store.add_item(self._guild_id, str(interaction.user.id), item_id, qty=qty, expires_at=expires)
        self._bot.store.add_chips(self._guild_id, OPUS_BANK_ID, int(item["price"]), user_name="Opus")
        await interaction.response.send_message(
            f"Bought **{item['name']}**. You have {balance} chips left.",
            ephemeral=True,
        )


@dataclass
class RouletteBet:
    user_id: str
    user_name: str
    mention: str
    kind: str
    value: str
    amount: int
    seat: str | None = None


@dataclass
class RouletteTable:
    guild_id: str
    channel_id: str
    bets: dict[str, RouletteBet] = field(default_factory=dict)
    closes_at: float = 0.0
    spinning: bool = False
    message: discord.Message | None = None
    task: asyncio.Task | None = None


def _wheel_color(number: int) -> str:
    if number == 0:
        return "green"
    return "red" if number in RED_NUMBERS else "black"


def _bet_label(bet: RouletteBet) -> str:
    if bet.kind == "number":
        return f"#{bet.value}"
    return bet.value


def _roulette_payout(bet: RouletteBet, number: int) -> int:
    color = _wheel_color(number)
    if bet.kind == "number":
        return bet.amount * 36 if int(bet.value) == number else 0
    if bet.kind == "color":
        if bet.value == color:
            return bet.amount * 2 if color != "green" else bet.amount * 36
        return 0
    if bet.kind == "parity":
        if number == 0:
            return 0
        even = number % 2 == 0
        if (bet.value == "even" and even) or (bet.value == "odd" and not even):
            return bet.amount * 2
        return 0
    if bet.kind == "range":
        if number == 0:
            return 0
        low = number <= 18
        if (bet.value == "low" and low) or (bet.value == "high" and not low):
            return bet.amount * 2
        return 0
    return 0


def _roulette_embed(table: RouletteTable, *, result: int | None = None, paid: str = "") -> discord.Embed:
    left = max(0, int(table.closes_at - time.time()))
    if result is None:
        embed = discord.Embed(title="Roulette vs Opus", color=0xE11D48)
        embed.description = (
            f"You're betting against me. Public table — anyone can join this spin.\n"
            f"European wheel · **0–36**. Bets close in **{left}s**.\n"
            "Red/black, even/odd, low (1–18) / high (19–36) pay **1:1**. A single number pays **35:1**.\n"
            "Luck items apply to your own bet only. The ball is the same for everyone."
        )
    else:
        color = _wheel_color(result)
        hue = 0x22C55E if color == "green" else 0xE11D48 if color == "red" else 0x111827
        embed = discord.Embed(title="Roulette vs Opus", color=hue)
        embed.description = f"The ball landed on **{result} {color}**."
        if paid:
            embed.description += f"\n{paid}"
    if table.bets:
        lines = [
            f"{bet.mention} — {_bet_label(bet)} · {bet.amount}"
            for bet in table.bets.values()
        ]
        embed.add_field(name="Bets", value="\n".join(lines)[:1024], inline=False)
    else:
        embed.add_field(name="Bets", value="Nobody has bet yet.", inline=False)
    return embed


class RouletteNumberModal(discord.ui.Modal, title="Bet a number"):
    number = discord.ui.TextInput(label="Number (0–36)", placeholder="17", min_length=1, max_length=2)
    amount = discord.ui.TextInput(label="Chips", placeholder="25", min_length=1, max_length=9)

    def __init__(self, view: RouletteView):
        super().__init__()
        self._view = view

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            number = int(str(self.number.value).strip())
        except ValueError:
            await interaction.response.send_message("Pick a number from 0 to 36.", ephemeral=True)
            return
        if number < 0 or number > 36:
            await interaction.response.send_message("Pick a number from 0 to 36.", ephemeral=True)
            return
        stake = _parse_amount(str(self.amount.value))
        if not stake:
            await interaction.response.send_message("Enter a whole number of chips.", ephemeral=True)
            return
        await self._view._place(interaction, "number", str(number), stake)


class RouletteView(discord.ui.View):
    def __init__(self, bot, table: RouletteTable):
        super().__init__(timeout=ROULETTE_WAIT + 20)
        self._bot = bot
        self._table = table

    async def _place(self, interaction: discord.Interaction, kind: str, value: str, amount: int) -> None:
        table = self._table
        if table.spinning or time.time() >= table.closes_at:
            await interaction.response.send_message("Bets are closed.", ephemeral=True)
            return
        uid = str(interaction.user.id)
        guild_id = table.guild_id
        existing = table.bets.get(uid)
        if existing:
            await interaction.response.send_message("You already have a bet on this spin.", ephemeral=True)
            return
        if not can_start_game(self._bot.store, guild_id, uid):
            await interaction.response.send_message("You already have 2 games going.", ephemeral=True)
            return
        ok, balance = self._bot.store.try_spend(guild_id, uid, amount, user_name=_name(interaction.user))
        if not ok:
            await interaction.response.send_message(f"You only have {balance} chips.", ephemeral=True)
            return
        seat = take_seat(self._bot.store, guild_id, uid)
        if not seat:
            self._bot.store.add_chips(guild_id, uid, amount)
            await interaction.response.send_message("You already have 2 games going.", ephemeral=True)
            return
        table.bets[uid] = RouletteBet(
            user_id=uid,
            user_name=_name(interaction.user),
            mention=interaction.user.mention,
            kind=kind,
            value=value,
            amount=amount,
            seat=seat,
        )
        self._bot.store.place_hold(
            guild_id,
            uid,
            amount,
            hold_id=seat,
            user_name=_name(interaction.user),
        )
        await interaction.response.send_message(
            f"Bet **{amount}** on **{_bet_label(table.bets[uid])}**. {balance} chips left.",
            ephemeral=True,
        )
        if table.message:
            try:
                await table.message.edit(embed=_roulette_embed(table), view=self)
            except Exception:
                pass

    async def _bet_color(self, interaction: discord.Interaction, color: str) -> None:
        await interaction.response.send_modal(RouletteAmountModal(self, "color", color))

    @discord.ui.button(label="Red", style=discord.ButtonStyle.danger, row=0)
    async def red(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._bet_color(interaction, "red")

    @discord.ui.button(label="Black", style=discord.ButtonStyle.secondary, row=0)
    async def black(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._bet_color(interaction, "black")

    @discord.ui.button(label="Green 0", style=discord.ButtonStyle.success, row=0)
    async def green(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._bet_color(interaction, "green")

    @discord.ui.button(label="Number", style=discord.ButtonStyle.primary, row=0)
    async def number(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(RouletteNumberModal(self))

    @discord.ui.button(label="Even", style=discord.ButtonStyle.secondary, row=1)
    async def even(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(RouletteAmountModal(self, "parity", "even"))

    @discord.ui.button(label="Odd", style=discord.ButtonStyle.secondary, row=1)
    async def odd(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(RouletteAmountModal(self, "parity", "odd"))

    @discord.ui.button(label="1–18", style=discord.ButtonStyle.secondary, row=1)
    async def low(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(RouletteAmountModal(self, "range", "low"))

    @discord.ui.button(label="19–36", style=discord.ButtonStyle.secondary, row=1)
    async def high(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(RouletteAmountModal(self, "range", "high"))

    @discord.ui.button(label="Spin now", style=discord.ButtonStyle.primary, row=1)
    async def spin_now(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not self._table.bets:
            await interaction.response.send_message("Need at least one bet first.", ephemeral=True)
            return
        await interaction.response.defer()
        await settle_roulette(self._bot, self._table, self)


class RouletteAmountModal(discord.ui.Modal, title="Roulette bet"):
    amount = discord.ui.TextInput(label="Chips", placeholder="25", min_length=1, max_length=9)

    def __init__(self, view: RouletteView, kind: str, value: str):
        super().__init__()
        self._view = view
        self._kind = kind
        self._value = value

    async def on_submit(self, interaction: discord.Interaction) -> None:
        stake = _parse_amount(str(self.amount.value))
        if not stake:
            await interaction.response.send_message("Enter a whole number of chips.", ephemeral=True)
            return
        await self._view._place(interaction, self._kind, self._value, stake)


async def settle_roulette(bot, table: RouletteTable, view: RouletteView) -> None:
    if table.spinning:
        return
    table.spinning = True
    key = (table.guild_id, table.channel_id)
    if table.task and not table.task.done() and table.task is not asyncio.current_task():
        table.task.cancel()
    number = random.randint(0, 36)
    lines = []
    skipped = 0
    for bet in table.bets.values():
        drop_seat(table.guild_id, bet.user_id, bet.seat)
        held = bot.store.release_hold(bet.seat, refund=False)
        if held is None:
            skipped += 1
            continue
        luck = vs_opus_luck(bot.store, table.guild_id, bet.user_id)
        payout = _roulette_payout(bet, number)
        if payout:
            payout += payout_bonus(bot.store, table.guild_id, bet.user_id, payout)
            bot.store.pay_from_bank(table.guild_id, payout)
            bot.store.add_chips(
                table.guild_id,
                bet.user_id,
                payout,
                user_name=bet.user_name,
                played=True,
                won=True,
            )
            lines.append(f"{bet.mention} +{payout}")
        elif try_mulligan(bot.store, table.guild_id, bet.user_id, bet.amount):
            bot.store.record_game(table.guild_id, bet.user_id, won=False, user_name=bet.user_name)
            lines.append(f"{bet.mention} mulligan")
        elif luck and random.random() < luck:
            bot.store.add_chips(
                table.guild_id,
                bet.user_id,
                bet.amount,
                user_name=bet.user_name,
                played=True,
            )
            lines.append(f"{bet.mention} luck push")
        else:
            bot.store.add_chips(table.guild_id, OPUS_BANK_ID, bet.amount, user_name="Opus")
            bot.store.record_game(table.guild_id, bet.user_id, won=False, user_name=bet.user_name)
    paid = ", ".join(lines) if lines else ("Table cancelled — chips returned." if skipped else "I take the table.")
    for child in view.children:
        child.disabled = True
    view.stop()
    embed = _roulette_embed(table, result=number, paid=paid)
    if table.message:
        try:
            await table.message.edit(embed=embed, view=view)
        except Exception:
            pass
    _roulette.pop(key, None)


async def start_roulette(bot, message: discord.Message) -> None:
    key = (str(message.guild.id), str(message.channel.id))
    existing = _roulette.get(key)
    if existing and not existing.spinning and time.time() < existing.closes_at:
        await message.reply("A roulette table is already open in this channel. Join that one.")
        return
    table = RouletteTable(
        guild_id=str(message.guild.id),
        channel_id=str(message.channel.id),
        closes_at=time.time() + ROULETTE_WAIT,
    )
    view = RouletteView(bot, table)
    sent = await message.reply(embed=_roulette_embed(table), view=view)
    table.message = sent
    _roulette[key] = table

    async def auto_spin() -> None:
        try:
            await asyncio.sleep(ROULETTE_WAIT)
            if not table.spinning:
                if not table.bets:
                    for child in view.children:
                        child.disabled = True
                    view.stop()
                    try:
                        await sent.edit(content="Roulette closed — no bets.", embed=None, view=view)
                    except Exception:
                        pass
                    _roulette.pop(key, None)
                    return
                await settle_roulette(bot, table, view)
        except asyncio.CancelledError:
            return
        except Exception:
            log.exception("roulette auto spin failed")

    table.task = asyncio.create_task(auto_spin())


class SlotsView(discord.ui.View):
    def __init__(self, bot, guild_id: str, user_id: int, amount: int, seat: str):
        super().__init__(timeout=90)
        self._bot = bot
        self._guild_id = str(guild_id)
        self._user_id = int(user_id)
        self._amount = int(amount)
        self._seat = seat
        self._done = False

    def _spin(self) -> tuple[tuple[str, str, str], str]:
        held = self._bot.store.release_hold(self._seat, refund=False)
        if held is None:
            return ("❌", "❌", "❌"), "This spin was cancelled."
        luck = vs_opus_luck(self._bot.store, self._guild_id, str(self._user_id))
        weights = list(SLOT_WEIGHTS)
        if luck:
            weights[-1] += 8
            weights[-2] += 6
        reels = list(random.choices(SLOT_SYMBOLS, weights=weights, k=3))
        if self._bot.store.consume_item(self._guild_id, str(self._user_id), "seven"):
            if "7️⃣" not in reels:
                reels[random.randint(0, 2)] = "7️⃣"
        reels = tuple(reels)
        a, b, c = reels
        if a == b == c:
            if a == "7️⃣":
                pay = self._amount * 12
            elif a == "💎":
                pay = self._amount * 8
            else:
                pay = self._amount * 5
            pay += payout_bonus(self._bot.store, self._guild_id, str(self._user_id), pay)
            self._bot.store.pay_from_bank(self._guild_id, pay)
            self._bot.store.add_chips(self._guild_id, str(self._user_id), pay, played=True, won=True)
            text = f"{' '.join(reels)}\nYou win **{pay}** chips."
        elif a == b or b == c or a == c:
            pay = self._amount * 2
            pay += payout_bonus(self._bot.store, self._guild_id, str(self._user_id), pay)
            self._bot.store.pay_from_bank(self._guild_id, pay)
            self._bot.store.add_chips(self._guild_id, str(self._user_id), pay, played=True, won=True)
            text = f"{' '.join(reels)}\nPair. You win **{pay}** chips."
        else:
            self._bot.store.record_game(self._guild_id, str(self._user_id), won=False)
            if try_mulligan(self._bot.store, self._guild_id, str(self._user_id), self._amount):
                text = f"{' '.join(reels)}\nNo match, but Mulligan returns your {self._amount} chips."
            else:
                self._bot.store.add_chips(self._guild_id, OPUS_BANK_ID, self._amount, user_name="Opus")
                text = f"{' '.join(reels)}\nNo match. You lose {self._amount}."
        return reels, text

    def _embed(self, text: str = "") -> discord.Embed:
        embed = discord.Embed(title="Slots", color=0xF59E0B)
        embed.description = text or f"Bet **{self._amount}**. Pull to spin."
        return embed

    def _close(self) -> None:
        self._done = True
        drop_seat(self._guild_id, str(self._user_id), self._seat)
        for child in self.children:
            child.disabled = True
        self.stop()

    def abort(self) -> None:
        if self._done:
            return
        self._done = True
        self._bot.store.release_hold(self._seat, refund=True)
        drop_seat(self._guild_id, str(self._user_id), self._seat)

    @discord.ui.button(label="Spin", style=discord.ButtonStyle.success, emoji="🎰")
    async def spin(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if int(interaction.user.id) != self._user_id or self._done:
            await interaction.response.send_message("Not your machine.", ephemeral=True)
            return
        _reels, text = self._spin()
        self._close()
        await interaction.response.edit_message(embed=self._embed(text), view=self)

    async def on_timeout(self) -> None:
        if not self._done:
            self.abort()


class RpsView(discord.ui.View):
    def __init__(self, bot, guild_id: str, user_id: int, amount: int, seat: str):
        super().__init__(timeout=90)
        self._bot = bot
        self._guild_id = str(guild_id)
        self._user_id = int(user_id)
        self._amount = int(amount)
        self._seat = seat
        self._done = False

    def abort(self) -> None:
        if self._done:
            return
        self._done = True
        self._bot.store.release_hold(self._seat, refund=True)
        drop_seat(self._guild_id, str(self._user_id), self._seat)

    async def _throw(self, interaction: discord.Interaction, pick: str) -> None:
        if int(interaction.user.id) != self._user_id or self._done:
            await interaction.response.send_message("Not your game.", ephemeral=True)
            return
        self._done = True
        held = self._bot.store.release_hold(self._seat, refund=False)
        drop_seat(self._guild_id, str(self._user_id), self._seat)
        if held is None:
            await interaction.response.send_message("This game was cancelled.", ephemeral=True)
            return
        options = ("rock", "paper", "scissors")
        beats = {"rock": "scissors", "paper": "rock", "scissors": "paper"}
        luck = vs_opus_luck(self._bot.store, self._guild_id, str(self._user_id))
        if luck and random.random() < luck + 0.15:
            opus = beats[pick]
        else:
            opus = random.choice(options)
        if pick == opus:
            self._bot.store.add_chips(self._guild_id, str(self._user_id), self._amount, played=True)
            text = f"We both threw **{pick}**. Push. Chips back."
        elif beats[pick] == opus:
            won = self._amount * 2
            won += payout_bonus(self._bot.store, self._guild_id, str(self._user_id), won)
            self._bot.store.pay_from_bank(self._guild_id, won)
            self._bot.store.add_chips(self._guild_id, str(self._user_id), won, played=True, won=True)
            text = f"You threw **{pick}**. I threw **{opus}**. You win **{won}**."
        else:
            self._bot.store.record_game(self._guild_id, str(self._user_id), won=False)
            if try_mulligan(self._bot.store, self._guild_id, str(self._user_id), self._amount):
                text = f"You threw **{pick}**. I threw **{opus}**. Mulligan returns your {self._amount}."
            else:
                self._bot.store.add_chips(self._guild_id, OPUS_BANK_ID, self._amount, user_name="Opus")
                text = f"You threw **{pick}**. I threw **{opus}**. You lose {self._amount}."
        for child in self.children:
            child.disabled = True
        embed = discord.Embed(title="Rock paper scissors", color=CHIP_COLOR, description=text)
        self.stop()
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Rock", style=discord.ButtonStyle.secondary, emoji="🪨")
    async def rock(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._throw(interaction, "rock")

    @discord.ui.button(label="Paper", style=discord.ButtonStyle.primary, emoji="📄")
    async def paper(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._throw(interaction, "paper")

    @discord.ui.button(label="Scissors", style=discord.ButtonStyle.danger, emoji="✂️")
    async def scissors(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._throw(interaction, "scissors")

    async def on_timeout(self) -> None:
        if not self._done:
            self.abort()


class DiceView(discord.ui.View):
    def __init__(self, bot, guild_id: str, user_id: int, amount: int, seat: str, call: str):
        super().__init__(timeout=90)
        self._bot = bot
        self._guild_id = str(guild_id)
        self._user_id = int(user_id)
        self._amount = int(amount)
        self._seat = seat
        self._call = call
        self._done = False

    def abort(self) -> None:
        if self._done:
            return
        self._done = True
        self._bot.store.release_hold(self._seat, refund=True)
        drop_seat(self._guild_id, str(self._user_id), self._seat)

    def _embed(self, text: str | None = None) -> discord.Embed:
        embed = discord.Embed(title="Dice", color=0x22C55E)
        embed.description = text or (
            f"Bet **{self._amount}** on **{self._call}** 7 with 2d6.\n"
            "Over/under pays 1:1. Exact 7 pays 4:1."
        )
        return embed

    @discord.ui.button(label="Roll", style=discord.ButtonStyle.success, emoji="🎲")
    async def roll(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if int(interaction.user.id) != self._user_id or self._done:
            await interaction.response.send_message("Not your dice.", ephemeral=True)
            return
        self._done = True
        held = self._bot.store.release_hold(self._seat, refund=False)
        drop_seat(self._guild_id, str(self._user_id), self._seat)
        if held is None:
            await interaction.response.send_message("This game was cancelled.", ephemeral=True)
            return
        luck = vs_opus_luck(self._bot.store, self._guild_id, str(self._user_id))
        a, b = random.randint(1, 6), random.randint(1, 6)
        total = a + b
        if luck and random.random() < luck:
            if self._call in {"over"} and total <= 7:
                total = min(12, total + 2)
            elif self._call in {"under"} and total >= 7:
                total = max(2, total - 2)
            elif self._call in {"seven", "7"} and total != 7:
                total = 7
        won = 0
        if self._call in {"seven", "7"} and total == 7:
            won = self._amount * 5
        elif self._call == "over" and total > 7:
            won = self._amount * 2
        elif self._call == "under" and total < 7:
            won = self._amount * 2
        if won:
            won += payout_bonus(self._bot.store, self._guild_id, str(self._user_id), won)
            self._bot.store.pay_from_bank(self._guild_id, won)
            self._bot.store.add_chips(self._guild_id, str(self._user_id), won, played=True, won=True)
            text = f"🎲 {a} + {b} = **{total}**. You win **{won}**."
        else:
            self._bot.store.record_game(self._guild_id, str(self._user_id), won=False)
            if try_mulligan(self._bot.store, self._guild_id, str(self._user_id), self._amount):
                text = f"🎲 {a} + {b} = **{total}**. Mulligan returns your {self._amount}."
            else:
                self._bot.store.add_chips(self._guild_id, OPUS_BANK_ID, self._amount, user_name="Opus")
                text = f"🎲 {a} + {b} = **{total}**. You lose {self._amount}."
        for child in self.children:
            child.disabled = True
        self.stop()
        await interaction.response.edit_message(embed=self._embed(text), view=self)

    async def on_timeout(self) -> None:
        if not self._done:
            self.abort()


async def _stake_or_reply(bot, message, amount: int) -> tuple[str, int, str] | None:
    guild_id = str(message.guild.id)
    uid = str(message.author.id)
    if not can_start_game(bot.store, guild_id, uid):
        await message.reply("You already have 2 games going. Finish one first.")
        return None
    ok, balance = bot.store.try_spend(guild_id, uid, amount, user_name=_name(message.author))
    if not ok:
        await message.reply(f"You only have {balance} chips.")
        return None
    seat = take_seat(bot.store, guild_id, uid)
    if not seat:
        bot.store.add_chips(guild_id, uid, amount)
        await message.reply("You already have 2 games going. Finish one first.")
        return None
    bot.store.place_hold(guild_id, uid, amount, hold_id=seat, user_name=_name(message.author))
    return guild_id, amount, seat


async def start_slots(bot, message: discord.Message, amount: int | None) -> None:
    stake = amount or 10
    packed = await _stake_or_reply(bot, message, stake)
    if not packed:
        return
    guild_id, stake, seat = packed
    view = SlotsView(bot, guild_id, int(message.author.id), stake, seat)
    await send_private_game(message, embed=view._embed(), view=view, on_abort=view.abort)


async def start_rps(bot, message: discord.Message, amount: int | None) -> None:
    stake = amount or 10
    packed = await _stake_or_reply(bot, message, stake)
    if not packed:
        return
    guild_id, stake, seat = packed
    view = RpsView(bot, guild_id, int(message.author.id), stake, seat)
    embed = discord.Embed(title="Rock paper scissors", color=CHIP_COLOR)
    embed.description = f"Bet **{stake}**. Winner takes 2× from me."
    await send_private_game(message, embed=embed, view=view, on_abort=view.abort)


async def start_dice(bot, message: discord.Message, amount: int | None, call: str | None) -> None:
    stake = amount or 10
    pick = (call or "over").lower()
    if pick == "7":
        pick = "seven"
    if pick not in {"over", "under", "seven"}:
        pick = "over"
    packed = await _stake_or_reply(bot, message, stake)
    if not packed:
        return
    guild_id, stake, seat = packed
    view = DiceView(bot, guild_id, int(message.author.id), stake, seat, pick)
    await send_private_game(message, embed=view._embed(), view=view, on_abort=view.abort)


async def start_shop(bot, message: discord.Message) -> None:
    view = ShopView(bot, str(message.guild.id), int(message.author.id))
    embed = shop_embed(bot.store, str(message.guild.id), message.author)
    await send_private_game(message, embed=embed, view=view)


async def start_bag(bot, message: discord.Message) -> None:
    embed = bag_embed(bot.store, str(message.guild.id), message.author)
    await send_private_game(message, embed=embed)


async def start_daily(bot, message: discord.Message) -> None:
    guild_id = str(message.guild.id)
    uid = str(message.author.id)
    left = bot.store.cooldown_left(guild_id, uid, "daily")
    if left > 0:
        hours, rem = divmod(int(left + 0.999), 3600)
        minutes = rem // 60
        wait = f"{hours}h {minutes}m" if hours else f"{minutes}m"
        await message.reply(f"Daily already claimed. Come back in {wait}.", delete_after=12)
        return
    bonus = 50 if bot.store.consume_item(guild_id, uid, "coffee") else 0
    gain = DAILY_CHIPS + bonus
    bot.store.set_cooldown(guild_id, uid, "daily", DAILY_WAIT)
    bot.store.pay_from_bank(guild_id, gain)
    balance = bot.store.add_chips(guild_id, uid, gain, user_name=_name(message.author))
    extra = " Coffee bonus applied." if bonus else ""
    await message.reply(f"Daily **+{gain}** chips.{extra} You now have {balance}.", delete_after=15)


async def start_pay(bot, message: discord.Message, amount: int | None, target) -> None:
    if target is None or not amount:
        await message.reply("Pay someone: `opus pay 50 @user`.")
        return
    if int(target.id) == int(message.author.id):
        await message.reply("You already have those chips.")
        return
    guild_id = str(message.guild.id)
    ok, balance = bot.store.try_spend(guild_id, str(message.author.id), amount, user_name=_name(message.author))
    if not ok:
        await message.reply(f"You only have {balance} chips.")
        return
    got = bot.store.add_chips(guild_id, str(target.id), amount, user_name=_name(target))
    await message.reply(f"Sent **{amount}** chips to {target.mention}. They have {got}. You have {balance}.")
