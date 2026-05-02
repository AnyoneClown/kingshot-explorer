from datetime import datetime, timezone
from typing import List
import logging

import discord
from discord import app_commands
from discord.ext import commands, tasks

from services.event_scheduler_service import IEventSchedulerService

logger = logging.getLogger(__name__)


class EventHandler:
    """Handles event scheduling Discord commands."""

    def __init__(self, scheduler_service: IEventSchedulerService, bot: commands.Bot):
        """
        Initialize event handler.

            scheduler_service: Service for managing scheduled events
            bot: Discord bot instance
        """
        self._scheduler_service = scheduler_service
        self._bot = bot
        self._scheduler_loop = None

    def register_commands(self):
        """Register all event scheduling commands with the bot."""

        @self._bot.tree.command(
            name="schedule", description="Schedule an @everyone ping 10 minutes before the specified time"
        )
        @app_commands.describe(
            date="Event date (YYYY-MM-DD)", time="Event time (HH:MM in UTC)", message="Event message"
        )
        async def schedule_event(interaction: discord.Interaction, date: str, time: str, message: str):
            """Schedule an @everyone ping 10 minutes before the specified time."""
            await self._handle_schedule_event(interaction, date, time, message)

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
            due_events = self._scheduler_service.check_and_get_due_events()

            for channel_id, events in due_events.items():
                channel = self._bot.get_channel(channel_id)
                if not channel:
                    continue

                for event_time, role_names, message in events:
                    await self._send_event_notification(channel, role_names, message)

        self._scheduler_loop = check_scheduled_events
        check_scheduled_events.start()

    def is_scheduler_running(self) -> bool:
        """Return whether the scheduler loop is currently active."""
        return bool(self._scheduler_loop and self._scheduler_loop.is_running())

    async def _handle_schedule_event(self, interaction: discord.Interaction, date: str, time: str, message: str):
        """Handle scheduling a new event."""
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

            notification_time = event_time - timedelta(minutes=10)

            if notification_time <= now_utc:
                await interaction.followup.send(
                    embed=self._build_status_embed(
                        title="⏱️ Event Too Soon",
                        description="Event time must be at least 10 minutes from now.",
                        color=discord.Color.orange(),
                    )
                )
                return

            success = self._scheduler_service.schedule_event(
                interaction.channel.id, notification_time, ["everyone"], cleaned_message
            )

            if success:
                embed = self._build_status_embed(
                    title="✅ Event Scheduled",
                    description="Your reminder is set and will ping @everyone 10 minutes before the event.",
                    color=discord.Color.green(),
                )
                embed.add_field(name="Event Time (UTC)", value=self._format_discord_timestamp(event_time), inline=False)
                embed.add_field(
                    name="Reminder Time (UTC)",
                    value=self._format_discord_timestamp(notification_time),
                    inline=False,
                )
                embed.add_field(name="Message", value=cleaned_message[:900], inline=False)
                embed.set_footer(text="Tip: Use /events to review scheduled reminders")

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
                    description="Use `YYYY-MM-DD` for date and `HH:MM` for UTC time. Example: `2026-04-01` and `18:30`.",
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

        events = self._scheduler_service.get_events_for_channel(interaction.channel.id)

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
        for idx, (event_time, role_names, message) in enumerate(events[:max_items], 1):
            message_preview = (message[:100] + "...") if len(message) > 100 else message
            event_lines.append(
                f"**{idx}.** {self._format_discord_timestamp(event_time)}\n"
                f"Message: {message_preview}"
            )

        if len(events) > max_items:
            event_lines.append(f"... and {len(events) - max_items} more event(s)")

        embed.add_field(name="Upcoming", value="\n\n".join(event_lines), inline=False)
        embed.set_footer(text="Use /cancel <number> to remove an event")

        await interaction.followup.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    async def _handle_cancel_event(self, interaction: discord.Interaction, event_number: int):
        """Handle cancelling a scheduled event."""
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

            if self._scheduler_service.cancel_event(interaction.channel.id, index):
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
