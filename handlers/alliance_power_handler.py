"""Daily alliance and member power reports."""

import logging
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import tasks

from handlers.ui import EmbedColors, OwnedView, build_status_embed, send_ui_error

logger = logging.getLogger(__name__)


def _change(current: int | None, baseline: int | None) -> str:
    if current is None:
        return "Unavailable"
    if baseline is None:
        return "New history"
    difference = current - baseline
    percentage = f"{difference / baseline:+.1%}" if baseline else "percentage unavailable: baseline 0"
    return f"{difference:+,} ({percentage})"


class AlliancePowerView(OwnedView):
    def __init__(self, handler, guild_id: int, report: dict, author_id: int):
        super().__init__(author_id)
        self.handler = handler
        self.guild_id = guild_id
        self.report = report
        self.page = 0
        self.showing_members = False
        latest = report["latest"]
        self.members = sorted(
            (latest.members if latest else {}).items(),
            key=lambda pair: (pair[1].get("power") is None, -(pair[1].get("power") or 0), pair[0]),
        )
        if author_id not in handler.admin_user_ids:
            self.remove_item(self.stop_button)
        self._update_controls()

    def _update_controls(self):
        self.members_button.disabled = self.showing_members or not self.members
        self.summary_button.disabled = not self.showing_members
        self.previous_button.disabled = not self.showing_members or self.page == 0
        self.next_button.disabled = not self.showing_members or (self.page + 1) * 10 >= len(self.members)

    def build_embed(self) -> discord.Embed:
        latest = self.report["latest"]
        embed = build_status_embed(title=f"Alliance power · Kingdom {self.report['kingdom_id']}", description="")
        if latest is None:
            embed.description = "Waiting for the first complete snapshot."
            return embed
        name = discord.utils.escape_markdown(str(latest.alliance_name or "Alliance")[:100])
        tag = discord.utils.escape_markdown(str(latest.alliance_tag or "")[:20])
        captured = int(latest.captured_at.timestamp())
        embed.description = f"**[{tag}] {name}**\nCaptured <t:{captured}:f> (<t:{captured}:R>)"
        embed.timestamp = latest.captured_at
        if latest.snapshot_date < datetime.now(timezone.utc).date():
            embed.description += "\nLatest snapshot is from an earlier UTC day."
            embed.color = EmbedColors.WARNING
        daily, weekly = self.report["daily"], self.report["weekly"]
        if self.showing_members:
            for uid, member in self.members[self.page * 10:(self.page + 1) * 10]:
                power = member.get("power")
                name = discord.utils.escape_markdown(str(member.get("name") or "Unknown player")[:90])
                changes = []
                for label, baseline in (("Daily", daily), ("Weekly", weekly)):
                    old_power = (baseline.members.get(uid) or {}).get("power") if baseline else None
                    changes.append(f"{label}: {_change(power, old_power)}")
                embed.add_field(
                    name=name,
                    value=f"Power: {power:,}\n" + "\n".join(changes) if power is not None
                    else "Power: Unavailable\n" + "\n".join(changes),
                    inline=False,
                )
            prefix = f"Members {self.page + 1}/{max(1, (len(self.members) + 9) // 10)} · "
        else:
            embed.add_field(name="Total member power", value=f"{latest.total_power:,}", inline=False)
            embed.add_field(name="Daily change", value=_change(latest.total_power, daily.total_power if daily else None))
            embed.add_field(name="Weekly change", value=_change(latest.total_power, weekly.total_power if weekly else None))
            embed.add_field(name="Members", value=str(len(self.members)))
            history = self.report["history"][-7:]
            if history:
                embed.add_field(
                    name="Recent daily totals (UTC)",
                    value="\n".join(f"{row.snapshot_date.isoformat()} · {row.total_power:,}" for row in history),
                    inline=False,
                )
            if len(self.report["history"]) == 1:
                embed.description += "\nFirst snapshot saved. Daily and weekly changes need earlier snapshots."
            embed.description += "\nRoster changes affect total member power."
            prefix = ""
        embed.set_footer(text=f"{prefix}One snapshot per UTC day · Daily: previous date · Weekly: 7 days earlier")
        return embed

    async def _edit(self, interaction):
        self._update_controls()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="Summary", style=discord.ButtonStyle.secondary)
    async def summary_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.showing_members = False
        await self._edit(interaction)

    @discord.ui.button(label="Members", style=discord.ButtonStyle.primary)
    async def members_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.showing_members = True
        await self._edit(interaction)

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary)
    async def previous_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = max(0, self.page - 1)
        await self._edit(interaction)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = min(max(0, (len(self.members) - 1) // 10), self.page + 1)
        await self._edit(interaction)

    @discord.ui.button(label="Stop tracking", style=discord.ButtonStyle.danger, row=1)
    async def stop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild_id != self.guild_id or interaction.user.id not in self.handler.admin_user_ids:
            await interaction.response.send_message("Only configured bot admins can stop tracking in this server.", ephemeral=True)
            return
        await interaction.response.defer()
        stopped = await self.handler.service.stop_tracking(
            self.guild_id, self.report["kingdom_id"], self.report["alliance_id"]
        )
        if not stopped:
            await interaction.followup.send("Tracking changed. Reopen /alliance before stopping it.", ephemeral=True)
            return
        for item in self.children:
            item.disabled = True
        await interaction.edit_original_response(
            embed=build_status_embed(title="Power tracking stopped", description="Saved history is retained. An admin can select an alliance with /alliance to start again."),
            view=self,
        )
        self.stop()


class AlliancePowerHandler:
    def __init__(self, service, bot, admin_user_ids, *, alliance_choices, kid_choices):
        self.service = service
        self._bot = bot
        self.admin_user_ids = set(admin_user_ids)
        self._alliance_choices = alliance_choices
        self._kid_choices = kid_choices

    def register_commands(self):
        @self._bot.tree.command(name="alliance", description="View daily and weekly alliance power trends")
        @app_commands.guild_only()
        @app_commands.describe(kid="Kingdom ID (admins: select an alliance to track)", alliance="Choose an alliance from the suggestions")
        async def alliance(interaction: discord.Interaction, kid: int | None = None, alliance: str | None = None):
            await self._handle_alliance(interaction, kid, alliance)

        @alliance.autocomplete("alliance")
        async def alliance_autocomplete(interaction: discord.Interaction, current: str):
            return await self._alliance_choices(getattr(interaction.namespace, "kid", None), current)

        @alliance.autocomplete("kid")
        async def kid_autocomplete(interaction: discord.Interaction, current: int):
            return await self._kid_choices(interaction, current)

    async def _handle_alliance(self, interaction: discord.Interaction, kid: int | None = None, alliance: str | None = None):
        if interaction.guild_id is None:
            await interaction.response.send_message("Use /alliance in a server.", ephemeral=True)
            return
        configuring = kid is not None or alliance is not None
        if configuring:
            if interaction.user.id not in self.admin_user_ids:
                await interaction.response.send_message("Only configured bot admins can change power tracking. Use /alliance without options to view it.", ephemeral=True)
                return
            if (kid is None or alliance is None or not 0 < kid < 2**63 or len(alliance) > 19
                    or not alliance.isascii() or not alliance.isdigit() or not 0 < int(alliance) < 2**63):
                await interaction.response.send_message("Provide a positive kingdom ID and select an alliance from autocomplete. Typed tags are not selections.", ephemeral=True)
                return
        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            if configuring:
                await self.service.track_alliance(interaction.guild_id, kid, int(alliance))
            report = await self.service.get_report(interaction.guild_id)
            if report is None:
                await interaction.followup.send("No alliance is tracked in this server. A configured bot admin can use /alliance with a kingdom and alliance to begin.", ephemeral=True)
                return
            view = AlliancePowerView(self, interaction.guild_id, report, interaction.user.id)
            # Complete the private defer before sending the public report.
            await interaction.edit_original_response(content="Power report ready.")
            view.message = await interaction.followup.send(embed=view.build_embed(), view=view, wait=True, ephemeral=False)
            try:
                await interaction.delete_original_response()
            except discord.HTTPException:
                logger.debug("Could not remove the private power report receipt", exc_info=True)
        except ValueError as error:
            await interaction.followup.send(str(error), ephemeral=True)
        except Exception as error:
            await send_ui_error(interaction, error)

    def start_polling_task(self):
        if not self._collect_snapshots.is_running():
            self._collect_snapshots.start()
            logger.info("Alliance power polling task started")

    def is_polling_running(self) -> bool:
        return self._collect_snapshots.is_running()

    @tasks.loop(hours=1)
    async def _collect_snapshots(self):
        try:
            await self.service.collect_due_snapshots({guild.id for guild in self._bot.guilds})
        except Exception:
            logger.exception("Alliance power snapshot collection failed")

    @_collect_snapshots.before_loop
    async def _before_collection(self):
        await self._bot.wait_until_ready()
