import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

from handlers.confirm_view import ConfirmView
from handlers.permissions import AccessPolicy, manager_only_command
from handlers.ui import EmbedColors, OwnedView, build_status_embed, edit_panel_message
from services.event_scheduler_service import (
    DEFAULT_GRACE_PERIOD,
    IEventSchedulerService,
    ScheduledEvent,
)

logger = logging.getLogger(__name__)

DISCORD_MESSAGE_LIMIT = 2000
EVERYONE_NAMES = frozenset({"everyone", "@everyone"})
HERE_NAMES = frozenset({"here", "@here"})


def parse_event_time(date: str, time: str, time_zone: str) -> datetime:
    """Resolve a local time without silently accepting DST gaps or overlaps."""
    try:
        local = datetime.strptime(f"{date.strip()} {time.strip()}", "%Y-%m-%d %H:%M")
    except ValueError:
        raise ValueError("Use YYYY-MM-DD for the date and HH:MM for the time.") from None
    try:
        zone = ZoneInfo(time_zone.strip())
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("Use a timezone such as UTC, Europe/Kyiv, or America/New_York.") from None
    candidates = set()
    for fold in (0, 1):
        candidate = local.replace(tzinfo=zone, fold=fold).astimezone(timezone.utc)
        if candidate.astimezone(zone).replace(tzinfo=None) == local:
            candidates.add(candidate)
    if not candidates:
        raise ValueError("That local time does not exist because the clocks move forward. Choose another time.")
    if len(candidates) > 1:
        raise ValueError("That local time occurs twice when the clocks move back. Enter the intended time in UTC.")
    return candidates.pop()


def format_targets(role_names: list[str]) -> str:
    """Human-readable ping targets for previews and cards (never sent as a mention)."""
    names = [str(name).strip() for name in role_names or () if str(name).strip()]
    if not names:
        return "@everyone"
    labels = []
    for name in names:
        lowered = name.casefold()
        if lowered in EVERYONE_NAMES:
            labels.append("@everyone")
        elif lowered in HERE_NAMES:
            labels.append("@here")
        else:
            labels.append(f"@{name.lstrip('@')}")
    return discord.utils.escape_markdown(", ".join(dict.fromkeys(labels)))


def _short_label(message: str, limit: int = 60) -> str:
    text = " ".join(str(message).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _chunk_message(content: str, limit: int = DISCORD_MESSAGE_LIMIT) -> list[str]:
    """Split long legacy reminders into Discord-sized messages, preferring line breaks."""
    chunks = []
    while len(content) > limit:
        cut = content.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(content[:cut])
        content = content[cut:].lstrip("\n")
    if content:
        chunks.append(content)
    return chunks


@dataclass(frozen=True)
class DeliveryOutcome:
    """Result of one attempt to post a reminder."""

    delivered: bool
    error: Optional[str] = None
    retry: bool = False
    # The channel is gone for good: stop the reminder even if it repeats.
    deactivate: bool = False


@dataclass(frozen=True)
class SchedulerHealth:
    """Reminder scheduler state since the bot process started."""

    running: bool
    last_run_at: Optional[datetime]
    last_success_at: Optional[datetime]
    last_error: Optional[str]
    last_error_at: Optional[datetime]
    failed_count: int
    sent_count: int
    missed_count: int

    def summary(self, *, include_errors: bool = True) -> str:
        """Compact multi-line text for a status embed field (under 1024 characters).

        Error text can contain database details and other servers' reminder IDs, so
        ``/status`` passes ``include_errors`` only for bot operators.
        """
        lines = []
        if self.last_run_at is None:
            lines.append("No run yet")
        else:
            lines.append(f"Last run <t:{int(self.last_run_at.timestamp())}:R>")
        lines.append(f"Sent {self.sent_count} · Failed {self.failed_count} · Missed {self.missed_count}")
        if self.last_error and include_errors:
            when = f" (<t:{int(self.last_error_at.timestamp())}:R>)" if self.last_error_at else ""
            lines.append(f"Last error{when}: {self.last_error[:300]}")
        return "\n".join(lines)


class EventForm(discord.ui.Modal):
    def __init__(
        self,
        handler,
        author_id: int,
        channel_id: int,
        *,
        event: ScheduledEvent | None = None,
        date: str = "",
        time: str = "",
        message: str = "",
        time_zone: str = "UTC",
        reminder_minutes: int = 10,
        repeat_every_days: int | None = None,
    ):
        super().__init__(title="Edit reminder" if event else "Schedule event", timeout=300)
        self.handler, self.author_id, self.channel_id, self.event = (
            handler,
            author_id,
            channel_id,
            event,
        )
        if event:
            date, time = event.event_time.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M").split()
            message, repeat_every_days = event.message, event.repeat_every_days
        self.when = discord.ui.TextInput(
            label="Reminder date/time" if event else "Event date/time",
            placeholder="YYYY-MM-DD HH:MM",
            default=f"{date} {time}".strip() or None,
            max_length=16,
        )
        self.zone = discord.ui.TextInput(
            label="Timezone",
            placeholder="Europe/Kyiv or America/New_York",
            default=time_zone,
            max_length=100,
        )
        self.message_input = discord.ui.TextInput(
            label="Reminder message",
            style=discord.TextStyle.paragraph,
            default=message or None,
            max_length=4000 if event and len(message) > 1900 else 1900,
        )
        self.repeat = discord.ui.TextInput(
            label="Repeat every N days (blank = once)",
            required=False,
            default=str(repeat_every_days) if repeat_every_days else None,
            max_length=3,
        )
        for item in (self.when, self.zone, self.message_input, self.repeat):
            self.add_item(item)
        if not event:
            self.lead = discord.ui.TextInput(
                label="Remind how many minutes before?",
                default=str(reminder_minutes),
                max_length=4,
            )
            self.add_item(self.lead)

    async def on_submit(self, interaction: discord.Interaction):
        if not await self.handler._can_manage(interaction, self.author_id, self.channel_id):
            return
        try:
            date, time = self.when.value.strip().split()
            repeat = int(self.repeat.value) if self.repeat.value.strip() else None
            lead = int(self.lead.value) if self.event is None else 0
        except ValueError:
            await self.handler._send_error(
                interaction,
                "Use YYYY-MM-DD HH:MM and whole numbers for minutes and days.",
            )
            return
        await self.handler._show_preview(
            interaction,
            date,
            time,
            self.message_input.value,
            repeat,
            lead,
            self.zone.value,
            event=self.event,
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        logger.error("Event form failed", exc_info=(type(error), error, error.__traceback__))
        await self.handler._send_error(
            interaction,
            "The reminder could not be prepared. Reopen /remind or /reminders.",
        )


class EventPreview(OwnedView):
    def __init__(
        self,
        handler,
        author_id: int,
        channel_id: int,
        event: ScheduledEvent,
        embed: discord.Embed,
    ):
        super().__init__(author_id)
        self.handler, self.channel_id, self.event, self.embed = (
            handler,
            channel_id,
            event,
            embed,
        )
        self.saving = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await super().interaction_check(interaction) and await self.handler._can_manage(
            interaction,
            self.author_id,
            self.channel_id,
        )

    @discord.ui.button(label="Save reminder", style=discord.ButtonStyle.success)
    async def save(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.handler._can_manage(interaction, self.author_id, self.channel_id):
            return
        if self.saving:
            await interaction.response.send_message("This preview has already been submitted.", ephemeral=True)
            return
        self.saving = True
        await interaction.response.defer()
        event = self.event
        try:
            if event.id is None:
                saved = await self.handler._scheduler_service.schedule_event(
                    self.channel_id,
                    event.event_time,
                    event.role_names,
                    event.message,
                    event.repeat_every_days,
                    guild_id=getattr(interaction, "guild_id", None),
                    created_by_user_id=interaction.user.id,
                )
            else:
                saved = await self.handler._scheduler_service.update_event(
                    self.channel_id,
                    event.id,
                    event.event_time,
                    event.message,
                    event.repeat_every_days,
                )
        except Exception:
            logger.exception("Could not save reminder for channel %s", self.channel_id)
            self.saving = False
            await self.handler._send_error(
                interaction,
                "The reminder could not be saved right now. Press Save reminder again in a moment.",
            )
            return
        if not saved:
            self.saving = False
            await self.handler._send_error(
                interaction,
                "This reminder is no longer active or its time has passed. Reopen /reminders or /remind.",
            )
            return
        self.stop()
        self.embed.title = "Reminder updated" if event.id is not None else "Event scheduled"
        self.embed.description = "Saved. Use /reminders to edit or cancel this reminder."
        self.embed.color = EmbedColors.SUCCESS
        await interaction.edit_original_response(
            embed=self.embed, view=None, allowed_mentions=discord.AllowedMentions.none()
        )

    @discord.ui.button(label="Discard", style=discord.ButtonStyle.secondary)
    async def discard(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.handler._can_manage(interaction, self.author_id, self.channel_id):
            return
        if self.saving:
            await interaction.response.send_message("This preview has already been submitted.", ephemeral=True)
            return
        self.stop()
        self.saving = True
        await interaction.response.edit_message(
            embed=build_status_embed(title="Draft discarded", description="The schedule was not changed."),
            view=None,
        )


class EventListView(OwnedView):
    PANEL_TIMEOUT_SECONDS = 900.0

    def __init__(
        self,
        handler,
        author_id: int,
        channel_id: int,
        events: list[ScheduledEvent],
        can_manage: bool,
    ):
        super().__init__(author_id, timeout=self.PANEL_TIMEOUT_SECONDS)
        self.handler, self.channel_id, self.events = handler, channel_id, events
        self.index = 0
        self.edit.disabled = self.cancel.disabled = self.create.disabled = not can_manage
        self._refresh_controls()

    def _refresh_controls(self):
        self.previous.disabled = self.index == 0
        self.next.disabled = self.index >= len(self.events) - 1
        event_id = self.events[self.index].id
        self.edit.custom_id = f"event:edit:{event_id}"
        self.cancel.custom_id = f"event:cancel:{event_id}"
        start = self.index // 25 * 25
        self.select_event.options = [
            discord.SelectOption(
                label=f"#{event.id} · {event.message}"[:100],
                value=str(event.id),
                default=index == self.index,
            )
            for index, event in enumerate(self.events[start : start + 25], start)
        ]

    def embed(self) -> discord.Embed:
        event = self.events[self.index]
        embed = build_status_embed(
            title=f"Scheduled event #{event.id}",
            description=f"{len(self.events)} active reminder(s) in this channel.",
        )
        embed.add_field(
            name="Next reminder",
            value=self.handler._format_discord_timestamp(event.event_time),
            inline=False,
        )
        embed.add_field(
            name="Repeat",
            value=self.handler._format_repeat(event.repeat_every_days),
            inline=False,
        )
        embed.add_field(name="Pings", value=format_targets(event.role_names)[:1024], inline=False)
        if event.last_sent_at is not None:
            embed.add_field(
                name="Last sent",
                value=self.handler._format_discord_timestamp(event.last_sent_at),
                inline=False,
            )
        if event.last_error:
            embed.add_field(name="Last problem", value=event.last_error[:1024], inline=False)
            embed.color = EmbedColors.WARNING
        message = event.message[:1900]
        for offset in range(0, len(message), 950):
            embed.add_field(
                name="Message" if offset == 0 else "Message (continued)",
                value=message[offset : offset + 950],
                inline=False,
            )
        embed.set_footer(
            text=f"Event {self.index + 1}/{len(self.events)} · Times use your Discord timezone · ID {event.id}"
        )
        if len(event.message) > 1900:
            embed.description += " Message preview shortened to 1900 characters."
        return embed

    async def _render(self, interaction: discord.Interaction):
        self._refresh_controls()
        await interaction.response.edit_message(
            embed=self.embed(),
            view=self,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @discord.ui.select(placeholder="Choose an event", row=0)
    async def select_event(self, interaction: discord.Interaction, select: discord.ui.Select):
        event_id = int(select.values[0])
        self.index = next(index for index, event in enumerate(self.events) if event.id == event_id)
        await self._render(interaction)

    @discord.ui.button(label="Previous", row=1)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index = max(0, self.index - 1)
        await self._render(interaction)

    @discord.ui.button(label="Next", row=1)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index = min(len(self.events) - 1, self.index + 1)
        await self._render(interaction)

    @discord.ui.button(label="Edit reminder", style=discord.ButtonStyle.primary, row=2)
    async def edit(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.handler._can_manage(interaction, self.author_id, self.channel_id):
            return
        event_id = int(interaction.data["custom_id"].rsplit(":", 1)[1])
        event = next((event for event in self.events if event.id == event_id), None)
        if event is None:
            await self.handler._send_error(interaction, "This event is no longer active. Reopen /reminders.")
            return
        if len(event.message) > 4000:
            await self.handler._send_error(
                interaction,
                "This legacy message exceeds the form limit. Create a shorter reminder, then cancel this event.",
            )
            return
        await interaction.response.send_modal(EventForm(self.handler, self.author_id, self.channel_id, event=event))

    @discord.ui.button(label="Cancel event", style=discord.ButtonStyle.danger, row=2)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.handler._can_manage(interaction, self.author_id, self.channel_id):
            return
        event_id = int(interaction.data["custom_id"].rsplit(":", 1)[1])
        event = next((event for event in self.events if event.id == event_id), None)
        if event is None:
            await self.handler._send_error(interaction, "This event is no longer active. Reopen /reminders.")
            return

        async def confirm(confirm_interaction: discord.Interaction):
            await self._confirm_cancel(confirm_interaction, event_id)

        label = discord.utils.escape_markdown(_short_label(event.message))
        prompt = ConfirmView(
            interaction.user.id,
            on_confirm=confirm,
            confirm_label="Cancel event",
            keep_label="Keep",
            kept_title="Event kept",
            kept_description=f"Event #{event_id} is still scheduled.",
        )
        await interaction.response.send_message(
            embed=build_status_embed(
                title=f"Cancel #{event_id} '{label}'?"[:256],
                description="The reminder stops immediately. This cannot be undone.",
                color=EmbedColors.WARNING,
            ),
            view=prompt,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        try:
            prompt.message = await interaction.original_response()
        except discord.HTTPException:
            logger.debug("Could not load the cancel confirmation message", exc_info=True)

    async def _confirm_cancel(self, interaction: discord.Interaction, event_id: int):
        if not await self.handler._can_manage(interaction, self.author_id, self.channel_id):
            return
        await interaction.response.defer()
        try:
            cancelled = await self.handler._scheduler_service.cancel_event(self.channel_id, event_id)
        except Exception:
            logger.exception("Could not cancel reminder %s", event_id)
            await interaction.edit_original_response(
                embed=build_status_embed(
                    title="Event not cancelled",
                    description="The reminder could not be cancelled right now. Try again in a moment.",
                    color=EmbedColors.WARNING,
                ),
                view=None,
            )
            return
        if not cancelled:
            await interaction.edit_original_response(
                embed=build_status_embed(
                    title="Event not cancelled",
                    description="This event is no longer active. Reopen /reminders.",
                    color=EmbedColors.WARNING,
                ),
                view=None,
            )
            return
        await interaction.edit_original_response(
            embed=build_status_embed(
                title="Event cancelled",
                description=f"Event #{event_id} was cancelled.",
                color=EmbedColors.SUCCESS,
            ),
            view=None,
        )
        await self._refresh_after_cancel(event_id)

    async def _refresh_after_cancel(self, event_id: int):
        """Update the public list after a cancellation; failures leave the stale card."""
        try:
            self.events = await self.handler._scheduler_service.get_events_for_channel(self.channel_id)
        except Exception:
            logger.warning("Could not refresh events after cancelling %s", event_id, exc_info=True)
            self.events = [event for event in self.events if event.id != event_id]
        if self.message is None:
            if not self.events:
                self.stop()
            return
        try:
            if self.events:
                self.index = min(self.index, len(self.events) - 1)
                self._refresh_controls()
                embed = self.embed()
                embed.description = f"Event #{event_id} cancelled. {len(self.events)} active reminder(s) remain."
                await edit_panel_message(
                    self.message, embed=embed, view=self, allowed_mentions=discord.AllowedMentions.none()
                )
            else:
                self.stop()
                await edit_panel_message(
                    self.message,
                    embed=build_status_embed(
                        title="Event cancelled",
                        description="There are no active reminders in this channel.",
                        color=EmbedColors.SUCCESS,
                    ),
                    view=None,
                )
        except discord.HTTPException:
            logger.debug("Could not refresh the events panel", exc_info=True)

    @discord.ui.button(label="Create event", row=2)
    async def create(self, interaction: discord.Interaction, button: discord.ui.Button):
        if await self.handler._can_manage(interaction, self.author_id, self.channel_id):
            await self.handler.open_schedule_form(interaction)


class EventHandler:
    """Handles event scheduling Discord commands and the reminder scheduler."""

    GRACE_PERIOD = DEFAULT_GRACE_PERIOD
    SCHEDULER_RESTART_DELAY_SECONDS = 30.0

    def __init__(
        self,
        scheduler_service: IEventSchedulerService,
        bot: commands.Bot,
        admin_user_ids: set[int] | None = None,
        access_policy: AccessPolicy | None = None,
    ):
        self._scheduler_service, self._bot = scheduler_service, bot
        self._admin_user_ids = admin_user_ids or set()
        self._access = access_policy or AccessPolicy(self._admin_user_ids)
        self._scheduler_loop = None
        self._scheduler_stopped = False
        # Occurrences already posted whose bookkeeping could not be saved yet;
        # they are recorded again without being re-posted.
        self._delivered_pending: dict[tuple[int, datetime], DeliveryOutcome] = {}
        self._iteration_active = False
        self._last_run_at: datetime | None = None
        self._last_success_at: datetime | None = None
        self._last_error: str | None = None
        self._last_error_at: datetime | None = None
        self._failed_count = 0
        self._sent_count = 0
        self._missed_count = 0

    async def _send_error(
        self,
        interaction: discord.Interaction,
        description: str,
        *,
        title: str = "Reminder not saved",
    ):
        send = interaction.followup.send if interaction.response.is_done() else interaction.response.send_message
        await send(
            embed=build_status_embed(
                title=title,
                description=description,
                color=EmbedColors.WARNING,
            ),
            ephemeral=True,
        )

    async def _can_manage(
        self,
        interaction: discord.Interaction,
        author_id: int | None = None,
        channel_id: int | None = None,
    ) -> bool:
        if not await self._access.require_manager(interaction, action="manage reminders"):
            return False
        if author_id is not None and interaction.user.id != author_id:
            await self._send_error(interaction, "Only the person who opened this form can submit it.")
            return False
        if interaction.channel_id is None or (channel_id is not None and interaction.channel_id != channel_id):
            await self._send_error(interaction, "Open this action in the event's channel.")
            return False
        return True

    def register_commands(self):
        @self._bot.tree.command(
            name="remind",
            description="Create a reminder: open the form, or preview one from options",
        )
        @manager_only_command
        @app_commands.describe(
            date="Event date (YYYY-MM-DD)",
            time="Event time (HH:MM in the selected timezone)",
            message="Reminder message",
            time_zone="Timezone name, for example Europe/Kyiv (default: UTC)",
            repeat_every_days="Repeat every N days (fixed 24-hour periods)",
            reminder_minutes="Minutes before the event to send the reminder (default: 10)",
        )
        async def schedule_event(
            interaction: discord.Interaction,
            date: Optional[str] = None,
            time: Optional[str] = None,
            message: Optional[str] = None,
            repeat_every_days: Optional[int] = None,
            reminder_minutes: int = 10,
            time_zone: str = "UTC",
        ):
            await self._handle_schedule_event(
                interaction,
                date,
                time,
                message,
                repeat_every_days,
                reminder_minutes,
                time_zone,
            )

        @self._bot.tree.command(
            name="reminders",
            description="Browse this channel's reminders; managers can edit and cancel them",
        )
        async def list_events(interaction: discord.Interaction):
            await self._handle_list_events(interaction)

    async def open_schedule_form(self, interaction: discord.Interaction, **defaults):
        if await self._can_manage(interaction):
            await interaction.response.send_modal(
                EventForm(self, interaction.user.id, interaction.channel_id, **defaults)
            )

    async def _handle_schedule_event(
        self,
        interaction: discord.Interaction,
        date: str | None = None,
        time: str | None = None,
        message: str | None = None,
        repeat_every_days: int | None = None,
        reminder_minutes: int = 10,
        time_zone: str = "UTC",
    ):
        if not all(value is not None for value in (date, time, message)):
            if (
                len(date or "") > 10
                or len(time or "") > 5
                or len(message or "") > 1900
                or len(time_zone) > 100
                or not 1 <= reminder_minutes <= 1440
                or (repeat_every_days is not None and not 1 <= repeat_every_days <= 365)
            ):
                if await self._can_manage(interaction):
                    await self._send_error(
                        interaction,
                        "Use YYYY-MM-DD, HH:MM, a message up to 1900 characters, 1–1440 reminder minutes, and 1–365 repeat days.",
                    )
                return
            await self.open_schedule_form(
                interaction,
                date=date or "",
                time=time or "",
                message=message or "",
                repeat_every_days=repeat_every_days,
                reminder_minutes=reminder_minutes,
                time_zone=time_zone,
            )
            return
        await self._show_preview(
            interaction,
            date,
            time,
            message,
            repeat_every_days,
            reminder_minutes,
            time_zone,
        )

    async def _show_preview(
        self,
        interaction: discord.Interaction,
        date: str,
        time: str,
        message: str,
        repeat_every_days: int | None,
        reminder_minutes: int,
        time_zone: str,
        *,
        event: ScheduledEvent | None = None,
    ):
        if not await self._can_manage(interaction):
            return
        try:
            message = message.strip()
            if not message or len(message) > 1900:
                raise ValueError("Provide a reminder message between 1 and 1900 characters.")
            if event is None and not 1 <= reminder_minutes <= 1440:
                raise ValueError("Reminder lead time must be between 1 and 1440 minutes.")
            if repeat_every_days is not None and not 1 <= repeat_every_days <= 365:
                raise ValueError("Repeat interval must be between 1 and 365 days, or blank for a one-time event.")
            selected_time = parse_event_time(date, time, time_zone)
            reminder_time = selected_time - timedelta(minutes=reminder_minutes)
            if reminder_time <= datetime.now(timezone.utc):
                raise ValueError(
                    "The reminder must be in the future. Choose a later time or a shorter reminder lead time."
                )
        except ValueError as error:
            await self._send_error(interaction, str(error))
            return
        role_names = event.role_names if event else ["everyone"]
        blocked, permission_warning = self._channel_permission_problems(interaction, role_names)
        if blocked:
            await self._send_error(interaction, blocked)
            return
        draft = ScheduledEvent(
            event.id if event else None,
            reminder_time,
            role_names,
            message,
            repeat_every_days,
        )
        embed = build_status_embed(
            title="Preview reminder changes" if event else "Preview event",
            description=f"Review the times and message, then save. The reminder will ping {format_targets(role_names)}.",
        )
        if event is None:
            embed.add_field(
                name="Event",
                value=self._format_discord_timestamp(selected_time),
                inline=False,
            )
        embed.add_field(
            name="Reminder",
            value=self._format_discord_timestamp(reminder_time),
            inline=False,
        )
        embed.add_field(
            name="Input timezone",
            value=f"{time_zone.strip()} · {selected_time.astimezone(ZoneInfo(time_zone.strip())):%Y-%m-%d %H:%M %Z}",
            inline=False,
        )
        embed.add_field(name="Repeat", value=self._format_repeat(repeat_every_days), inline=False)
        if permission_warning:
            embed.add_field(name="⚠️ No ping", value=permission_warning, inline=False)
        for offset in range(0, len(message), 950):
            embed.add_field(
                name="Message" if offset == 0 else "Message (continued)",
                value=message[offset : offset + 950],
                inline=False,
            )
        embed.set_footer(
            text="Times use your Discord timezone. Editing changes the reminder time directly."
            if event
            else "Times use your Discord timezone. Nothing is saved until you confirm."
        )
        view = EventPreview(self, interaction.user.id, interaction.channel_id, draft, embed)
        await interaction.response.send_message(
            embed=embed,
            view=view,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        view.message = await interaction.original_response()

    @staticmethod
    def _channel_permission_problems(interaction, role_names) -> tuple[str | None, str | None]:
        """(blocking problem, warning) about the bot's permissions where the reminder will post.

        Slash commands work in channels the bot cannot write to, so without this check
        a reminder could be saved that can never be delivered.
        """
        channel = getattr(interaction, "channel", None)
        me = getattr(getattr(interaction, "guild", None), "me", None)
        permissions_for = getattr(channel, "permissions_for", None)
        if me is None or not callable(permissions_for):
            return None, None
        try:
            permissions = permissions_for(me)
        except Exception:
            # For example a thread whose parent channel is not cached: do not block.
            return None, None
        # Posting in a thread needs "Send Messages in Threads" of the parent channel.
        send_bit = "send_messages_in_threads" if isinstance(channel, discord.Thread) else "send_messages"
        if getattr(permissions, "view_channel", True) is False or getattr(permissions, send_bit, True) is False:
            return (
                "I can't post in this channel. Give me **View Channel** and **Send Messages** here, "
                "or schedule the reminder in a channel I can write to.",
                None,
            )
        pings_everyone = any(
            str(name).strip().casefold() in EVERYONE_NAMES or str(name).strip().casefold() in HERE_NAMES
            for name in role_names or ("everyone",)
        )
        if pings_everyone and getattr(permissions, "mention_everyone", True) is False:
            return None, (
                "I don't have **Mention @everyone, @here and All Roles** in this channel, so the reminder "
                "will be posted without notifying anyone. Grant the permission to get a real ping."
            )
        return None, None

    async def _handle_list_events(self, interaction: discord.Interaction):
        if interaction.channel_id is None:
            await self._send_error(interaction, "Use /reminders in a channel.", title="Events unavailable")
            return
        # Defer privately so errors stay private; the panel itself is posted publicly.
        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            events = await self._scheduler_service.get_events_for_channel(interaction.channel_id)
        except Exception:
            logger.exception("Could not load events for channel %s", interaction.channel_id)
            await self._send_error(
                interaction,
                "Scheduled events could not be loaded right now. Try /reminders again in a moment.",
                title="Events unavailable",
            )
            return
        if not events:
            await interaction.followup.send(
                embed=build_status_embed(
                    title="No scheduled events",
                    description="There are no active reminders in this channel. Server managers can use /remind to create one.",
                ),
                ephemeral=True,
            )
            return
        view = EventListView(
            self,
            interaction.user.id,
            interaction.channel_id,
            events,
            await self._access.can_manage(interaction),
        )
        await interaction.edit_original_response(content="Events ready.")
        view.message = await interaction.followup.send(
            embed=view.embed(),
            view=view,
            wait=True,
            ephemeral=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        try:
            await interaction.delete_original_response()
        except discord.HTTPException:
            logger.debug("Could not remove the private events receipt", exc_info=True)

    # ------------------------------------------------------------------ scheduler

    def start_scheduler_task(self):
        if self._scheduler_loop and self._scheduler_loop.is_running():
            return
        self._scheduler_stopped = False

        @tasks.loop(minutes=1)
        async def check_scheduled_events():
            await self.run_scheduler_iteration()

        @check_scheduled_events.error
        async def on_scheduler_error(error: Exception):
            # Safety net: iterations catch their own errors, but never let the loop die.
            self._note_failure(f"Scheduler loop stopped unexpectedly: {error!r}")
            logger.error(
                "Event scheduler loop crashed; restarting in %.0fs",
                self.SCHEDULER_RESTART_DELAY_SECONDS,
                exc_info=(type(error), error, error.__traceback__),
            )
            asyncio.get_running_loop().call_later(self.SCHEDULER_RESTART_DELAY_SECONDS, self._restart_scheduler)

        self._scheduler_loop = check_scheduled_events
        check_scheduled_events.start()

    def stop_scheduler_task(self):
        self._scheduler_stopped = True
        if self._scheduler_loop is not None:
            self._scheduler_loop.cancel()

    async def shutdown_scheduler(self, timeout: float = 10.0) -> None:
        """Stop the scheduler, letting a running pass finish first.

        Cancelling between "the ping was posted" and "the delivery was recorded" would
        make the restarted bot post the same @everyone ping again.
        """
        self._scheduler_stopped = True
        deadline = asyncio.get_running_loop().time() + timeout
        while self._iteration_active and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.05)
        if self._scheduler_loop is not None:
            self._scheduler_loop.cancel()

    def _restart_scheduler(self):
        if self._scheduler_stopped or self._scheduler_loop is None or self._scheduler_loop.is_running():
            return
        try:
            self._scheduler_loop.start()
            logger.info("Event scheduler loop restarted")
        except RuntimeError:
            logger.exception("Could not restart the event scheduler loop")

    def is_scheduler_running(self) -> bool:
        return bool(self._scheduler_loop and self._scheduler_loop.is_running())

    def scheduler_health(self) -> SchedulerHealth:
        return SchedulerHealth(
            running=self.is_scheduler_running(),
            last_run_at=self._last_run_at,
            last_success_at=self._last_success_at,
            last_error=self._last_error,
            last_error_at=self._last_error_at,
            failed_count=self._failed_count,
            sent_count=self._sent_count,
            missed_count=self._missed_count,
        )

    def _note_failure(self, message: str):
        self._failed_count += 1
        self._last_error = message[:500]
        self._last_error_at = datetime.now(timezone.utc)

    async def run_scheduler_iteration(self) -> None:
        """One scheduler pass. Never raises: every failure is logged and recorded."""
        self._iteration_active = True
        try:
            await self._run_scheduler_iteration()
        finally:
            self._iteration_active = False

    async def _run_scheduler_iteration(self) -> None:
        self._last_run_at = datetime.now(timezone.utc)
        try:
            batch = await self._scheduler_service.claim_due_events(self.GRACE_PERIOD)
        except Exception as error:
            self._note_failure(f"Could not load due reminders: {error}")
            logger.exception("Could not load due reminders; retrying next minute")
            return
        for event in batch.missed:
            self._missed_count += 1
            logger.warning(
                "Reminder #%s in channel %s was not posted: %s",
                event.id,
                event.channel_id,
                event.last_error,
            )
        due_keys = {(event.id, event.event_time) for event in batch.due}
        self._delivered_pending = {key: value for key, value in self._delivered_pending.items() if key in due_keys}
        for event in batch.due:
            if self._scheduler_stopped and self._scheduler_loop is not None:
                # Shutting down: leave the rest due; they are sent after the restart.
                break
            try:
                await self._deliver(event)
            except Exception as error:
                self._note_failure(f"Reminder #{event.id}: {error}")
                logger.exception("Unexpected failure while delivering reminder #%s", event.id)
        self._last_success_at = datetime.now(timezone.utc)

    async def _deliver(self, event: ScheduledEvent) -> None:
        key = (event.id, event.event_time)
        outcome = self._delivered_pending.get(key)
        if outcome is None:
            outcome = await self._attempt_send(event)
            if outcome.delivered:
                self._sent_count += 1
                self._delivered_pending[key] = outcome
            else:
                self._note_failure(f"Reminder #{event.id}: {outcome.error}")
                log = logger.warning if outcome.retry else logger.error
                log(
                    "Reminder #%s in channel %s failed (%s): %s",
                    event.id,
                    event.channel_id,
                    "will retry" if outcome.retry else "not retrying",
                    outcome.error,
                )
        try:
            await self._scheduler_service.record_delivery(
                event,
                delivered=outcome.delivered,
                error=outcome.error,
                retry=outcome.retry,
                deactivate=outcome.deactivate,
            )
        except Exception as error:
            self._note_failure(f"Could not record reminder #{event.id}: {error}")
            logger.exception("Could not record delivery of reminder #%s; retrying next minute", event.id)
            return
        self._delivered_pending.pop(key, None)

    async def _resolve_channel(self, channel_id: int | None):
        """Return (channel, failure outcome)."""
        if channel_id is None:
            return None, DeliveryOutcome(False, "Reminder has no channel.", retry=False)
        channel = self._bot.get_channel(channel_id)
        if channel is not None:
            return channel, None
        try:
            return await self._bot.fetch_channel(channel_id), None
        except discord.NotFound:
            return None, DeliveryOutcome(False, "The channel no longer exists.", deactivate=True)
        except discord.Forbidden:
            return None, DeliveryOutcome(False, "The bot can no longer access this channel.", retry=False)
        except discord.HTTPException as error:
            return None, self._http_failure(error, "loading the channel")
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as error:
            return None, DeliveryOutcome(False, f"Network error while loading the channel: {error}", retry=True)

    @staticmethod
    def _http_failure(error: discord.HTTPException, action: str) -> DeliveryOutcome:
        status = getattr(error, "status", 0) or 0
        transient = status >= 500 or status == 429
        detail = getattr(error, "text", "") or str(error)
        return DeliveryOutcome(False, f"Discord error {status} while {action}: {detail}", retry=transient)

    async def _attempt_send(self, event: ScheduledEvent) -> DeliveryOutcome:
        channel, failure = await self._resolve_channel(event.channel_id)
        if failure is not None:
            return failure
        if not hasattr(channel, "send"):
            return DeliveryOutcome(False, "This channel cannot receive messages.", retry=False)
        try:
            warning = await self._send_event_notification(channel, event.role_names, event.message)
        except discord.Forbidden:
            return DeliveryOutcome(False, "Missing permission to send messages in this channel.", retry=False)
        except discord.NotFound:
            return DeliveryOutcome(False, "The channel no longer exists.", deactivate=True)
        except discord.HTTPException as error:
            return self._http_failure(error, "sending the reminder")
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as error:
            return DeliveryOutcome(False, f"Network error while sending the reminder: {error}", retry=True)
        except Exception as error:
            logger.exception("Unexpected error while sending reminder #%s", event.id)
            return DeliveryOutcome(False, f"Unexpected error while sending the reminder: {error}", retry=True)
        return DeliveryOutcome(True, warning)

    @staticmethod
    def _find_role(guild, name: str):
        roles = list(getattr(guild, "roles", None) or ())
        bare = name.strip()
        if bare.startswith("<@&") and bare.endswith(">"):
            bare = bare[3:-1]
        if bare.isdigit():
            match = next((role for role in roles if getattr(role, "id", None) == int(bare)), None)
            if match is not None:
                return match
        bare = bare.lstrip("@")
        exact = next((role for role in roles if getattr(role, "name", None) == bare), None)
        if exact is not None:
            return exact
        folded = bare.casefold()
        return next((role for role in roles if str(getattr(role, "name", "")).casefold() == folded), None)

    def _resolve_mentions(self, guild, role_names: list[str]):
        """Return (mention text, allowed mentions, missing role names)."""
        names = [str(name).strip() for name in role_names or () if str(name).strip()] or ["everyone"]
        parts, roles, missing = [], [], []
        everyone = False
        for name in names:
            lowered = name.casefold()
            if lowered in EVERYONE_NAMES or lowered in HERE_NAMES:
                everyone = True
                parts.append("@here" if lowered in HERE_NAMES else "@everyone")
                continue
            role = self._find_role(guild, name)
            if role is None:
                missing.append(name)
            elif getattr(role, "is_default", lambda: False)():
                everyone = True
                parts.append("@everyone")
            elif role not in roles:
                roles.append(role)
                parts.append(role.mention)
        allowed = discord.AllowedMentions(
            everyone=everyone,
            users=False,
            roles=roles if roles else False,
            replied_user=False,
        )
        return " ".join(dict.fromkeys(parts)), allowed, missing

    async def _send_event_notification(
        self,
        channel: discord.abc.Messageable,
        role_names: list[str],
        message: str,
    ) -> Optional[str]:
        """Post a reminder pinging its roles (or @everyone). Returns a warning, if any."""
        mentions, allowed, missing = self._resolve_mentions(getattr(channel, "guild", None), role_names)
        content = f"{mentions}\n{message}" if mentions else message
        chunks = _chunk_message(content)
        # The first chunk carries the ping; failures there propagate so the send is retried.
        await channel.send(chunks[0], allowed_mentions=allowed)
        warnings = []
        for chunk in chunks[1:]:
            try:
                await channel.send(chunk, allowed_mentions=discord.AllowedMentions.none())
            except Exception as error:
                # Once the ping went out, a retry would ping everyone again: treat the
                # reminder as delivered and record the partial post instead.
                logger.warning("Reminder continuation could not be posted: %s", error)
                warnings.append(f"Only part of the reminder was posted: {error}")
                break
        if missing:
            warnings.append(f"Role(s) not found, so they were not pinged: {', '.join(missing)}")
        return " ".join(warnings)[:500] or None

    @staticmethod
    def _format_discord_timestamp(value: datetime) -> str:
        unix_ts = int(value.timestamp())
        return f"<t:{unix_ts}:F> (<t:{unix_ts}:R>)"

    @staticmethod
    def _format_repeat(repeat_every_days: Optional[int]) -> str:
        if repeat_every_days is None:
            return "Does not repeat"
        interval = "Every day" if repeat_every_days == 1 else f"Every {repeat_every_days} days"
        return f"{interval} at the same UTC time. Local time may shift with daylight saving."
