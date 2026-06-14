from datetime import datetime, timezone
from typing import List, Optional
import logging

import discord
from discord import app_commands
from discord.ext import commands, tasks

from services.event_scheduler_service import IEventSchedulerService, ScheduledEvent

logger = logging.getLogger(__name__)


class EventHandler:
    """Handles event scheduling Discord commands."""

    def __init__(
        self,
        scheduler_service: IEventSchedulerService,
        bot: commands.Bot,
        admin_user_ids: set[int] | None = None,
    ):
        """
        Initialize event handler.

            scheduler_service: Service for managing scheduled events
            bot: Discord bot instance
        """
        self._scheduler_service = scheduler_service
        self._bot = bot
        self._admin_user_ids = admin_user_ids or set()
        self._scheduler_loop = None

    def _is_bot_admin(self, interaction: discord.Interaction) -> bool:
        return int(interaction.user.id) in self._admin_user_ids

    async def _send_admin_only_response(self, interaction: discord.Interaction) -> None:
        embed = self._build_status_embed(
            title="⛔ Admin Only",
            description="Only configured bot admins can use this command.",
            color=discord.Color.orange(),
        )
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)

    def register_commands(self):
        """Register all event scheduling commands with the bot."""

        @self._bot.tree.command(
            name="schedule", description="Schedule a one-time or recurring @everyone reminder"
        )
        @app_commands.describe(
            date="Event date (YYYY-MM-DD)",
            time="Event time (HH:MM in UTC)",
            message="Reminder message",
            repeat_every_days="Optional: repeat every N days, for example 2 means once every 2 days",
            reminder_minutes="How many minutes before the event to send the reminder (default: 10)",
        )
        async def schedule_event(
            interaction: discord.Interaction,
            date: str,
            time: str,
            message: str,
            repeat_every_days: Optional[int] = None,
            reminder_minutes: int = 10,
        ):
            """Schedule an @everyone ping before the specified time."""
            await self._handle_schedule_event(interaction, date, time, message, repeat_every_days, reminder_minutes)

        @self._bot.tree.command(name="events", description="List all scheduled events for this channel")
        async def list_events(interaction: discord.Interaction):
            """List all scheduled events for this channel."""
            await self._handle_list_events(interaction)

        @self._bot.tree.command(name="cancel", description="Cancel a scheduled event")
        @app_commands.describe(event_number="Event number from /events command")
        async def cancel_event(interaction: discord.Interaction, event_number: int):
            """Cancel a scheduled event. Use /events to see event numbers."""
            await self._handle_cancel_event(interaction, event_number)

    def start_scheduler_task(self):
        """Start the background task that checks for due events."""
        if self._scheduler_loop and self._scheduler_loop.is_running():
            logger.info("Event scheduler task already running; skipping duplicate start")
            return

        @tasks.loop(minutes=1)
        async def check_scheduled_events():
            """Check for scheduled events and ping roles when it's time."""
            due_events = await self._scheduler_service.check_and_get_due_events()

            for channel_id, events in due_events.items():
                channel = self._bot.get_channel(channel_id)
                if not channel:
                    continue

                for event in events:
                    await self._send_event_notification(channel, event.role_names, event.message)

        self._scheduler_loop = check_scheduled_events
        check_scheduled_events.start()

    def is_scheduler_running(self) -> bool:
        """Return whether the scheduler loop is currently active."""
        return bool(self._scheduler_loop and self._scheduler_loop.is_running())

    async def _handle_schedule_event(
        self,
        interaction: discord.Interaction,
        date: str,
        time: str,
        message: str,
        repeat_every_days: Optional[int],
        reminder_minutes: int,
    ):
        """Handle scheduling a new event."""
        if not self._is_bot_admin(interaction):
            await self._send_admin_only_response(interaction)
            return

        await interaction.response.defer(thinking=True)

        try:
            from datetime import timedelta

            cleaned_message = (message or "").strip()
            if not cleaned_message:
                await interaction.followup.send(
                    embed=self._build_status_embed(
                        title="⚠️ Message Required",
                        description="Please provide a reminder message so members know what the event is for.",
                        color=discord.Color.orange(),
                    )
                )
                return

            if reminder_minutes < 1 or reminder_minutes > 1440:
                await interaction.followup.send(
                    embed=self._build_status_embed(
                        title="⚠️ Invalid Reminder Lead Time",
                        description="Reminder lead time must be between 1 minute and 1440 minutes (24 hours).",
                        color=discord.Color.orange(),
                    )
                )
                return

            if repeat_every_days is not None and (repeat_every_days < 1 or repeat_every_days > 365):
                await interaction.followup.send(
                    embed=self._build_status_embed(
                        title="⚠️ Invalid Repeat Interval",
                        description="Repeat interval must be between 1 and 365 days. Example: `2` for once every 2 days.",
                        color=discord.Color.orange(),
                    )
                )
                return

            datetime_str = f"{date} {time}"
            event_time = datetime.strptime(datetime_str, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)

            now_utc = datetime.now(timezone.utc)

            if event_time <= now_utc:
                await interaction.followup.send(
                    embed=self._build_status_embed(
                        title="❌ Invalid Event Time",
                        description="You cannot schedule an event in the past.",
                        color=discord.Color.red(),
                    )
                )
                return

            notification_time = event_time - timedelta(minutes=reminder_minutes)

            if notification_time <= now_utc:
                await interaction.followup.send(
                    embed=self._build_status_embed(
                        title="⏱️ Event Too Soon",
                        description=f"Event time must be at least {reminder_minutes} minute(s) from now.",
                        color=discord.Color.orange(),
                    )
                )
                return

            success = await self._scheduler_service.schedule_event(
                interaction.channel.id,
                notification_time,
                ["everyone"],
                cleaned_message,
                repeat_every_days=repeat_every_days,
            )

            if success:
                embed = self._build_status_embed(
                    title="✅ Event Scheduled",
                    description=self._schedule_success_description(reminder_minutes, repeat_every_days),
                    color=discord.Color.green(),
                )
                embed.add_field(name="First Event", value=self._format_discord_timestamp(event_time), inline=False)
                embed.add_field(
                    name="First Reminder",
                    value=self._format_discord_timestamp(notification_time),
                    inline=True,
                )
                embed.add_field(name="Repeat", value=self._format_repeat(repeat_every_days), inline=True)
                embed.add_field(name="Message", value=cleaned_message[:900], inline=False)
                embed.set_footer(text="Use /events to review reminders or /cancel with the listed number")

                await interaction.followup.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
            else:
                await interaction.followup.send(
                    embed=self._build_status_embed(
                        title="❌ Scheduling Failed",
                        description="The reminder could not be saved. Please try again.",
                        color=discord.Color.red(),
                    )
                )
        except ValueError:
            await interaction.followup.send(
                embed=self._build_status_embed(
                    title="🧭 Invalid Date/Time Format",
                    description=(
                        "Use `YYYY-MM-DD` for date and `HH:MM` for UTC time.\n"
                        "Example: `/schedule date:2026-04-01 time:18:30 message:Alliance prep repeat_every_days:2`"
                    ),
                    color=discord.Color.orange(),
                )
            )
        except Exception as e:
            logger.error(f"Error scheduling event: {e}", exc_info=True)
            await interaction.followup.send(
                embed=self._build_status_embed(
                    title="❌ Unexpected Error",
                    description="An unexpected error occurred while scheduling the event.",
                    color=discord.Color.red(),
                )
            )

    async def _handle_list_events(self, interaction: discord.Interaction):
        """Handle listing all scheduled events for a channel."""
        await interaction.response.defer(thinking=True)

        events = await self._scheduler_service.get_events_for_channel(interaction.channel.id)

        if not events:
            await interaction.followup.send(
                embed=self._build_status_embed(
                    title="📭 No Scheduled Events",
                    description="There are no active reminders in this channel.",
                    color=discord.Color.blue(),
                )
            )
            return

        embed = self._build_status_embed(
            title="🗓️ Scheduled Events",
            description=f"Found **{len(events)}** event(s) in this channel.",
            color=discord.Color.blurple(),
        )

        max_items = 15
        event_lines = []
        for idx, event in enumerate(events[:max_items], 1):
            event_lines.append(self._format_event_list_item(idx, event))

        if len(events) > max_items:
            event_lines.append(f"... and {len(events) - max_items} more event(s)")

        embed.add_field(name="Upcoming", value="\n\n".join(event_lines), inline=False)
        embed.set_footer(text="Use /cancel <number> to remove an event")

        await interaction.followup.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    async def _handle_cancel_event(self, interaction: discord.Interaction, event_number: int):
        """Handle cancelling a scheduled event."""
        if not self._is_bot_admin(interaction):
            await self._send_admin_only_response(interaction)
            return

        await interaction.response.defer(thinking=True)

        try:
            if event_number <= 0:
                await interaction.followup.send(
                    embed=self._build_status_embed(
                        title="⚠️ Invalid Event Number",
                        description="Event number must be greater than 0.",
                        color=discord.Color.orange(),
                    )
                )
                return

            index = event_number - 1

            if await self._scheduler_service.cancel_event(interaction.channel.id, index):
                await interaction.followup.send(
                    embed=self._build_status_embed(
                        title="✅ Event Cancelled",
                        description=f"Removed event **#{event_number}** from this channel.",
                        color=discord.Color.green(),
                    )
                )
            else:
                await interaction.followup.send(
                    embed=self._build_status_embed(
                        title="❌ Event Not Found",
                        description=f"Event **#{event_number}** does not exist. Use `/events` to list available events.",
                        color=discord.Color.red(),
                    )
                )
        except Exception as e:
            logger.error(f"Error cancelling event: {e}", exc_info=True)
            await interaction.followup.send(
                embed=self._build_status_embed(
                    title="❌ Unexpected Error",
                    description="An error occurred while cancelling the event.",
                    color=discord.Color.red(),
                )
            )

    async def _extract_role_names(self, guild: discord.Guild, args: tuple) -> List[str]:
        """Extract role names from command arguments (supports both @mentions and plain text)."""
        role_names = []
        for arg in args:
            if arg.startswith("<@&"):
                role_id = arg.strip("<@&>")
                role = discord.utils.get(guild.roles, id=int(role_id))
                if role:
                    role_names.append(role.name)
            else:
                role = discord.utils.get(guild.roles, name=arg)
                if role:
                    role_names.append(arg)
                    break
        return role_names

    def _extract_message(self, args: tuple) -> str:
        """Extract message text from command arguments."""
        message_parts = []
        found_role = False
        for arg in args:
            if not found_role and not arg.startswith("<@&"):
                found_role = True
                continue
            if found_role and not arg.startswith("<@&"):
                message_parts.append(arg)
        return " ".join(message_parts) if message_parts else "Event reminder!"

    async def _send_event_notification(self, channel: discord.TextChannel, role_names: List[str], message: str):
        """Send event notification with @everyone ping."""
        notification = f"@everyone\n{message}"
        await channel.send(
            notification,
            allowed_mentions=discord.AllowedMentions(everyone=True, users=False, roles=False),
        )

    @staticmethod
    def _build_status_embed(title: str, description: str, color: discord.Color) -> discord.Embed:
        """Build a consistent status embed for event command responses."""
        return discord.Embed(title=title, description=description, color=color)

    @staticmethod
    def _format_discord_timestamp(value: datetime) -> str:
        """Format a datetime for absolute + relative Discord display."""
        unix_ts = int(value.timestamp())
        return f"<t:{unix_ts}:F> (<t:{unix_ts}:R>)"

    @staticmethod
    def _format_repeat(repeat_every_days: Optional[int]) -> str:
        """Format recurrence for Discord embeds."""
        if repeat_every_days is None:
            return "Does not repeat"
        if repeat_every_days == 1:
            return "Every day"
        return f"Every {repeat_every_days} days"

    def _format_event_list_item(self, idx: int, event: ScheduledEvent) -> str:
        """Format a scheduled event for the /events list."""
        message_preview = (event.message[:100] + "...") if len(event.message) > 100 else event.message
        return (
            f"**{idx}.** Next reminder: {self._format_discord_timestamp(event.event_time)}\n"
            f"Repeat: {self._format_repeat(event.repeat_every_days)}\n"
            f"Message: {message_preview}"
        )

    def _schedule_success_description(self, reminder_minutes: int, repeat_every_days: Optional[int]) -> str:
        """Build the success text for scheduled reminders."""
        repeat_text = self._format_repeat(repeat_every_days).lower()
        if repeat_every_days is None:
            return f"Your reminder is set and will ping @everyone {reminder_minutes} minute(s) before the event."
        return (
            f"Your reminder is set and will ping @everyone {reminder_minutes} minute(s) before the event, "
            f"then repeat {repeat_text}."
        )
