"""Kingdom event calendar: ``/calendar`` and the daily summary post.

The data comes from kingshot.net (``KingshotCalendarService``). Three views share one
panel: **Today** (what starts, ends and runs on this game day), **This week** and
**Next week** (a timeline of the game week).

Each view is shown as pictures (``calendar_card_renderer``) attached to embeds. The
text builders below are the fallback for when a picture cannot be made or attached:
the same information as embed fields.

Servers that picked a calendar channel and a kingdom in ``/settings`` get the Today
view posted every day at ``DAILY_POST_TIME``. The buttons under that post are
persistent: they keep working after restarts and open a private copy of the panel for
whoever presses them.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, time, timedelta, timezone
from io import BytesIO
from typing import Iterable, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from handlers.permissions import AccessPolicy, interaction_guild_id, send_denied
from handlers.setup_prompts import bot_can_attach
from handlers.ui import EmbedColors, OwnedView, build_status_embed, send_ui_error
from services import calendar_card_renderer as card_renderer
from services.kingshot_calendar_service import (
    CATEGORIES,
    CATEGORY_ALLIANCE,
    CATEGORY_DEALS,
    CATEGORY_GROWTH,
    CATEGORY_PACK,
    CATEGORY_SPECIAL,
    CATEGORY_WAR,
    MAX_KINGDOM_NUMBER,
    CalendarError,
    CalendarEvent,
    CalendarStatus,
    KingdomCalendar,
    KingdomNotTrackedError,
    build_digest,
    build_week,
    filter_categories,
    start_of_day,
    start_of_week,
)

logger = logging.getLogger(__name__)

# When the daily summary is posted (game time is UTC; the game day starts at 00:00).
DAILY_POST_TIME = time(hour=8, minute=0, tzinfo=timezone.utc)
# The loop wakes up on these marks: exactly at the post time, and every ten minutes to
# catch up after a restart, an outage of kingshot.net or a channel that was fixed.
_TICK_TIMES = [time(hour=hour, minute=minute, tzinfo=timezone.utc) for hour in range(24) for minute in range(0, 60, 10)]

# Under Discord's 15-minute interaction token, so expired controls can still be disabled.
PANEL_TIMEOUT_SECONDS = 840.0
EMBED_TOTAL_LIMIT = 6000
EMBED_DESCRIPTION_LIMIT = 4096
EMBED_FIELD_LIMIT = 1024
CALENDAR_GOLD = discord.Color(0xE6A23C)

TAB_TODAY = "today"
TAB_WEEK = "week"
TAB_NEXT = "next"
TAB_LABELS = {TAB_TODAY: "Today", TAB_WEEK: "This week", TAB_NEXT: "Next week"}
TAB_EMOJI = {TAB_TODAY: "📅", TAB_WEEK: "🗓️", TAB_NEXT: "⏭️"}

CATEGORY_STYLE = {
    CATEGORY_WAR: ("⚔️", "War"),
    CATEGORY_ALLIANCE: ("🤝", "Alliance"),
    CATEGORY_GROWTH: ("📈", "Growth"),
    CATEGORY_DEALS: ("💎", "Deals"),
    CATEGORY_SPECIAL: ("✨", "Special"),
    CATEGORY_PACK: ("🛍️", "Packs"),
}
ESTIMATED_NOTE = "est. = usual rotation, not confirmed yet"
# Rendered cards kept for the same calendar, view and minute: a daily post for many
# servers of one kingdom draws its pictures once.
CARD_CACHE_SIZE = 24


# ------------------------------------------------------------------ formatting


def _stamp(moment: datetime, style: str) -> str:
    """A Discord timestamp: every reader sees it in their own time zone."""
    return f"<t:{int(moment.timestamp())}:{style}>"


def _emoji(event: CalendarEvent) -> str:
    return CATEGORY_STYLE.get(event.category, CATEGORY_STYLE[CATEGORY_SPECIAL])[0]


def _title(event: CalendarEvent, *, plain: bool = False) -> str:
    """Event name: main events bold, unconfirmed dates marked."""
    name = discord.utils.escape_markdown(event.display_title)
    if event.is_main and not plain:
        name = f"**{name}**"
    return f"{name} *(est.)*" if event.estimated else name


def _join_lines(lines: Iterable[str], limit: int = EMBED_FIELD_LIMIT) -> str:
    """Join lines without cutting one in half; say how many did not fit."""
    lines = list(lines)
    kept: list[str] = []
    used = 0
    for index, line in enumerate(lines):
        remaining = len(lines) - index
        # Room for this line plus, unless it is the last one, the "more" note.
        reserve = 0 if remaining == 1 else len(f"\n… and {remaining - 1} more")
        if used + len(line) + (1 if kept else 0) + reserve > limit:
            kept.append(f"… and {remaining} more")
            break
        kept.append(line)
        used += len(line) + (1 if len(kept) > 1 else 0)
    return "\n".join(kept)[:limit]


def _fit_embed(embed: discord.Embed) -> discord.Embed:
    """Keep the embed under Discord's total size by dropping the last sections."""
    dropped = 0
    while len(embed) > EMBED_TOTAL_LIMIT - 80 and embed.fields:
        embed.remove_field(len(embed.fields) - 1)
        dropped += 1
    if dropped:
        embed.add_field(name="More", value="The rest did not fit here.", inline=False)
    return embed


def _status_line(status: Optional[CalendarStatus]) -> Optional[str]:
    """One line for the running or next KvK or transfer."""
    if status is None:
        return None
    is_kvk = status.kind == "kvk"
    name = "KvK" if is_kvk else "Transfer"
    if status.number is not None:
        name = f"{name} #{status.number}"
    icon = status.icon or ("⚔️" if is_kvk else "✉️")
    when = _stamp(status.target, "R") if status.target is not None else None
    if status.phase == "countdown":
        return f"{icon} **{name}** starts {when}" if when else f"{icon} **{name}** is next"
    phase_name = discord.utils.escape_markdown(status.phase_name or status.phase.replace("-", " ").title())
    if not when:
        return f"{icon} **{name}** · {phase_name}"
    if is_kvk and status.phase == "preparation":
        return f"{icon} **{name}** · {phase_name} — battle {when}"
    return f"{icon} **{name}** · {phase_name} — ends {when}"


def _status_lines(calendar: KingdomCalendar) -> list[str]:
    return [line for line in (_status_line(calendar.kvk), _status_line(calendar.transfer)) if line]


def _today_line(event: CalendarEvent, now: datetime) -> str:
    head = f"{_emoji(event)} {_title(event)}"
    if event.start > now:
        if event.day_precision:
            return f"{head} · today, time not announced"
        return f"{head} · starts {_stamp(event.start, 't')} ({_stamp(event.start, 'R')})"
    return f"{head} · ends {_stamp(event.end, 'R')}"


def _day_name(day: date) -> str:
    return f"{day:%a}, {day:%b} {day.day}"


def _names(events: Iterable[CalendarEvent]) -> str:
    return ", ".join(_title(event, plain=True) for event in events)


def _range_label(event: CalendarEvent, week_end: datetime) -> str:
    """How long an event runs, in game time (UTC), for the week agenda."""
    if event.day_precision:
        return "time not announced"
    midnight = time(0, 0)
    starts_at_reset = event.start.time() == midnight
    ends_at_reset = event.end.time() == midnight
    last_day = (event.end - timedelta(seconds=1)).date()
    if last_day == event.start.date():
        if starts_at_reset and ends_at_reset:
            return "1 day"
        return f"{event.start:%H:%M}–{event.end:%H:%M} UTC"
    # Through the last full day, like the game shows it. A weekday is enough when the
    # end is near; a date is clearer for long events.
    is_near = event.end <= week_end or last_day - event.start.date() < timedelta(days=7)
    label = f"{last_day:%a} {last_day.day}" if is_near else f"{last_day:%b} {last_day.day}"
    if not ends_at_reset:
        label = f"{label}, {event.end:%H:%M} UTC"
    prefix = "" if starts_at_reset else f"{event.start:%H:%M} UTC "
    return f"{prefix}→ {label}"


def _week_line(event: CalendarEvent, now: datetime, week_end: datetime) -> str:
    name = _title(event)
    if event.has_ended(now):
        name = f"~~{_title(event, plain=True)}~~"
    return f"{_emoji(event)} {name} · {_range_label(event, week_end)}"


def _footer(*parts: str) -> str:
    return " · ".join(part for part in parts if part)


def _has_estimates(events: Iterable[CalendarEvent]) -> bool:
    return any(event.estimated for event in events)


def build_today_embed(
    calendar: KingdomCalendar,
    now: datetime,
    categories: Optional[Iterable[str]] = None,
    *,
    daily: bool = False,
    pictures: bool = False,
) -> discord.Embed:
    """What is on in the kingdom on the game day of ``now``.

    With ``pictures`` the sections are left out: they are shown as attached cards.
    """
    events = filter_categories(calendar.events, categories)
    digest = build_digest(events, now)
    today = start_of_day(now)
    has_main_start = any(event.is_main for event in digest.starting_today)
    embed = discord.Embed(
        title=f"📅 Kingdom {calendar.kingdom} · {today:%A}, {today:%b} {today.day}",
        color=CALENDAR_GOLD if has_main_start else EmbedColors.NEUTRAL,
    )
    lines = _status_lines(calendar)
    if digest.is_empty:
        lines.append(
            "\nNothing is scheduled for this kingdom right now."
            if categories is None
            else "\nNothing in the selected categories right now."
        )
    embed.description = "\n".join(lines)[:EMBED_DESCRIPTION_LIMIT] or None
    shown = [
        *digest.starting_today,
        *digest.ending_today,
        *digest.running,
        *(event for _, day_events in digest.upcoming for event in day_events),
        *digest.packs_starting,
        *digest.packs_ending,
        *digest.packs_running,
    ]
    embed.set_footer(
        text=_footer(
            f"Posted daily at {DAILY_POST_TIME:%H:%M} UTC" if daily else "",
            "Game time (UTC)" if pictures else "" if daily else "Times are in your time zone",
            ESTIMATED_NOTE if _has_estimates(shown) else "",
        )
    )
    embed.timestamp = calendar.fetched_at
    if pictures:
        return embed

    if digest.starting_today:
        embed.add_field(
            name="🆕 Starts today",
            value=_join_lines(_today_line(event, now) for event in digest.starting_today),
            inline=False,
        )
    if digest.ending_today:
        embed.add_field(
            name="⏳ Last day",
            value=_join_lines(_today_line(event, now) for event in digest.ending_today),
            inline=False,
        )
    if digest.running:
        embed.add_field(
            name="▶️ Running",
            value=_join_lines(_today_line(event, now) for event in digest.running),
            inline=False,
        )
    if digest.upcoming:
        embed.add_field(
            name="🔜 Coming up",
            value=_join_lines(
                f"**{_day_name(day)}** · " + ", ".join(f"{_emoji(event)} {_title(event)}" for event in day_events)
                for day, day_events in digest.upcoming
            ),
            inline=False,
        )
    pack_lines = [
        f"**{label}:** {_names(packs)}"
        for label, packs in (
            ("New today", digest.packs_starting),
            ("Last day", digest.packs_ending),
            ("Also on sale", digest.packs_running),
        )
        if packs
    ]
    if pack_lines:
        embed.add_field(name="🛍️ Packs", value=_join_lines(pack_lines), inline=False)
    return _fit_embed(embed)


def build_week_embed(
    calendar: KingdomCalendar,
    week_start: datetime,
    now: datetime,
    categories: Optional[Iterable[str]] = None,
    *,
    pictures: bool = False,
) -> discord.Embed:
    """A day-by-day agenda of the game week that starts on ``week_start``.

    With ``pictures`` the agenda is left out: it is shown as attached timelines.
    """
    week = build_week(filter_categories(calendar.events, categories), week_start)
    last_day = (week.end - timedelta(days=1)).date()
    today = start_of_day(now).date()
    embed = discord.Embed(
        title=(
            f"🗓️ Kingdom {calendar.kingdom} · {week.start:%b} {week.start.day} – "
            f"{last_day:%b} {last_day.day}"
        ),
        color=EmbedColors.NEUTRAL,
    )
    lines = _status_lines(calendar)
    # Events from earlier weeks that are already over would only add noise.
    carried = [event for event in week.carried_over if not event.is_pack and not event.has_ended(now)]
    if carried and not pictures:
        lines.append("")
        lines.append("**Running from earlier**")
        lines.extend(_week_line(event, now, week.end) for event in carried)
    if week.is_empty:
        is_future = week.start > now
        lines.append("")
        if is_future and not calendar.includes_next_week:
            lines.append("Next week could not be loaded. Try again in a few minutes.")
        elif is_future:
            lines.append("There are no dates for this week yet.")
        else:
            lines.append("Nothing is scheduled for this week.")
    embed.description = _join_lines(lines, EMBED_DESCRIPTION_LIMIT) or None

    shown: list[CalendarEvent] = list(carried)
    for day, day_events in week.days:
        regular = [event for event in day_events if not event.is_pack]
        packs = [event for event in day_events if event.is_pack]
        if not regular and not packs:
            continue
        shown.extend(day_events)
        if pictures:
            continue
        day_lines = [_week_line(event, now, week.end) for event in regular]
        if packs:
            day_lines.append(f"🛍️ {_names(packs)}")
        embed.add_field(
            name=f"{_day_name(day)}{' · today' if day == today else ''}",
            value=_join_lines(day_lines),
            inline=False,
        )
    embed.set_footer(
        text=_footer(
            "Game time (UTC)" if pictures else "Game days (UTC), events start at 00:00 unless a time is shown",
            "" if pictures else ESTIMATED_NOTE if _has_estimates(shown) else "",
        )
    )
    embed.timestamp = calendar.fetched_at
    return _fit_embed(embed)


def build_calendar_embed(
    calendar: KingdomCalendar,
    tab: str,
    now: datetime,
    categories: Optional[Iterable[str]] = None,
    *,
    daily: bool = False,
    pictures: bool = False,
) -> discord.Embed:
    if tab == TAB_WEEK:
        return build_week_embed(calendar, start_of_week(now), now, categories, pictures=pictures)
    if tab == TAB_NEXT:
        return build_week_embed(calendar, start_of_week(now) + timedelta(days=7), now, categories, pictures=pictures)
    return build_today_embed(calendar, now, categories, daily=daily, pictures=pictures)


def render_calendar_cards(
    calendar: KingdomCalendar,
    tab: str,
    now: datetime,
    categories: Optional[Iterable[str]] = None,
    extra_icons=None,
) -> list[bytes]:
    """PNG cards for a view. Synchronous and CPU-bound: run it in a thread."""
    events = filter_categories(calendar.events, categories)
    if tab == TAB_WEEK:
        return card_renderer.render_week_cards(build_week(events, start_of_week(now)), now, extra_icons)
    if tab == TAB_NEXT:
        week = build_week(events, start_of_week(now) + timedelta(days=7))
        return card_renderer.render_week_cards(week, now, extra_icons, title="Next week")
    return card_renderer.render_today_cards(build_digest(events, now), now, extra_icons)


def card_filenames(tab: str, count: int) -> list[str]:
    return [f"calendar-{tab}-{index}.png" for index in range(1, count + 1)]


def attach_cards(embed: discord.Embed, filenames: list[str]) -> list[discord.Embed]:
    """Show each card in an embed of its own, so Discord does not shrink them into one box.

    The first card goes into ``embed``; the footer moves under the last card.
    """
    if not filenames:
        return [embed]
    embed.set_image(url=f"attachment://{filenames[0]}")
    embeds = [embed]
    for filename in filenames[1:]:
        extra = discord.Embed(color=embed.color)
        extra.set_image(url=f"attachment://{filename}")
        embeds.append(extra)
    if len(embeds) > 1:
        last = embeds[-1]
        if embed.footer.text:
            last.set_footer(text=embed.footer.text)
            embed.remove_footer()
        last.timestamp, embed.timestamp = embed.timestamp, None
    return embeds


def calendar_error_message(error: Exception, kingdom: int) -> str:
    """A sentence a member can act on."""
    if isinstance(error, KingdomNotTrackedError):
        return (
            f"Kingdom **{kingdom}** is not on the kingshot.net calendar. "
            "Check the number; very new kingdoms appear there a little later."
        )
    return "The event calendar (kingshot.net) is not answering right now. Try again in a few minutes."


# ----------------------------------------------------------------------- views


class CalendarTabButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"calendar:(?P<tab>today|week|next):(?P<kingdom>[0-9]{1,5})",
):
    """Button under a daily post. The kingdom travels in the ``custom_id``, so the
    button of a post from weeks ago still opens the right calendar after a restart."""

    handler: Optional["CalendarHandler"] = None

    def __init__(self, tab: str, kingdom: int):
        super().__init__(
            discord.ui.Button(
                label=TAB_LABELS[tab],
                emoji=TAB_EMOJI[tab],
                style=discord.ButtonStyle.secondary,
                custom_id=f"calendar:{tab}:{kingdom}",
            )
        )
        self.tab = tab
        self.kingdom = kingdom

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match):
        del interaction, item
        return cls(match["tab"], int(match["kingdom"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        handler = type(self).handler
        if handler is None:
            await interaction.response.send_message("The calendar is starting up. Try again in a moment.", ephemeral=True)
            return
        try:
            await handler.send_private_panel(interaction, self.kingdom, self.tab)
        except Exception as error:
            await send_ui_error(interaction, error)


def build_digest_view(kingdom: int) -> discord.ui.View:
    """Buttons under the daily post; anyone may press them."""
    view = discord.ui.View(timeout=None)
    view.add_item(CalendarTabButton(TAB_WEEK, kingdom))
    view.add_item(CalendarTabButton(TAB_NEXT, kingdom))
    return view


class CalendarView(OwnedView):
    """Today / This week / Next week tabs with a category filter."""

    def __init__(self, handler: "CalendarHandler", calendar: KingdomCalendar, author_id: int, tab: str = TAB_TODAY):
        super().__init__(author_id, timeout=PANEL_TIMEOUT_SECONDS)
        self.handler = handler
        self.kingdom = calendar.kingdom
        self.tab = tab if tab in TAB_LABELS else TAB_TODAY
        self.categories: set[str] = set(CATEGORIES)
        self._tab_buttons = {
            TAB_TODAY: self.today_button,
            TAB_WEEK: self.week_button,
            TAB_NEXT: self.next_button,
        }
        self.category_select.options = [
            discord.SelectOption(label=CATEGORY_STYLE[name][1], value=name, emoji=CATEGORY_STYLE[name][0])
            for name in CATEGORIES
        ]
        self.category_select.max_values = len(CATEGORIES)
        self._sync_controls()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # Someone else's panel: give them their own private copy instead of a refusal.
        if interaction.user.id != self.author_id:
            await self.handler.send_private_panel(interaction, self.kingdom, self.tab)
            return False
        return True

    def _sync_controls(self) -> None:
        for tab, button in self._tab_buttons.items():
            active = tab == self.tab
            button.style = discord.ButtonStyle.primary if active else discord.ButtonStyle.secondary
            button.disabled = active
        for option in self.category_select.options:
            option.default = option.value in self.categories

    def _selected_categories(self) -> Optional[set[str]]:
        return None if len(self.categories) == len(CATEGORIES) else set(self.categories)

    async def build_message(self, calendar: KingdomCalendar) -> tuple[list[discord.Embed], list[discord.File]]:
        return await self.handler.build_message(calendar, self.tab, self._selected_categories())

    async def _show(self, interaction: discord.Interaction) -> None:
        self._sync_controls()
        await interaction.response.defer()
        try:
            calendar = await self.handler.service.get_calendar(self.kingdom)
        except CalendarError as error:
            await interaction.followup.send(calendar_error_message(error, self.kingdom), ephemeral=True)
            return
        embeds, files = await self.build_message(calendar)
        await interaction.edit_original_response(embeds=embeds, attachments=files, view=self)

    async def _open_tab(self, interaction: discord.Interaction, tab: str) -> None:
        self.tab = tab
        await self._show(interaction)

    @discord.ui.button(label=TAB_LABELS[TAB_TODAY], emoji=TAB_EMOJI[TAB_TODAY], row=0)
    async def today_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        del button
        await self._open_tab(interaction, TAB_TODAY)

    @discord.ui.button(label=TAB_LABELS[TAB_WEEK], emoji=TAB_EMOJI[TAB_WEEK], row=0)
    async def week_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        del button
        await self._open_tab(interaction, TAB_WEEK)

    @discord.ui.button(label=TAB_LABELS[TAB_NEXT], emoji=TAB_EMOJI[TAB_NEXT], row=0)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        del button
        await self._open_tab(interaction, TAB_NEXT)

    @discord.ui.select(placeholder="Show categories", min_values=1, max_values=1, row=1)
    async def category_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        self.categories = set(select.values) & set(CATEGORIES) or set(CATEGORIES)
        await self._show(interaction)


# --------------------------------------------------------------------- handler


class CalendarHandler:
    """``/calendar`` and the daily calendar post."""

    def __init__(
        self,
        calendar_service,
        bot: commands.Bot,
        *,
        guild_configuration_service=None,
        access_policy: Optional[AccessPolicy] = None,
        now_provider=None,
    ):
        self.service = calendar_service
        self._bot = bot
        self._guild_configuration_service = guild_configuration_service
        self._access = access_policy or AccessPolicy(guild_configuration_service=guild_configuration_service)
        self._now_provider = now_provider or (lambda: datetime.now(timezone.utc))
        self._persistent_views_registered = False
        self._post_lock = asyncio.Lock()
        # Day of the last post per server, kept in memory too: if saving it fails the
        # next pass must not post the same summary again.
        self._posted_on: dict[int, date] = {}
        # Last reason a server's post failed, so a broken channel is logged once.
        self._post_failures: dict[int, str] = {}
        self._background_tasks: set[asyncio.Task] = set()
        self._card_cache: dict[tuple, list[bytes]] = {}
        logger.info("CalendarHandler initialized")

    def now(self) -> datetime:
        return self._now_provider()

    # ------------------------------------------------------------- pictures

    async def render_cards(
        self,
        calendar: KingdomCalendar,
        tab: str,
        now: datetime,
        categories: Optional[Iterable[str]] = None,
    ) -> list[bytes]:
        """The view as PNG cards, or an empty list when they cannot be drawn."""
        key = (
            calendar.kingdom,
            calendar.fetched_at,
            tab,
            frozenset(categories) if categories is not None else None,
            now.replace(second=0, microsecond=0),
        )
        cached = self._card_cache.get(key)
        if cached is not None:
            return cached
        try:
            events = filter_categories(calendar.events, categories)
            missing = await asyncio.to_thread(card_renderer.missing_icon_keys, events)
            loader = getattr(self.service, "get_event_icons", None)
            extra_icons = await loader(missing) if missing and loader is not None else None
            cards = await asyncio.to_thread(render_calendar_cards, calendar, tab, now, categories, extra_icons)
        except Exception:
            logger.warning("Could not draw the %s calendar of kingdom %s", tab, calendar.kingdom, exc_info=True)
            return []
        while len(self._card_cache) >= CARD_CACHE_SIZE:
            self._card_cache.pop(next(iter(self._card_cache)))
        self._card_cache[key] = cards
        return cards

    async def build_message(
        self,
        calendar: KingdomCalendar,
        tab: str = TAB_TODAY,
        categories: Optional[Iterable[str]] = None,
        *,
        now: Optional[datetime] = None,
        daily: bool = False,
        pictures: bool = True,
    ) -> tuple[list[discord.Embed], list[discord.File]]:
        """Embeds and attachments for a view: picture cards, or text when there are none."""
        now = now or self.now()
        cards = await self.render_cards(calendar, tab, now, categories) if pictures else []
        embed = build_calendar_embed(calendar, tab, now, categories, daily=daily, pictures=bool(cards))
        filenames = card_filenames(tab, len(cards))
        files = [discord.File(BytesIO(png), filename=name) for png, name in zip(cards, filenames)]
        return attach_cards(embed, filenames), files

    # ------------------------------------------------------------ commands

    def register_commands(self) -> None:
        @self._bot.tree.command(
            name="calendar",
            description="Kingdom event calendar: what is on today, this week and next week",
        )
        @app_commands.describe(kingdom="Kingdom number. Leave empty to use this server's kingdom")
        @app_commands.checks.cooldown(4, 30.0)
        async def calendar(
            interaction: discord.Interaction,
            kingdom: Optional[app_commands.Range[int, 1, MAX_KINGDOM_NUMBER]] = None,
        ):
            await self._handle_calendar(interaction, kingdom)

    def register_persistent_views(self) -> None:
        """Make the buttons of daily posts sent earlier work again (call once, on ready)."""
        add_dynamic_items = getattr(self._bot, "add_dynamic_items", None)
        if self._persistent_views_registered or not callable(add_dynamic_items):
            return
        CalendarTabButton.handler = self
        add_dynamic_items(CalendarTabButton)
        self._persistent_views_registered = True

    async def _settings(self, guild_id: Optional[int]):
        if guild_id is None or self._guild_configuration_service is None:
            return None
        try:
            return await self._guild_configuration_service.get_settings(guild_id)
        except Exception:
            logger.warning("Could not load calendar settings for guild %s", guild_id, exc_info=True)
            return None

    async def _handle_calendar(self, interaction: discord.Interaction, kingdom: Optional[int] = None) -> None:
        guild_id = interaction_guild_id(interaction)
        # Acknowledge first: the settings lookup below may wait on the database.
        await interaction.response.defer(thinking=True, ephemeral=True)
        if kingdom is None:
            kingdom = getattr(await self._settings(guild_id), "kingdom_id", None)
        if kingdom is None:
            hint = (
                "A server manager can set this server's kingdom in `/settings`, "
                "then `/calendar` works without a number."
                if guild_id is not None
                else ""
            )
            await interaction.edit_original_response(
                embed=build_status_embed(
                    title="Which kingdom?",
                    description=f"Add the kingdom number: `/calendar kingdom:795`. {hint}".strip(),
                    color=EmbedColors.INFO,
                ),
            )
            return

        try:
            calendar = await self.service.get_calendar(int(kingdom))
        except CalendarError as error:
            await interaction.edit_original_response(content=calendar_error_message(error, kingdom))
            return
        except Exception as error:
            await send_ui_error(interaction, error)
            return

        view = CalendarView(self, calendar, interaction.user.id)
        embeds, files = await view.build_message(calendar)
        # Complete the private defer before sending the public calendar.
        await interaction.edit_original_response(content="Calendar ready.")
        view.message = await interaction.followup.send(embeds=embeds, files=files, view=view, wait=True)
        try:
            await interaction.delete_original_response()
        except discord.HTTPException:
            logger.debug("Could not remove the private calendar receipt", exc_info=True)

    async def send_private_panel(self, interaction: discord.Interaction, kingdom: int, tab: str = TAB_TODAY) -> None:
        """Open a private copy of the calendar panel for whoever pressed a button."""
        if self._access.is_banned(interaction.user.id):
            await send_denied(interaction, "You can't use this bot.")
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            calendar = await self.service.get_calendar(kingdom)
        except CalendarError as error:
            await interaction.followup.send(calendar_error_message(error, kingdom), ephemeral=True)
            return
        view = CalendarView(self, calendar, interaction.user.id, tab)
        embeds, files = await view.build_message(calendar)
        view.message = await interaction.followup.send(
            embeds=embeds, files=files, view=view, ephemeral=True, wait=True
        )

    # ---------------------------------------------------------- daily post

    async def verify_kingdom(self, kingdom: int) -> None:
        """Raise ``CalendarError`` when the kingdom's calendar cannot be loaded."""
        await self.service.get_calendar(kingdom)

    def next_post_time(self, now: Optional[datetime] = None) -> datetime:
        now = (now or self.now()).astimezone(timezone.utc)
        today = datetime.combine(now.date(), DAILY_POST_TIME)
        return today if now < today else today + timedelta(days=1)

    async def post_now(self, guild_id: int) -> tuple[bool, str]:
        """Post today's summary for one server right away (after its settings changed).

        Returns whether it was posted and a sentence for the manager. The day counts as
        posted, so the scheduled post does not repeat it a few hours later.
        """
        settings = await self._settings(guild_id)
        channel_id = getattr(settings, "calendar_channel_id", None)
        kingdom = getattr(settings, "kingdom_id", None)
        if not channel_id or not kingdom:
            return False, "Pick both a calendar channel and a kingdom to start the daily post."
        async with self._post_lock:
            problem = await self._post_digest(guild_id, channel_id, kingdom, self.now())
        if problem:
            return False, f"Could not post the calendar: {problem}"
        return True, f"Posted today's calendar in <#{channel_id}>."

    async def post_due_digests(self, now: Optional[datetime] = None) -> int:
        """Post today's summary to every server that is due. Returns how many were posted."""
        if self._guild_configuration_service is None:
            return 0
        now = (now or self.now()).astimezone(timezone.utc)
        if now.timetz() < DAILY_POST_TIME:
            return 0
        today = now.date()
        connected = {guild.id for guild in getattr(self._bot, "guilds", None) or ()}
        posted = 0
        async with self._post_lock:
            for settings in await self._guild_configuration_service.list_all():
                guild_id = settings.guild_id
                channel_id = getattr(settings, "calendar_channel_id", None)
                kingdom = getattr(settings, "kingdom_id", None)
                if not channel_id or not kingdom or guild_id not in connected:
                    continue
                if getattr(settings, "calendar_last_posted_on", None) == today or self._posted_on.get(guild_id) == today:
                    continue
                if await self._post_digest(guild_id, channel_id, kingdom, now) is None:
                    posted += 1
        return posted

    async def _post_digest(self, guild_id: int, channel_id: int, kingdom: int, now: datetime) -> Optional[str]:
        """Send the summary and record the day. Returns the problem, or None on success."""
        problem = await self._send_digest(guild_id, channel_id, kingdom, now)
        if problem is not None:
            if self._post_failures.get(guild_id) != problem:
                logger.warning("Daily calendar for guild %s was not posted: %s", guild_id, problem)
            self._post_failures[guild_id] = problem
            return problem
        self._post_failures.pop(guild_id, None)
        self._posted_on[guild_id] = now.date()
        try:
            await self._guild_configuration_service.update_for_guild(guild_id, calendar_last_posted_on=now.date())
        except Exception:
            logger.warning("Could not record the calendar post for guild %s", guild_id, exc_info=True)
        return None

    async def _send_digest(self, guild_id: int, channel_id: int, kingdom: int, now: datetime) -> Optional[str]:
        try:
            calendar = await self.service.get_calendar(kingdom)
        except KingdomNotTrackedError:
            return f"kingdom {kingdom} is not on the kingshot.net calendar."
        except CalendarError:
            return "kingshot.net is not answering right now; I will retry shortly."
        try:
            channel = self._bot.get_channel(channel_id) or await self._bot.fetch_channel(channel_id)
            channel_guild_id = getattr(getattr(channel, "guild", None), "id", None)
            if channel_guild_id is not None and channel_guild_id != guild_id:
                return "the calendar channel belongs to another server."
            # Without Attach Files the pictures would make Discord reject the whole post.
            embeds, files = await self.build_message(
                calendar, TAB_TODAY, now=now, daily=True, pictures=bot_can_attach(channel)
            )
            await channel.send(
                embeds=embeds,
                files=files,
                view=build_digest_view(kingdom),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.NotFound:
            return "the calendar channel no longer exists."
        except discord.Forbidden:
            return f"I need View Channel, Send Messages and Embed Links in <#{channel_id}>."
        except discord.HTTPException as error:
            return f"Discord rejected the message ({error.status})."
        return None

    def _run_in_background(self, coroutine) -> None:
        task = asyncio.create_task(coroutine)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _run_due_pass(self) -> None:
        try:
            posted = await self.post_due_digests()
            if posted:
                logger.info("Posted the daily calendar in %s server(s)", posted)
        except Exception:
            logger.exception("Posting daily calendars failed")

    def start_daily_task(self) -> None:
        if self._daily_loop.is_running():
            return
        self._daily_loop.start()
        # The loop first wakes on the next ten-minute mark; catch up right away after a restart.
        self._run_in_background(self._run_due_pass())
        logger.info("Daily calendar task started (posts at %s UTC)", f"{DAILY_POST_TIME:%H:%M}")

    def stop_daily_task(self) -> None:
        if self._daily_loop.is_running():
            self._daily_loop.cancel()

    def is_daily_post_running(self) -> bool:
        return self._daily_loop.is_running()

    @tasks.loop(time=_TICK_TIMES)
    async def _daily_loop(self):
        await self._run_due_pass()

    @_daily_loop.before_loop
    async def _before_daily_loop(self):
        await self._bot.wait_until_ready()
