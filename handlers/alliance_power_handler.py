"""Daily alliance and member power reports."""

import asyncio
import logging
import re
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import tasks

from handlers.confirm_view import ConfirmView
from handlers.permissions import AccessPolicy
from handlers.ui import EmbedColors, OwnedView, build_status_embed, edit_panel_message, send_ui_error

logger = logging.getLogger(__name__)

PANEL_TIMEOUT_SECONDS = 900.0
MEMBERS_PER_PAGE = 10
_CHOICE_TAG = re.compile(r"^\[([^\]]+)\]")
INACTIVITY_CAVEAT = (
    "Flat power is only a hint. Maxed players, and players saving resources or "
    "keeping troops healed, also stop growing. Check in-game before acting."
)


def _change(current: int | None, baseline: int | None) -> str:
    if current is None:
        return "Unavailable"
    if baseline is None:
        return "New history"
    difference = current - baseline
    percentage = f"{difference / baseline:+.1%}" if baseline else "percentage unavailable: baseline 0"
    return f"{difference:+,} ({percentage})"


def _escape(value, limit: int) -> str:
    return discord.utils.escape_markdown(str(value)[:limit])


def _member_details(member: dict) -> str:
    """Optional profile details; older snapshots do not carry them."""
    parts = []
    if member.get("rank") is not None:
        parts.append(f"R{member['rank']}")
    if member.get("stove_lv") is not None:
        parts.append(f"TC {member['stove_lv']}")
    if member.get("vip") is not None:
        parts.append(f"VIP {member['vip']}")
    return " · ".join(parts)


def _bounded_lines(lines: list[str], limit: int = 1024, separator: str = "\n") -> str:
    """Join lines without exceeding a Discord field limit, noting what was left out."""
    if not lines:
        return "None"
    output: list[str] = []
    for index, line in enumerate(lines):
        left_after = len(lines) - index - 1
        # Reserve room for a "+N more" tail so stopping later always fits.
        tail = f"{separator}+{left_after} more" if left_after else ""
        if len(separator.join([*output, line])) + len(tail) > limit:
            return separator.join([*output, f"+{len(lines) - index} more"])
        output.append(line)
    return separator.join(output)


def _member_line(uid: str, member: dict) -> str:
    name = _escape(member.get("name") or f"Player {uid}", 60)
    power = member.get("power")
    details = _member_details(member)
    text = f"{name} · {power:,}" if power is not None else name
    return f"{text} · {details}" if details else text


def _changes_period(changes) -> str:
    if changes.gap_days == 1:
        return "since the previous day"
    return f"since {changes.older_date.isoformat()} ({changes.gap_days} days, no complete snapshot in between)"


def _sentence(text: str) -> str:
    return text[:1].upper() + text[1:]


def _add_change_fields(embed: discord.Embed, changes) -> None:
    embed.add_field(
        name=f"Joined ({len(changes.joined)})",
        value=_bounded_lines([_member_line(uid, member) for uid, member in changes.joined]),
        inline=False,
    )
    embed.add_field(
        name=f"Left ({len(changes.left)})",
        value=_bounded_lines([_member_line(uid, member) for uid, member in changes.left]),
        inline=False,
    )
    embed.add_field(
        name=f"Renamed ({len(changes.renamed)})",
        value=_bounded_lines([f"{_escape(old, 60)} → {_escape(new, 60)}" for _, old, new in changes.renamed]),
        inline=False,
    )


def _alliance_title(latest) -> str:
    name = _escape(latest.alliance_name or "Alliance", 100)
    tag = _escape(latest.alliance_tag or "", 20)
    return f"[{tag}] {name}"


def build_daily_report_embed(report: dict) -> discord.Embed:
    """Compact automatic report posted after a new daily snapshot."""
    latest = report["latest"]
    captured = int(latest.captured_at.timestamp())
    embed = build_status_embed(
        title=f"Daily alliance report · {_alliance_title(latest)}"[:256],
        description=f"Kingdom {report['kingdom_id']} · captured <t:{captured}:f>",
    )
    embed.timestamp = latest.captured_at
    daily = report.get("daily")
    embed.add_field(name="Total member power", value=f"{latest.total_power:,}")
    embed.add_field(name="Daily change", value=_change(latest.total_power, daily.total_power if daily else None))
    embed.add_field(name="Members", value=str(len(latest.members or {})))
    changes = report.get("changes")
    if changes is None:
        embed.add_field(
            name="Roster changes",
            value="First snapshot saved. Changes appear after the next daily capture.",
            inline=False,
        )
    elif changes.is_empty:
        embed.add_field(
            name="Roster changes",
            value=f"No joins, departures, or renames {_changes_period(changes)}.",
            inline=False,
        )
    else:
        embed.add_field(name="Roster changes", value=f"{_sentence(_changes_period(changes))}.", inline=False)
        _add_change_fields(embed, changes)
    flat = (report.get("inactive") or {}).get(7)
    if flat:
        embed.add_field(
            name="Possibly inactive",
            value=f"{len(flat)} member(s) with flat power for 7+ days. Managers can review them with /alliance → Inactive.",
            inline=False,
        )
    embed.set_footer(text="Automatic daily report · Open /alliance for members and trends")
    return embed


def build_inactive_embed(report: dict) -> discord.Embed:
    """Manager-only list of members whose power stayed flat."""
    embed = build_status_embed(
        title="Possibly inactive members",
        description=f"Members whose power did not change in any daily snapshot over the period.\n{INACTIVITY_CAVEAT}",
        color=EmbedColors.WARNING,
    )
    inactive = report.get("inactive") or {}
    seven, fourteen = inactive.get(7), inactive.get(14)

    def lines(members):
        return [
            f"{_escape(member.name, 60)} · {member.power:,}" + (f" · TC {member.stove_lv}" if member.stove_lv is not None else "")
            for member in members
        ]

    if seven is None:
        embed.add_field(
            name="Not enough history",
            value="About 7 days of daily snapshots are needed before flat power can be detected.",
            inline=False,
        )
        return embed
    long_term = {member.uid for member in fourteen or ()}
    if fourteen is None:
        embed.add_field(name="Flat for 14+ days", value="Not enough history yet.", inline=False)
    else:
        embed.add_field(
            name=f"Flat for 14+ days ({len(fourteen)})",
            value=_bounded_lines(lines(fourteen)),
            inline=False,
        )
    recent = [member for member in seven if member.uid not in long_term]
    embed.add_field(
        name=f"Flat for 7+ days ({len(recent)})" if fourteen is not None else f"Flat for 7+ days ({len(seven)})",
        value=_bounded_lines(lines(recent)),
        inline=False,
    )
    embed.set_footer(text=f"Since {seven[0].since.isoformat()} (7 days)" if seven else "One snapshot per UTC day")
    return embed


class AlliancePowerView(OwnedView):
    def __init__(self, handler, guild_id: int, report: dict, author_id: int, *, can_manage: bool | None = None):
        super().__init__(author_id, timeout=PANEL_TIMEOUT_SECONDS)
        self.handler = handler
        self.guild_id = guild_id
        self.report = report
        self.page = 0
        self.mode = "summary"
        latest = report["latest"]
        self.members = sorted(
            (latest.members if latest else {}).items(),
            key=lambda pair: (pair[1].get("power") is None, -(pair[1].get("power") or 0), pair[0]),
        )
        if can_manage is None:
            can_manage = handler._access.is_super_admin(author_id)
        self.can_manage = can_manage
        if not can_manage:
            self.remove_item(self.stop_button)
            self.remove_item(self.inactive_button)
        self._update_controls()

    @property
    def showing_members(self) -> bool:
        return self.mode == "members"

    def _update_controls(self):
        self.summary_button.disabled = self.mode == "summary"
        self.members_button.disabled = self.mode == "members" or not self.members
        self.changes_button.disabled = self.mode == "changes" or self.report["latest"] is None
        self.previous_button.disabled = self.mode != "members" or self.page == 0
        self.next_button.disabled = self.mode != "members" or (self.page + 1) * MEMBERS_PER_PAGE >= len(self.members)

    def build_embed(self) -> discord.Embed:
        latest = self.report["latest"]
        embed = build_status_embed(title=f"Alliance power · Kingdom {self.report['kingdom_id']}", description="")
        if latest is None:
            embed.description = "Waiting for the first complete snapshot."
            return embed
        captured = int(latest.captured_at.timestamp())
        embed.description = f"**{_alliance_title(latest)}**\nCaptured <t:{captured}:f> (<t:{captured}:R>)"
        embed.timestamp = latest.captured_at
        if latest.snapshot_date < datetime.now(timezone.utc).date():
            embed.description += "\nLatest snapshot is from an earlier UTC day."
            embed.color = EmbedColors.WARNING
        if self.mode == "members":
            prefix = self._add_member_fields(embed)
        elif self.mode == "changes":
            prefix = self._add_changes(embed)
        else:
            prefix = self._add_summary(embed)
        embed.set_footer(text=f"{prefix}One snapshot per UTC day · Daily: previous date · Weekly: 7 days earlier")
        return embed

    def _add_member_fields(self, embed: discord.Embed) -> str:
        daily, weekly = self.report["daily"], self.report["weekly"]
        start = self.page * MEMBERS_PER_PAGE
        for uid, member in self.members[start:start + MEMBERS_PER_PAGE]:
            power = member.get("power")
            name = _escape(member.get("name") or "Unknown player", 90)
            changes = []
            for label, baseline in (("Daily", daily), ("Weekly", weekly)):
                old_power = (baseline.members.get(uid) or {}).get("power") if baseline else None
                changes.append(f"{label}: {_change(power, old_power)}")
            lines = [f"Power: {power:,}" if power is not None else "Power: Unavailable"]
            details = _member_details(member)
            if details:
                lines.append(details)
            embed.add_field(name=name, value="\n".join(lines + changes), inline=False)
        pages = max(1, (len(self.members) + MEMBERS_PER_PAGE - 1) // MEMBERS_PER_PAGE)
        return f"Members {self.page + 1}/{pages} · "

    def _add_changes(self, embed: discord.Embed) -> str:
        changes = self.report.get("changes")
        if changes is None:
            embed.add_field(
                name="Roster changes",
                value="Two complete snapshots are needed. Check again after the next daily capture.",
                inline=False,
            )
            return "Changes · "
        if changes.is_empty:
            embed.add_field(
                name="Roster changes",
                value=f"No joins, departures, or renames {_changes_period(changes)}.",
                inline=False,
            )
        else:
            embed.description += f"\nRoster changes {_changes_period(changes)}."
            _add_change_fields(embed, changes)
        return "Changes · "

    def _add_summary(self, embed: discord.Embed) -> str:
        latest = self.report["latest"]
        daily, weekly = self.report["daily"], self.report["weekly"]
        embed.add_field(name="Total member power", value=f"{latest.total_power:,}", inline=False)
        embed.add_field(name="Daily change", value=_change(latest.total_power, daily.total_power if daily else None))
        embed.add_field(name="Weekly change", value=_change(latest.total_power, weekly.total_power if weekly else None))
        embed.add_field(name="Members", value=str(len(self.members)))
        changes = self.report.get("changes")
        if changes is not None:
            summary = (
                "No changes" if changes.is_empty
                else f"+{len(changes.joined)} joined · -{len(changes.left)} left · {len(changes.renamed)} renamed"
            )
            embed.add_field(name="Roster", value=f"{summary} {_changes_period(changes)}", inline=False)
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
        return ""

    async def _edit(self, interaction):
        self._update_controls()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="Summary", style=discord.ButtonStyle.secondary, row=0)
    async def summary_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.mode = "summary"
        await self._edit(interaction)

    @discord.ui.button(label="Members", style=discord.ButtonStyle.primary, row=0)
    async def members_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.mode = "members"
        await self._edit(interaction)

    @discord.ui.button(label="Changes", style=discord.ButtonStyle.secondary, row=0)
    async def changes_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.mode = "changes"
        await self._edit(interaction)

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary, row=0)
    async def previous_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = max(0, self.page - 1)
        await self._edit(interaction)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary, row=0)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = min(max(0, (len(self.members) - 1) // MEMBERS_PER_PAGE), self.page + 1)
        await self._edit(interaction)

    async def _require_manager(self, interaction: discord.Interaction, action: str) -> bool:
        if interaction.guild_id != self.guild_id:
            await interaction.response.send_message("Open /alliance in this server to do that.", ephemeral=True)
            return False
        return await self.handler._access.require_manager(interaction, action=action)

    @discord.ui.button(label="Inactive", style=discord.ButtonStyle.secondary, row=1)
    async def inactive_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._require_manager(interaction, "view possibly inactive members"):
            return
        await interaction.response.send_message(
            embed=build_inactive_embed(self.report),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @discord.ui.button(label="Stop tracking", style=discord.ButtonStyle.danger, row=1)
    async def stop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._require_manager(interaction, "stop power tracking"):
            return
        latest = self.report["latest"]
        label = _alliance_title(latest) if latest else f"alliance {self.report['alliance_id']}"
        prompt = ConfirmView(
            interaction.user.id,
            on_confirm=self._confirm_stop,
            confirm_label="Stop tracking",
            keep_label="Keep tracking",
            kept_title="Tracking continues",
            kept_description="Power tracking was not changed.",
        )
        await interaction.response.send_message(
            embed=build_status_embed(
                title="Stop power tracking?",
                description=(
                    f"Daily snapshots of {label} stop for this server. Saved history is kept, "
                    "and a manager can start tracking again with /alliance."
                ),
                color=EmbedColors.WARNING,
            ),
            view=prompt,
            ephemeral=True,
        )
        try:
            prompt.message = await interaction.original_response()
        except (discord.HTTPException, AttributeError):
            logger.debug("Could not load the stop-tracking confirmation", exc_info=True)

    async def _confirm_stop(self, interaction: discord.Interaction):
        if not await self._require_manager(interaction, "stop power tracking"):
            return
        await interaction.response.defer()
        stopped = await self.handler.service.stop_tracking(
            self.guild_id, self.report["kingdom_id"], self.report["alliance_id"]
        )
        if not stopped:
            await interaction.edit_original_response(
                embed=build_status_embed(
                    title="Tracking changed",
                    description="Tracking changed meanwhile. Reopen /alliance before stopping it.",
                    color=EmbedColors.WARNING,
                ),
                view=None,
            )
            return
        stopped_embed = build_status_embed(
            title="Power tracking stopped",
            description="Saved history is retained. A server manager can select an alliance with /alliance to start again.",
        )
        await interaction.edit_original_response(embed=stopped_embed, view=None)
        for item in self.children:
            item.disabled = True
        self.stop()
        if self.message is not None:
            try:
                await edit_panel_message(self.message, embed=stopped_embed, view=self)
            except discord.HTTPException:
                logger.debug("Could not update the power report after stopping", exc_info=True)


class AlliancePowerHandler:
    def __init__(
        self,
        service,
        bot,
        admin_user_ids,
        *,
        alliance_choices,
        kid_choices,
        access_policy: AccessPolicy | None = None,
        guild_configuration_service=None,
    ):
        self.service = service
        self._bot = bot
        self.admin_user_ids = set(admin_user_ids)
        self._guild_configuration_service = guild_configuration_service
        self._access = access_policy or AccessPolicy(
            self.admin_user_ids,
            guild_configuration_service=guild_configuration_service,
        )
        self._alliance_choices = alliance_choices
        self._kid_choices = kid_choices
        self._background_tasks: set[asyncio.Task] = set()

    def register_commands(self):
        @self._bot.tree.command(name="alliance", description="View daily and weekly alliance power trends")
        @app_commands.guild_only()
        @app_commands.describe(
            kid="Kingdom ID (managers: select an alliance to track)",
            alliance="Choose an alliance from the suggestions or enter its exact tag",
        )
        async def alliance(interaction: discord.Interaction, kid: int | None = None, alliance: str | None = None):
            await self._handle_alliance(interaction, kid, alliance)

        @alliance.autocomplete("alliance")
        async def alliance_autocomplete(interaction: discord.Interaction, current: str):
            return await self._alliance_choices(getattr(interaction.namespace, "kid", None), current)

        @alliance.autocomplete("kid")
        async def kid_autocomplete(interaction: discord.Interaction, current: int):
            return await self._kid_choices(interaction, current)

    @staticmethod
    def _parse_selection(kid: int | None, alliance: str | None) -> tuple[int | None, str | None] | None:
        """Return (alliance_id, tag) for a valid selection, otherwise None."""
        selection = (alliance or "").strip()
        if kid is None or not selection or not 0 < kid < 2**63 or not selection.isascii():
            return None
        if selection.isdigit():
            if len(selection) > 19 or not 0 < int(selection) < 2**63:
                return None
            return int(selection), None
        if len(selection) == 3 and selection.isalnum():
            return None, selection
        return None

    async def _resolve_tag(self, kid: int, tag: str) -> int | None:
        """Resolve an exact tag through the autocomplete cache, then the Data API."""
        needle = tag.casefold()
        try:
            choices = await self._alliance_choices(kid, tag)
        except Exception:
            logger.debug("Alliance suggestions unavailable for tag lookup", exc_info=True)
            choices = []
        matches = set()
        for choice in choices or ():
            found = _CHOICE_TAG.match(str(getattr(choice, "name", "")))
            if found and found.group(1).strip().casefold() == needle:
                matches.add(str(getattr(choice, "value", "")))
        if len(matches) == 1:
            value = matches.pop()
            if value.isdigit() and int(value) > 0:
                return int(value)
        resolver = getattr(self.service, "resolve_alliance_tag", None)
        return await resolver(kid, tag) if resolver is not None else None

    async def _handle_alliance(self, interaction: discord.Interaction, kid: int | None = None, alliance: str | None = None):
        if interaction.guild_id is None:
            await interaction.response.send_message("Use /alliance in a server.", ephemeral=True)
            return
        configuring = kid is not None or alliance is not None
        alliance_id = tag = None
        if configuring:
            if not await self._access.require_manager(interaction, action="change power tracking"):
                return
            if (alliance or "").strip() == "__loading__":
                # Placeholder choice shared with /addalliance while suggestions load.
                await interaction.response.send_message(
                    "Alliance suggestions were still loading. Type the alliance again in a few "
                    "seconds and pick it from the list, or enter its exact 3-character tag.",
                    ephemeral=True,
                )
                return
            selection = self._parse_selection(kid, alliance)
            if selection is None:
                await interaction.response.send_message(
                    "Provide a positive kingdom ID and select an alliance from autocomplete, "
                    "or enter its exact 3-character tag.",
                    ephemeral=True,
                )
                return
            alliance_id, tag = selection
        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            captured = False
            if configuring:
                if alliance_id is None:
                    alliance_id = await self._resolve_tag(kid, tag)
                    if alliance_id is None:
                        await interaction.followup.send(
                            f"No alliance tagged `{discord.utils.escape_markdown(tag)}` was found among the "
                            f"top alliances of kingdom {kid}. Choose one from the suggestions.",
                            ephemeral=True,
                        )
                        return
                captured = await self.service.track_alliance(interaction.guild_id, kid, alliance_id)
            report = await self.service.get_report(interaction.guild_id)
            if report is None:
                await interaction.followup.send("No alliance is tracked in this server. A server manager can use /alliance with a kingdom and alliance to begin.", ephemeral=True)
                return
            can_manage = await self._access.can_manage(interaction)
            view = AlliancePowerView(self, interaction.guild_id, report, interaction.user.id, can_manage=can_manage)
            # Complete the private defer before sending the public report.
            await interaction.edit_original_response(content="Power report ready.")
            view.message = await interaction.followup.send(embed=view.build_embed(), view=view, wait=True, ephemeral=False)
            try:
                await interaction.delete_original_response()
            except discord.HTTPException:
                logger.debug("Could not remove the private power report receipt", exc_info=True)
            if captured is True:
                self._run_in_background(self.post_daily_reports([(kid, alliance_id)]))
            elif configuring and captured is None:
                await interaction.followup.send(
                    "Tracking is saved, but the KingShot Data API returned incomplete member profiles, so the "
                    "first snapshot isn't complete yet. The bot retries automatically every hour; this report "
                    "fills in once a complete snapshot is captured.",
                    ephemeral=True,
                )
        except ValueError as error:
            await interaction.followup.send(str(error), ephemeral=True)
        except Exception as error:
            await send_ui_error(interaction, error)

    def _run_in_background(self, coroutine) -> None:
        task = asyncio.create_task(coroutine)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def post_daily_reports(self, captured) -> int:
        """Post a compact report for each new snapshot to tracking servers' report channels."""
        if self._guild_configuration_service is None:
            return 0
        connected = {guild.id for guild in getattr(self._bot, "guilds", None) or ()}
        posted = 0
        for kingdom_id, alliance_id in captured:
            try:
                guild_ids = [
                    guild_id for guild_id in await self.service.guilds_tracking(kingdom_id, alliance_id)
                    if guild_id in connected
                ]
                if not guild_ids:
                    continue
                report = await self.service.get_alliance_report(kingdom_id, alliance_id)
                if report.get("latest") is None:
                    continue
                embed = build_daily_report_embed(report)
            except Exception:
                logger.warning("Could not build the daily report for kingdom %s alliance %s",
                               kingdom_id, alliance_id, exc_info=True)
                continue
            for guild_id in guild_ids:
                posted += await self._post_report(guild_id, embed)
        return posted

    async def _post_report(self, guild_id: int, embed: discord.Embed) -> bool:
        try:
            settings = await self._guild_configuration_service.get_settings(guild_id)
            channel_id = getattr(settings, "alliance_report_channel_id", None)
            if not channel_id:
                return False
            channel = self._bot.get_channel(channel_id) or await self._bot.fetch_channel(channel_id)
            channel_guild_id = getattr(getattr(channel, "guild", None), "id", None)
            if channel_guild_id is not None and channel_guild_id != guild_id:
                logger.warning("Alliance report channel %s is not in guild %s; skipping", channel_id, guild_id)
                return False
            await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
            return True
        except Exception:
            logger.warning("Could not post the daily alliance report in guild %s", guild_id, exc_info=True)
            return False

    def start_polling_task(self):
        if not self._collect_snapshots.is_running():
            self._collect_snapshots.start()
            logger.info("Alliance power polling task started")

    def is_polling_running(self) -> bool:
        return self._collect_snapshots.is_running()

    @tasks.loop(hours=1)
    async def _collect_snapshots(self):
        try:
            captured = await self.service.collect_due_snapshots({guild.id for guild in self._bot.guilds})
        except Exception:
            logger.exception("Alliance power snapshot collection failed")
            return
        try:
            await self.post_daily_reports(list(captured or ()))
        except Exception:
            logger.exception("Posting daily alliance reports failed")

    @_collect_snapshots.before_loop
    async def _before_collection(self):
        await self._bot.wait_until_ready()
