from datetime import datetime, timedelta, timezone
import logging
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
from discord import app_commands
from discord.ext import commands, tasks

from handlers.ui import EmbedColors, OwnedView, build_status_embed
from services.event_scheduler_service import IEventSchedulerService, ScheduledEvent

logger = logging.getLogger(__name__)


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
            "The reminder could not be prepared. Reopen /schedule or /events.",
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
        try:
            event = self.event
            if event.id is None:
                saved = await self.handler._scheduler_service.schedule_event(
                    self.channel_id,
                    event.event_time,
                    event.role_names,
                    event.message,
                    event.repeat_every_days,
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
            self.saving = False
            raise
        if not saved:
            self.saving = False
            await self.handler._send_error(
                interaction,
                "This reminder is no longer active or its time has passed. Reopen /events or /schedule.",
            )
            return
        self.stop()
        self.embed.title = "Reminder updated" if event.id is not None else "Event scheduled"
        self.embed.description = "Saved. Use /events to edit or cancel this reminder."
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
    def __init__(
        self,
        handler,
        author_id: int,
        channel_id: int,
        events: list[ScheduledEvent],
        can_manage: bool,
    ):
        super().__init__(author_id)
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
        message = event.message[:1900]
        for offset in range(0, len(message), 950):
            embed.add_field(
                name="Message" if offset == 0 else "Message (continued)",
                value=message[offset : offset + 950],
                inline=False,
            )
        embed.set_footer(
            text=f"Event {self.index + 1}/{len(self.events)} · Times use your Discord timezone · /cancel event_id:{event.id}"
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
            await self.handler._send_error(interaction, "This event is no longer active. Reopen /events.")
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
        await interaction.response.defer()
        saved = await self.handler._scheduler_service.cancel_event(self.channel_id, event_id)
        if not saved:
            await self.handler._send_error(interaction, "This event is no longer active. Reopen /events.")
            return
        self.events = await self.handler._scheduler_service.get_events_for_channel(self.channel_id)
        if self.events:
            self.index = min(self.index, len(self.events) - 1)
            self._refresh_controls()
            embed = self.embed()
            embed.description = f"Event #{event_id} cancelled. {len(self.events)} active reminder(s) remain."
            await interaction.edit_original_response(
                embed=embed, view=self, allowed_mentions=discord.AllowedMentions.none()
            )
        else:
            self.stop()
            await interaction.edit_original_response(
                embed=build_status_embed(
                    title="Event cancelled",
                    description="There are no active reminders in this channel.",
                    color=EmbedColors.SUCCESS,
                ),
                view=None,
            )

    @discord.ui.button(label="Create event", row=2)
    async def create(self, interaction: discord.Interaction, button: discord.ui.Button):
        if await self.handler._can_manage(interaction, self.author_id, self.channel_id):
            await self.handler.open_schedule_form(interaction)


class EventHandler:
    """Handles event scheduling Discord commands."""

    def __init__(
        self,
        scheduler_service: IEventSchedulerService,
        bot: commands.Bot,
        admin_user_ids: set[int] | None = None,
    ):
        self._scheduler_service, self._bot = scheduler_service, bot
        self._admin_user_ids = admin_user_ids or set()
        self._scheduler_loop = None

    def _is_bot_admin(self, interaction: discord.Interaction) -> bool:
        return int(interaction.user.id) in self._admin_user_ids

    async def _send_error(self, interaction: discord.Interaction, description: str):
        send = interaction.followup.send if interaction.response.is_done() else interaction.response.send_message
        await send(
            embed=build_status_embed(
                title="Reminder not saved",
                description=description,
                color=EmbedColors.WARNING,
            ),
            ephemeral=True,
        )

    async def _send_admin_only_response(self, interaction: discord.Interaction):
        send = interaction.followup.send if interaction.response.is_done() else interaction.response.send_message
        await send(
            embed=build_status_embed(
                title="Admin only",
                description="Only configured bot admins can manage reminders.",
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
        if not self._is_bot_admin(interaction):
            await self._send_admin_only_response(interaction)
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
            name="schedule",
            description="Open an event form, or preview a reminder using options",
        )
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
            name="events",
            description="Browse scheduled events and manage reminders in this channel",
        )
        async def list_events(interaction: discord.Interaction):
            await self._handle_list_events(interaction)

        @self._bot.tree.command(name="cancel", description="Cancel a scheduled event by its stable ID")
        @app_commands.describe(event_id="Event ID shown on its /events card")
        async def cancel_event(interaction: discord.Interaction, event_id: str):
            await self._handle_cancel_event(interaction, event_id)

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
        draft = ScheduledEvent(
            event.id if event else None,
            reminder_time,
            event.role_names if event else ["everyone"],
            message,
            repeat_every_days,
        )
        embed = build_status_embed(
            title="Preview reminder changes" if event else "Preview event",
            description="Review the times and message, then save. The reminder will ping @everyone.",
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

    async def _handle_list_events(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)
        if interaction.channel_id is None:
            await self._send_error(interaction, "Use /events in a channel.")
            return
        events = await self._scheduler_service.get_events_for_channel(interaction.channel_id)
        if not events:
            await interaction.followup.send(
                embed=build_status_embed(
                    title="No scheduled events",
                    description="There are no active reminders in this channel. Bot admins can use /schedule to create one.",
                )
            )
            return
        view = EventListView(
            self,
            interaction.user.id,
            interaction.channel_id,
            events,
            self._is_bot_admin(interaction),
        )
        view.message = await interaction.followup.send(
            embed=view.embed(),
            view=view,
            wait=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _handle_cancel_event(self, interaction: discord.Interaction, event_id: str):
        if not await self._can_manage(interaction):
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            event_id = int(event_id)
            if event_id < 1 or not await self._scheduler_service.cancel_event(interaction.channel_id, event_id):
                await self._send_error(
                    interaction,
                    "This event is no longer active in this channel. Use the event ID on its /events card.",
                )
                return
            await interaction.followup.send(
                embed=build_status_embed(
                    title="Event cancelled",
                    description=f"Event #{event_id} was cancelled.",
                    color=EmbedColors.SUCCESS,
                ),
                ephemeral=True,
            )
        except ValueError:
            await self._send_error(interaction, "Copy the numeric event ID shown on its /events card.")
        except Exception:
            logger.exception("Error cancelling event")
            await self._send_error(interaction, "This reminder could not be cancelled. Try again.")

    def start_scheduler_task(self):
        if self._scheduler_loop and self._scheduler_loop.is_running():
            return

        @tasks.loop(minutes=1)
        async def check_scheduled_events():
            due_events = await self._scheduler_service.check_and_get_due_events()
            for channel_id, events in due_events.items():
                channel = self._bot.get_channel(channel_id)
                if channel:
                    for event in events:
                        await self._send_event_notification(channel, event.role_names, event.message)

        self._scheduler_loop = check_scheduled_events
        check_scheduled_events.start()

    def is_scheduler_running(self) -> bool:
        return bool(self._scheduler_loop and self._scheduler_loop.is_running())

    async def _send_event_notification(self, channel: discord.TextChannel, role_names: list[str], message: str):
        await channel.send(
            f"@everyone\n{message}",
            allowed_mentions=discord.AllowedMentions(everyone=True, users=False, roles=False),
        )

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
