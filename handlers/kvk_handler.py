import logging
from typing import Any, Dict

import discord
from discord import app_commands
from discord.ext import commands

from services.kvk_service import IKVKService
from handlers.ui import EmbedColors, OwnedView, build_status_embed, send_ui_error

logger = logging.getLogger(__name__)


class CompareKingdomModal(discord.ui.Modal, title="Compare kingdoms"):
    opponent = discord.ui.TextInput(label="Other kingdom number", placeholder="For example, 831", max_length=10)

    def __init__(self, handler, kingdom_number: int, author_id: int):
        super().__init__(timeout=180)
        self.handler = handler
        self.kingdom_number = kingdom_number
        self.author_id = author_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Open your own kingdom comparison to continue.", ephemeral=True)
            return False
        return True

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        await send_ui_error(interaction, error)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            opponent = int(self.opponent.value.strip())
        except ValueError:
            await interaction.response.send_message("Enter a positive kingdom number, for example 831.", ephemeral=True)
            return
        if opponent <= 0 or opponent == self.kingdom_number:
            await interaction.response.send_message("Enter a positive kingdom number different from this kingdom.", ephemeral=True)
            return
        await self.handler._handle_compare_kvk_slash(interaction, self.kingdom_number, opponent)


class KVKHistoryView(OwnedView):
    """Keep all loaded matches accessible without flooding the channel."""

    def __init__(self, handler, kingdom_number: int, stats: dict, author_id: int, overview=None):
        super().__init__(author_id)
        self.handler = handler
        self.kingdom_number = kingdom_number
        self.stats = stats
        self.overview = overview
        self.current_page = 0
        lines = [handler._format_history_line(entry)[:200] for entry in stats.get("history", [])]
        self.pages = [lines[index:index + 5] for index in range(0, len(lines), 5)] or [[]]
        self._update_controls()

    def _update_controls(self):
        self.previous_button.disabled = self.current_page == 0
        self.next_button.disabled = self.current_page == len(self.pages) - 1

    def build_embed(self) -> discord.Embed:
        embed = self.overview.copy() if self.overview else self.handler._build_history_embed(self.kingdom_number, self.stats)
        embed.title = str(embed.title or "KVK history")[:256]
        embed.description = str(embed.description or "")[:512]
        for index, field in enumerate(embed.fields):
            embed.set_field_at(index, name=field.name, value=str(field.value)[:400], inline=field.inline)
        embed.add_field(
            name="Direct Match History" if self.overview else "Match History",
            value="\n".join(self.pages[self.current_page]) or ("History unavailable." if self.stats.get("matchCount") is None else "No matches available."),
            inline=False,
        )
        embed.set_footer(text=f"Page {self.current_page + 1}/{len(self.pages)} · {len(self.stats.get('history', []))} loaded matches · Source: Kingshot KVK")
        return embed

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary)
    async def previous_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page = max(0, self.current_page - 1)
        self._update_controls()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page = min(len(self.pages) - 1, self.current_page + 1)
        self._update_controls()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="Compare kingdom", style=discord.ButtonStyle.primary)
    async def compare_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(CompareKingdomModal(self.handler, self.kingdom_number, self.author_id))


class KVKHandler:
    """Handles KVK (Kingdom vs Kingdom) Discord commands."""

    def __init__(self, kvk_service: IKVKService, bot: commands.Bot):
        """
        Initialize KVK handler.

        Args:
            kvk_service: Service for fetching KVK matches
            bot: Discord bot instance
        """
        self._kvk_service = kvk_service
        self._bot = bot
        logger.info("KVKHandler initialized")

    def register_commands(self):
        """Register all KVK commands with the bot."""

        @self._bot.tree.command(name="kvk", description="Get KVK match history for a kingdom")
        @app_commands.describe(kingdom_number="The kingdom number to fetch stats for (e.g., 830)")
        async def get_kvk_stats(interaction: discord.Interaction, kingdom_number: int):
            """Get KVK stats for a kingdom."""
            await self._handle_get_kvk_stats_slash(interaction, kingdom_number)

        @self._bot.tree.command(name="kvk_compare", description="Compare KVK match history for two kingdoms")
        @app_commands.describe(
            kingdom_a="First kingdom number to compare",
            kingdom_b="Second kingdom number to compare",
        )
        async def compare_kvk_stats(interaction: discord.Interaction, kingdom_a: int, kingdom_b: int):
            """Compare KVK stats for two kingdoms."""
            await self._handle_compare_kvk_slash(interaction, kingdom_a, kingdom_b)

    async def _handle_get_kvk_stats_slash(self, interaction: discord.Interaction, kingdom_number: int):
        """
        Handle fetching KVK stats for a kingdom.

        Args:
            interaction: Discord interaction
            kingdom_number: The kingdom number to fetch matches for
        """
        await interaction.response.defer(thinking=True)

        user_info = f"{interaction.user.name}#{interaction.user.discriminator} (ID: {interaction.user.id})"
        guild_info = f"{interaction.guild.name} (ID: {interaction.guild.id})" if interaction.guild else "DM"

        if kingdom_number <= 0:
            await interaction.followup.send(
                embed=self._build_status_embed(
                    title="⚠️ Invalid Kingdom Number",
                    description="Kingdom number must be a positive integer.",
                    color=EmbedColors.WARNING,
                )
            )
            return

        logger.info(f"KVK stats requested for kingdom {kingdom_number} by {user_info} in {guild_info}")

        try:
            result = await self._kvk_service.get_kingdom_stats(kingdom_number)

            if not result.get("success"):
                embed = self._build_status_embed(
                    title=f"❌ Could Not Fetch Kingdom {kingdom_number}",
                    description="Match history is temporarily unavailable. Try again in a moment.",
                    color=EmbedColors.ERROR,
                )
                embed.set_footer(text="Try again in a moment or verify the kingdom number")
                await interaction.followup.send(embed=embed)
                logger.warning(f"Failed to fetch KVK stats for kingdom {kingdom_number}: {result.get('message')}")
                return

            stats = result.get("data", {})
            view = KVKHistoryView(self, kingdom_number, stats, interaction.user.id)
            view.message = await interaction.followup.send(embed=view.build_embed(), view=view, wait=True)
            logger.info(f"Successfully sent KVK stats for kingdom {kingdom_number}")

        except Exception as e:
            logger.error(f"Error fetching KVK stats for kingdom {kingdom_number}: {e}", exc_info=True)
            await interaction.followup.send(
                embed=self._build_status_embed(
                    title="❌ Unexpected Error",
                    description="An error occurred while fetching KVK stats. Please try again later.",
                    color=EmbedColors.ERROR,
                )
            )

    async def _handle_compare_kvk_slash(self, interaction: discord.Interaction, kingdom_a: int, kingdom_b: int):
        """Handle comparing KVK stats for two kingdoms."""
        await interaction.response.defer(thinking=True)

        if kingdom_a <= 0 or kingdom_b <= 0:
            await interaction.followup.send(
                embed=self._build_status_embed(
                    title="⚠️ Invalid Kingdom Numbers",
                    description="Both kingdom numbers must be positive integers.",
                    color=EmbedColors.WARNING,
                )
            )
            return

        if kingdom_a == kingdom_b:
            await interaction.followup.send(
                embed=self._build_status_embed(
                    title="⚠️ Duplicate Kingdom",
                    description="Please provide two different kingdoms to compare.",
                    color=EmbedColors.WARNING,
                )
            )
            return

        logger.info(f"KVK comparison requested for kingdoms {kingdom_a} vs {kingdom_b}")

        try:
            result = await self._kvk_service.compare_kingdoms(kingdom_a, kingdom_b)
            if not result.get("success"):
                embed = self._build_status_embed(
                    title="❌ Comparison Failed",
                    description="Could not compare the selected kingdoms.",
                    color=EmbedColors.ERROR,
                )
                await interaction.followup.send(embed=embed)
                return

            data = result.get("data", {})
            stats_a = data.get("kingdom_a", {})
            stats_b = data.get("kingdom_b", {})
            score = data.get("score", {})
            h2h = data.get("head_to_head", {})

            score_a = score.get(str(kingdom_a))
            score_b = score.get(str(kingdom_b))

            if score_a is None or score_b is None:
                verdict = "Comparison score unavailable"
            elif not stats_a.get("matchCount") or not stats_b.get("matchCount"):
                verdict = "Not enough match history to compare both kingdoms"
            elif score_a > score_b:
                verdict = f"Kingdom {kingdom_a} leads on tracked metrics ({score_a}-{score_b})"
            elif score_b > score_a:
                verdict = f"Kingdom {kingdom_b} leads on tracked metrics ({score_b}-{score_a})"
            else:
                verdict = f"Dead heat ({score_a}-{score_b})"

            embed = discord.Embed(
                title=f"⚔️ KvK Compare: {kingdom_a} vs {kingdom_b}",
                description=verdict,
                color=EmbedColors.INFO,
            )

            embed.add_field(
                name=f"Kingdom {kingdom_a}",
                value=self._build_compact_match_stats(stats_a),
                inline=True,
            )
            embed.add_field(
                name=f"Kingdom {kingdom_b}",
                value=self._build_compact_match_stats(stats_b),
                inline=True,
            )

            metrics = [
                ("Castle Wins", stats_a.get("wins"), stats_b.get("wins")),
                ("Castle Losses", stats_a.get("losses"), stats_b.get("losses")),
                ("Prep Wins", stats_a.get("prepWins"), stats_b.get("prepWins")),
                ("Castle Captures", stats_a.get("castleCaptures"), stats_b.get("castleCaptures")),
                ("Defenses Held", stats_a.get("defensesHeld"), stats_b.get("defensesHeld")),
            ]

            comparison_lines = []
            for metric, value_a, value_b in metrics:
                comparison_lines.append(
                    f"{metric}: {self._format_metric(metric, value_a)} vs {self._format_metric(metric, value_b)}"
                )

            embed.add_field(name="Overall Metrics", value="\n".join(comparison_lines), inline=False)
            embed.add_field(
                name="Direct Matchups",
                value=self._build_head_to_head_summary(kingdom_a, kingdom_b, h2h),
                inline=False,
            )
            direct_stats = dict(h2h.get("kingdom_a", {}), history=h2h.get("matches", []))
            if not h2h.get("available"):
                direct_stats["matchCount"] = None
            view = KVKHistoryView(self, kingdom_a, direct_stats, interaction.user.id, overview=embed)
            view.message = await interaction.followup.send(embed=view.build_embed(), view=view, wait=True)
            logger.info(f"Successfully compared kingdoms {kingdom_a} and {kingdom_b}")
        except Exception as e:
            logger.error(f"Error comparing kingdoms {kingdom_a} vs {kingdom_b}: {e}", exc_info=True)
            await interaction.followup.send(
                embed=self._build_status_embed(
                    title="❌ Unexpected Error",
                    description="An error occurred while comparing kingdoms. Please try again later.",
                    color=EmbedColors.ERROR,
                )
            )

    @staticmethod
    def _build_status_embed(title: str, description: str, color: discord.Color) -> discord.Embed:
        """Build consistent response embeds for KVK commands."""
        return build_status_embed(title=title, description=description, color=color)

    @staticmethod
    def _format_float(value: Any) -> str:
        """Format numbers with two decimals when possible."""
        try:
            return f"{float(value):.2f}"
        except (TypeError, ValueError):
            return "Unavailable"

    def _build_latest_match_summary(self, latest: Any) -> str:
        """Build the embed description from the most recent match."""
        if not latest:
            return "No KVK matches found for this kingdom."

        return (
            f"Latest: **KvK #{latest.get('season_id', '?')}** vs **{latest.get('opponent', '?')}** "
            f"on **{latest.get('season_date', 'Unknown date')}** | "
            f"Castle: **{self._format_history_result(latest.get('castleResult'))}** | "
            f"Prep: **{self._format_history_result(latest.get('prepResult'))}**"
        )

    def _format_history_line(self, entry: Dict[str, Any]) -> str:
        """Format one normalized KVK match for Discord history."""
        side = self._format_side(entry.get("side"))
        castle = self._format_history_result(entry.get("castleResult"))
        prep = self._format_history_result(entry.get("prepResult"))
        return (
            f"#{entry.get('season_id', '?')} {entry.get('season_date', 'Unknown')} "
            f"vs {entry.get('opponent', '?')} | {side} | Castle {castle} | Prep {prep}"
        )

    def _build_history_embed(self, kingdom_number: int, stats: Dict[str, Any]) -> discord.Embed:
        embed = discord.Embed(
            title=f"⚔️ KVK History · Kingdom {kingdom_number}",
            description=self._build_latest_match_summary(stats.get("latestMatch")) if stats.get("matchCount") is not None else "Match history unavailable.",
            color=EmbedColors.INFO,
        )
        embed.add_field(name="Castle Record", value=self._record(stats, "wins", "losses"), inline=True)
        embed.add_field(name="Castle Win Rate", value=self._win_rate(stats), inline=True)
        embed.add_field(name="Current Streak", value=self._format_metric("streak", stats.get("currentStreak")), inline=True)
        embed.add_field(name="Prep Record", value=self._record(stats, "prepWins", "prepLosses"), inline=True)
        embed.add_field(name="Prep Win Rate", value=self._win_rate(stats, prep=True), inline=True)
        embed.add_field(name="Matches Tracked", value=self._format_metric("Matches", stats.get("matchCount")), inline=True)
        embed.add_field(
            name="Attack / Defense",
            value=f"{self._format_metric('attacks', stats.get('attacks'))} / {self._format_metric('defenses', stats.get('defenses'))}",
            inline=True,
        )
        embed.add_field(
            name="Castles Captured / Held",
            value=f"{self._format_metric('captures', stats.get('castleCaptures'))} / {self._format_metric('held', stats.get('defensesHeld'))}",
            inline=True,
        )
        return embed

    @staticmethod
    def _record(stats: Dict[str, Any], wins: str, losses: str) -> str:
        if stats.get(wins) is None or stats.get(losses) is None:
            return "Unavailable"
        return f"{stats[wins]}-{stats[losses]}"

    def _win_rate(self, stats: Dict[str, Any], prep: bool = False) -> str:
        wins, losses, rate = ("prepWins", "prepLosses", "prepWinRate") if prep else ("wins", "losses", "winRate")
        if stats.get(wins) == 0 and stats.get(losses) == 0:
            return "No decided matches"
        return self._format_metric("Prep Win Rate" if prep else "Castle Win Rate", stats.get(rate))

    def _build_compact_match_stats(self, stats: Dict[str, Any]) -> str:
        """Lead with the records; unknown values stay distinct from zero."""
        return (
            f"Castle: {self._record(stats, 'wins', 'losses')} ({self._win_rate(stats)})\n"
            f"Prep: {self._record(stats, 'prepWins', 'prepLosses')} ({self._win_rate(stats, prep=True)})\n"
            f"Roles: Attack {self._format_metric('attacks', stats.get('attacks'))} / "
            f"Defense {self._format_metric('defenses', stats.get('defenses'))}\n"
            f"Captured/Held: {self._format_metric('captures', stats.get('castleCaptures'))}/"
            f"{self._format_metric('held', stats.get('defensesHeld'))}\n"
            f"Streak: {self._format_metric('streak', stats.get('currentStreak'))}"
        )

    def _format_metric(self, metric: str, value: Any) -> str:
        """Format metric values for compare output."""
        if value in (None, "", "N/A"):
            return "Unavailable"
        if metric in {"Castle Win Rate", "Prep Win Rate"}:
            formatted = self._format_float(value)
            return f"{formatted}%" if formatted != "Unavailable" else formatted
        return str(value)

    def _build_head_to_head_summary(self, kingdom_a: int, kingdom_b: int, h2h: Dict[str, Any]) -> str:
        """Build direct-matchup comparison text."""
        if not h2h.get("available"):
            return "Head-to-head data unavailable. Try again later."

        summary_a = h2h.get("kingdom_a", {})
        summary_b = h2h.get("kingdom_b", {})
        total = summary_a.get("matchCount")
        if total is None:
            return "Head-to-head data unavailable."
        if total == 0:
            return "No direct KVK matchups found."

        lines = [
            f"Matches: {total}",
            (
                f"Castle: K{kingdom_a} {self._record(summary_a, 'wins', 'losses')} | "
                f"K{kingdom_b} {self._record(summary_b, 'wins', 'losses')}"
            ),
            (
                f"Prep: K{kingdom_a} {self._record(summary_a, 'prepWins', 'prepLosses')} | "
                f"K{kingdom_b} {self._record(summary_b, 'prepWins', 'prepLosses')}"
            ),
        ]

        return "\n".join(lines)

    @staticmethod
    def _format_side(side: Any) -> str:
        normalized = str(side or "unknown").strip().lower()
        if normalized == "attacker":
            return "Attack"
        if normalized == "defender":
            return "Defense"
        return "Unknown"

    @staticmethod
    def _format_history_result(result: Any) -> str:
        """Normalize match result labels from KVK API."""
        normalized = str(result or "Unknown").strip().lower()
        if normalized == "win":
            return "Win"
        if normalized == "loss":
            return "Loss"
        if normalized == "unknown":
            return "Unknown"
        return str(result or "Unknown")
