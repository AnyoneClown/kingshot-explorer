import logging
import time
from typing import Any, Dict, Optional

import discord
from discord import app_commands
from discord.ext import commands

from services import kvk_card_renderer, ranking_card_renderer
from services.kvk_service import IKVKService
from services.ranking_card_renderer import RankedRow, VersusSection
from handlers.kingdom_default import PublicReply, defer_for_kingdom
from handlers.ui import EmbedColors, OwnedView, build_status_embed, card_files, render_card

logger = logging.getLogger(__name__)

MAX_KINGDOM_NUMBER = 99999
# Public panels stay usable for a typical channel discussion.
BROWSE_PANEL_TIMEOUT_SECONDS = 900
EMBED_TOTAL_LIMIT = 6000
EMBED_FIELD_LIMIT = 1024
# Matches listed as text: what the history card shows, and under a comparison.
HISTORY_TEXT_MATCHES = kvk_card_renderer.MAX_SEASONS
HISTORY_TEXT_MATCHES_PER_FIELD = 5
DIRECT_MATCHES_LISTED = 3


class KVKCompareView(OwnedView):
    """The one control under a comparison: the prep dossier of the two kingdoms."""

    def __init__(self, handler, kingdom_number: int, opponent: int, author_id: int):
        super().__init__(author_id, timeout=BROWSE_PANEL_TIMEOUT_SECONDS)
        self.handler = handler
        self.kingdom_number = kingdom_number
        self.opponent = opponent

    @discord.ui.button(label="Prep dossier", emoji="🛡️", style=discord.ButtonStyle.success)
    async def dossier_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        del button
        # Each click posts a new public message, so it has a per-user limit.
        retry_after = self.handler._dossier_button_retry_after(interaction.user.id)
        if retry_after:
            await interaction.response.send_message(
                f"Slow down — try again in {retry_after:.0f}s.", ephemeral=True
            )
            return
        await self.handler._handle_kvk_prep(interaction, self.kingdom_number, self.opponent)


class KVKHandler:
    """Handles the KvK (Kingdom vs Kingdom) Discord command."""

    def __init__(
        self,
        kvk_service: IKVKService,
        bot: commands.Bot,
        dossier_service: Any = None,
        guild_configuration_service: Any = None,
    ):
        """
        Initialize KVK handler.

        Args:
            kvk_service: Service for fetching KVK matches
            bot: Discord bot instance
            dossier_service: Optional ``KvKDossierService``; enables the "Prep dossier"
                button on comparisons.
            guild_configuration_service: Server settings; gives ``/kvk`` the server's
                kingdom when the command names none.
        """
        self._kvk_service = kvk_service
        self._bot = bot
        self._dossier_service = dossier_service
        self._guild_configuration_service = guild_configuration_service
        # 2 dossiers per 60 s per user.
        self._dossier_button_cooldown = app_commands.Cooldown(2, 60.0)
        self._dossier_button_cooldowns: dict[int, app_commands.Cooldown] = {}
        logger.info("KVKHandler initialized")

    def _dossier_button_retry_after(self, user_id: int) -> float | None:
        now = time.time()
        # Forget users whose window has reset, or the map grows with every user who
        # ever pressed the button.
        for stale in [
            key for key, cooldown in self._dossier_button_cooldowns.items() if cooldown.get_tokens(now) >= cooldown.rate
        ]:
            del self._dossier_button_cooldowns[stale]
        cooldown = self._dossier_button_cooldowns.get(user_id)
        if cooldown is None:
            cooldown = self._dossier_button_cooldown.copy()
            self._dossier_button_cooldowns[user_id] = cooldown
        return cooldown.update_rate_limit(now)

    @property
    def has_dossier(self) -> bool:
        return self._dossier_service is not None

    def register_commands(self):
        """Register ``/kvk``: one kingdom shows its history, two kingdoms a comparison."""

        @self._bot.tree.command(
            name="kvk",
            description="KvK history of a kingdom, or a comparison of two kingdoms",
        )
        @app_commands.describe(
            kingdom="Kingdom number. Leave empty to use this server's kingdom",
            opponent="A second kingdom: compares the two instead of showing the history",
        )
        async def kvk(
            interaction: discord.Interaction,
            kingdom: Optional[app_commands.Range[int, 1, MAX_KINGDOM_NUMBER]] = None,
            opponent: Optional[app_commands.Range[int, 1, MAX_KINGDOM_NUMBER]] = None,
        ):
            await self._handle_kvk(interaction, kingdom, opponent)

    @staticmethod
    def _kingdom_problem(*kingdoms: int) -> tuple[str, str] | None:
        """Title and text of what is wrong with the kingdom numbers, or None."""
        if any(
            not isinstance(kingdom, int) or isinstance(kingdom, bool) or not 1 <= kingdom <= MAX_KINGDOM_NUMBER
            for kingdom in kingdoms
        ):
            title = "⚠️ Invalid Kingdom Number" if len(kingdoms) == 1 else "⚠️ Invalid Kingdom Numbers"
            return title, f"Kingdom numbers must be between 1 and {MAX_KINGDOM_NUMBER}."
        if len(kingdoms) == 2 and kingdoms[0] == kingdoms[1]:
            return "⚠️ Duplicate Kingdom", "Please provide two different kingdoms."
        return None

    async def _reject_invalid_kingdoms(self, interaction: discord.Interaction, *kingdoms: int) -> bool:
        """Privately reject bad input before deferring (Discord's ranges are not the only way in)."""
        problem = self._kingdom_problem(*kingdoms)
        if problem is None:
            return False
        await interaction.response.send_message(
            embed=self._build_status_embed(title=problem[0], description=problem[1], color=EmbedColors.WARNING),
            ephemeral=True,
        )
        return True

    async def _handle_kvk(
        self,
        interaction: discord.Interaction,
        kingdom: Optional[int] = None,
        opponent: Optional[int] = None,
    ) -> None:
        """``/kvk``: the history of one kingdom, or a comparison when an opponent is named.

        Without a kingdom the server's kingdom is used, also as the first side of a
        comparison (``/kvk opponent:831``).
        """
        given = [number for number in (kingdom, opponent) if number is not None]
        if given and await self._reject_invalid_kingdoms(interaction, *given):
            return

        kingdom, reply = await defer_for_kingdom(
            interaction,
            kingdom,
            self._guild_configuration_service,
            command="kvk",
            receipt="KvK report ready.",
        )
        if reply is None:
            return

        if opponent is None:
            await self._send_history(interaction, reply, kingdom)
            return
        # Checked again: the first kingdom may have come from the server's settings.
        problem = self._kingdom_problem(kingdom, opponent)
        if problem is not None:
            if reply.private:
                description = f"{problem[1]} This server's kingdom is {kingdom}: name another opponent."
            else:
                description = problem[1]
            await reply.fail(
                self._build_status_embed(title=problem[0], description=description, color=EmbedColors.WARNING)
            )
            return
        await self._send_comparison(interaction, reply, kingdom, opponent)

    async def _send_history(self, interaction: discord.Interaction, reply: PublicReply, kingdom_number: int) -> None:
        """Post a kingdom's KvK history: the card, or the same record as text."""
        logger.info("KVK history requested for kingdom %s by %s", kingdom_number, interaction.user.id)

        try:
            result = await self._kvk_service.get_kingdom_stats(kingdom_number)

            if not result.get("success"):
                embed = self._build_status_embed(
                    title=f"❌ Could Not Fetch Kingdom {kingdom_number}",
                    description="Match history is temporarily unavailable. Try again in a moment.",
                    color=EmbedColors.ERROR,
                )
                embed.set_footer(text="Try again in a moment or verify the kingdom number")
                await reply.fail(embed)
                logger.warning(f"Failed to fetch KVK stats for kingdom {kingdom_number}: {result.get('message')}")
                return

            stats = result.get("data", {})
            card = await render_card(kvk_card_renderer.render_kvk_history_card, kingdom_number, stats)
            embed = self._build_history_embed(kingdom_number, stats, pictures=bool(card))
            if card:
                embed.set_image(url=f"attachment://{kvk_card_renderer.HISTORY_FILENAME}")
            await reply.send(embed=embed, files=card_files(card, kvk_card_renderer.HISTORY_FILENAME))
            logger.info(f"Successfully sent KVK stats for kingdom {kingdom_number}")

        except Exception as e:
            logger.error(f"Error fetching KVK stats for kingdom {kingdom_number}: {e}", exc_info=True)
            await reply.fail(
                self._build_status_embed(
                    title="❌ Unexpected Error",
                    description="An error occurred while fetching KVK stats. Please try again later.",
                    color=EmbedColors.ERROR,
                )
            )

    async def _send_comparison(
        self, interaction: discord.Interaction, reply: PublicReply, kingdom_a: int, kingdom_b: int
    ) -> None:
        """Post the comparison of two kingdoms, with the prep dossier as its only button."""
        logger.info(f"KVK comparison requested for kingdoms {kingdom_a} vs {kingdom_b}")

        try:
            result = await self._kvk_service.compare_kingdoms(kingdom_a, kingdom_b)
            if not result.get("success"):
                await reply.fail(
                    self._build_status_embed(
                        title="❌ Comparison Failed",
                        description="Could not compare the selected kingdoms.",
                        color=EmbedColors.ERROR,
                    )
                )
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
            card = await render_card(
                kvk_card_renderer.render_kvk_compare_card, kingdom_a, kingdom_b, stats_a, stats_b, h2h
            )
            if card is None:
                self._add_compare_text(embed, kingdom_a, kingdom_b, stats_a, stats_b)
            else:
                embed.set_image(url=f"attachment://{kvk_card_renderer.COMPARE_FILENAME}")
            embed.add_field(
                name="Direct Matchups",
                value=self._build_direct_matchups(kingdom_a, kingdom_b, h2h),
                inline=False,
            )
            footer = "Source: Kingshot KVK"
            view = None
            if self.has_dossier:
                view = KVKCompareView(self, kingdom_a, kingdom_b, interaction.user.id)
                footer += " · Prep dossier posts a new message in this channel"
            embed.set_footer(text=footer)
            sent: Dict[str, Any] = {
                "embed": self._fit_embed(embed),
                "files": card_files(card, kvk_card_renderer.COMPARE_FILENAME),
            }
            if view is not None:
                sent.update(view=view, wait=True)
            message = await reply.send(**sent)
            if view is not None:
                view.message = message
            logger.info(f"Successfully compared kingdoms {kingdom_a} and {kingdom_b}")
        except Exception as e:
            logger.error(f"Error comparing kingdoms {kingdom_a} vs {kingdom_b}: {e}", exc_info=True)
            await reply.fail(
                self._build_status_embed(
                    title="❌ Unexpected Error",
                    description="An error occurred while comparing kingdoms. Please try again later.",
                    color=EmbedColors.ERROR,
                )
            )

    def _build_direct_matchups(self, kingdom_a: int, kingdom_b: int, h2h: Dict[str, Any]) -> str:
        """The direct record of two kingdoms and their latest meetings, from the first one's side."""
        summary = self._build_head_to_head_summary(kingdom_a, kingdom_b, h2h)
        matches = h2h.get("matches") if h2h.get("available") else None
        if not matches:
            return summary
        lines = [summary, f"Latest (K{kingdom_a} view):"]
        lines.extend(f"• {self._format_compact_match(match)}" for match in matches[:DIRECT_MATCHES_LISTED])
        if len(matches) > DIRECT_MATCHES_LISTED:
            lines.append(f"… and {len(matches) - DIRECT_MATCHES_LISTED} earlier")
        return "\n".join(lines)[:EMBED_FIELD_LIMIT]

    def _add_compare_text(
        self, embed: discord.Embed, kingdom_a: int, kingdom_b: int, stats_a: Dict[str, Any], stats_b: Dict[str, Any]
    ) -> None:
        """The comparison as text fields, for when the picture card cannot be drawn."""
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

    async def _handle_kvk_prep(self, interaction: discord.Interaction, kingdom_a: int, kingdom_b: int):
        """The **Prep dossier** button: post a public KvK prep dossier for two kingdoms."""
        if await self._reject_invalid_kingdoms(interaction, kingdom_a, kingdom_b):
            return
        if self._dossier_service is None:
            await interaction.response.send_message(
                embed=self._build_status_embed(
                    title="❌ Prep Dossier Unavailable",
                    description="The prep dossier needs the KingShot Data API, which is not configured.",
                    color=EmbedColors.ERROR,
                ),
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True)
        logger.info("KvK prep dossier requested for %s vs %s by %s", kingdom_a, kingdom_b, interaction.user.id)

        try:
            dossier = await self._dossier_service.build_dossier(kingdom_a, kingdom_b)
            embeds, files = await self._build_dossier_message(kingdom_a, kingdom_b, dossier)
            await interaction.followup.send(embeds=embeds, files=files)
        except Exception as e:
            logger.error("Error building KvK prep dossier for %s vs %s: %s", kingdom_a, kingdom_b, e, exc_info=True)
            await interaction.followup.send(
                embed=self._build_status_embed(
                    title="❌ Unexpected Error",
                    description="An error occurred while building the prep dossier. Please try again later.",
                    color=EmbedColors.ERROR,
                )
            )

    # ------------------------------------------------------------ prep dossier

    DOSSIER_ALLIANCES_FILENAME = "kvk-prep-alliances.png"
    DOSSIER_PLAYERS_FILENAME = "kvk-prep-players.png"

    @staticmethod
    def _ranked_rows(section: Dict[str, Any] | None, value_key: str) -> list[RankedRow] | None:
        """Rows of a dossier ranking for a card; None when the source did not respond."""
        if not section or not section.get("ok"):
            return None
        rows = []
        for index, row in enumerate(section["data"] or [], 1):
            try:
                value = float(row.get(value_key))
            except (TypeError, ValueError):
                value = None
            rank = row.get("rank") if isinstance(row.get("rank"), int) else index
            rows.append(
                RankedRow(name=str(row.get("name") or "Unknown"), value=value, rank=rank, tag=str(row.get("abbr") or ""))
            )
        return rows

    async def _build_dossier_message(
        self, kingdom_a: int, kingdom_b: int, dossier: Dict[str, Any]
    ) -> tuple[list[discord.Embed], list[discord.File]]:
        """The dossier with its rankings as picture cards, or as one text embed when they cannot be drawn."""
        intel_a = dossier.get("kingdom_a") or {}
        intel_b = dossier.get("kingdom_b") or {}
        names = (f"Kingdom {kingdom_a}", f"Kingdom {kingdom_b}")
        alliances = await render_card(
            ranking_card_renderer.render_versus_card,
            *names,
            [
                VersusSection(
                    "Top alliances",
                    "power",
                    self._ranked_rows(intel_a.get("alliances"), "power"),
                    self._ranked_rows(intel_b.get("alliances"), "power"),
                )
            ],
        )
        players = await render_card(
            ranking_card_renderer.render_versus_card,
            *names,
            [
                VersusSection(
                    "Top power",
                    "player power",
                    self._ranked_rows(intel_a.get("top_power"), "score"),
                    self._ranked_rows(intel_b.get("top_power"), "score"),
                ),
                VersusSection(
                    "Top kills",
                    "kills",
                    self._ranked_rows(intel_a.get("top_kills"), "score"),
                    self._ranked_rows(intel_b.get("top_kills"), "score"),
                ),
            ],
        )
        if alliances is None or players is None:
            return [self._build_dossier_embed(kingdom_a, kingdom_b, dossier)], []
        embed = self._build_dossier_embed(kingdom_a, kingdom_b, dossier, pictures=True)
        embed.set_image(url=f"attachment://{self.DOSSIER_ALLIANCES_FILENAME}")
        second = discord.Embed(color=embed.color)
        second.set_image(url=f"attachment://{self.DOSSIER_PLAYERS_FILENAME}")
        # The footer belongs under the last card.
        second.set_footer(text=embed.footer.text)
        embed.remove_footer()
        files = [
            *card_files(alliances, self.DOSSIER_ALLIANCES_FILENAME),
            *card_files(players, self.DOSSIER_PLAYERS_FILENAME),
        ]
        return [embed, second], files

    def _build_dossier_embed(
        self, kingdom_a: int, kingdom_b: int, dossier: Dict[str, Any], *, pictures: bool = False
    ) -> discord.Embed:
        """One mobile-friendly embed: per-kingdom castle + rankings, then KvK record.

        With ``pictures`` the rankings are left out: they are on the attached cards.
        """
        intel_a = dossier.get("kingdom_a") or {}
        intel_b = dossier.get("kingdom_b") or {}
        head_to_head = dossier.get("head_to_head") or {"ok": False}
        embed = discord.Embed(
            title=f"🛡️ KvK Prep · K{kingdom_a} vs K{kingdom_b}",
            description=self._dossier_headline(kingdom_a, kingdom_b, intel_a, intel_b, head_to_head),
            color=EmbedColors.INFO,
        )
        for kid, intel in ((kingdom_a, intel_a), (kingdom_b, intel_b)):
            embed.add_field(
                name=f"🏰 Kingdom {kid} · Castle & throne",
                value=self._format_castle_section(kid, intel.get("castle")),
                inline=False,
            )
            if pictures:
                continue
            embed.add_field(
                name=f"🤝 K{kid} top alliances",
                value=self._format_ranked_alliances(intel.get("alliances")),
                inline=True,
            )
            embed.add_field(name=f"💪 K{kid} top power", value=self._format_ranked_players(intel.get("top_power")), inline=True)
            embed.add_field(name=f"🗡️ K{kid} top kills", value=self._format_ranked_players(intel.get("top_kills")), inline=True)
        embed.add_field(
            name="📜 KvK record",
            value=self._format_dossier_record(kingdom_a, kingdom_b, intel_a, intel_b, head_to_head),
            inline=False,
        )
        embed.set_footer(
            text="Cached up to 10 min · Sources: KingShot Data API, kingshot.net · Unavailable = source did not respond"
        )
        return self._fit_embed(embed)

    def _dossier_headline(
        self,
        kingdom_a: int,
        kingdom_b: int,
        intel_a: Dict[str, Any],
        intel_b: Dict[str, Any],
        head_to_head: Dict[str, Any],
    ) -> str:
        lines = []
        home_held = 0
        for kid, intel in ((kingdom_a, intel_a), (kingdom_b, intel_b)):
            castle = intel.get("castle") or {}
            if not castle.get("ok"):
                lines.append(f"❔ K{kid} castle status unavailable")
                continue
            data = castle["data"]
            if data.get("foreign_owner"):
                lines.append(f"⚠️ K{kid}'s castle is held by **K{data.get('owner_kid', '?')}**")
            else:
                home_held += 1
        if home_held == 2:
            lines.append("🏰 Both castles are held by their home kingdoms")

        if head_to_head.get("ok"):
            data = head_to_head["data"]
            count = data.get("match_count") or 0
            if count:
                summary = data.get("kingdom_a") or {}
                lines.append(
                    f"⚔️ Met {count}× before · K{kingdom_a} castle {self._record(summary, 'wins', 'losses')}, "
                    f"prep {self._record(summary, 'prepWins', 'prepLosses')}"
                )
            else:
                lines.append("⚔️ No previous direct KvK matches")
        else:
            lines.append("❔ Direct match history unavailable")
        return "\n".join(lines)

    def _format_castle_section(self, kid: int, section: Dict[str, Any] | None) -> str:
        if not section or not section.get("ok"):
            return self._unavailable(section)
        data = section["data"]
        lines = []

        king = data.get("king")
        if king:
            origin = (
                f"from K{king['home_kid']} (foreign)" if king.get("is_foreign")
                else f"K{king['home_kid']}" if king.get("home_kid") is not None
                else ""
            )
            lines.append(f"👑 King: **{self._player_label(king)}**" + (f" · {origin}" if origin else ""))
        else:
            lines.append("👑 Throne vacant")

        owner = data.get("owner_kid")
        if data.get("foreign_owner"):
            lines.append(f"⚠️ Castle occupied by K{owner if owner is not None else '?'}")
        elif owner is not None:
            lines.append(f"🏰 Castle held by home kingdom K{owner}")

        kvk = data.get("kvk")
        if kvk:
            text = f"🏆 Ruling alliance KvK wins: {kvk.get('victories', 0)}"
            if kvk.get("last_opponent_kid") is not None:
                text += f" · last vs K{kvk['last_opponent_kid']}"
                if kvk.get("last_ts"):
                    text += f" <t:{kvk['last_ts']}:d>"
            lines.append(text)
        elif not data.get("kvk_included"):
            lines.append("🏆 Ruling alliance KvK wins: unavailable")

        reigns = data.get("recent_reigns") or []
        if reigns:
            lines.append("📜 Recent kings:")
            for reign in reigns:
                parts = [self._player_label(reign)]
                if reign.get("ts"):
                    parts.append(f"<t:{reign['ts']}:d>")
                if reign.get("is_cross_mode"):
                    parts.append("⚔️ KvK")
                if reign.get("is_invader") and reign.get("home_kid") is not None:
                    parts.append(f"🚩 from K{reign['home_kid']}")
                lines.append("• " + " · ".join(parts))

        top_kings = data.get("top_kings") or []
        if top_kings:
            crowned = ", ".join(
                f"{self._player_label(tally)} ×{tally.get('king_terms', 0)}"
                + (f" ({tally['high_king_terms']} KvK)" if tally.get("high_king_terms") else "")
                for tally in top_kings
            )
            lines.append(f"🥇 Most terms: {crowned}")
        return "\n".join(lines)

    def _format_ranked_alliances(self, section: Dict[str, Any] | None) -> str:
        if not section or not section.get("ok"):
            return self._unavailable(section)
        rows = section["data"] or []
        if not rows:
            return "No ranking returned"
        lines = []
        for index, row in enumerate(rows, 1):
            tag = f"`[{row['abbr']}]` " if row.get("abbr") else ""
            name = self._escape(row.get("name") or "", 14)
            lines.append(f"{index}. {tag}{name} · {self._compact_number(row.get('power'))}".replace("  ", " "))
        return "\n".join(lines)

    def _format_ranked_players(self, section: Dict[str, Any] | None) -> str:
        if not section or not section.get("ok"):
            return self._unavailable(section)
        rows = section["data"] or []
        if not rows:
            return "No ranking returned"
        lines = []
        for index, row in enumerate(rows, 1):
            tag = f"`[{row['abbr']}]` " if row.get("abbr") else ""
            name = self._escape(row.get("name") or "Unknown", 14)
            lines.append(f"{row.get('rank') or index}. {tag}{name} · {self._compact_number(row.get('score'))}")
        return "\n".join(lines)

    def _format_dossier_record(
        self,
        kingdom_a: int,
        kingdom_b: int,
        intel_a: Dict[str, Any],
        intel_b: Dict[str, Any],
        head_to_head: Dict[str, Any],
    ) -> str:
        lines = []
        for kid, intel in ((kingdom_a, intel_a), (kingdom_b, intel_b)):
            section = intel.get("kvk_history") or {}
            if not section.get("ok"):
                lines.append(f"**K{kid}**: {self._unavailable(section)}")
                continue
            summary = section["data"]
            if not summary.get("matchCount"):
                lines.append(f"**K{kid}**: no tracked KvK matches")
                continue
            lines.append(
                f"**K{kid}**: Castle {self._record(summary, 'wins', 'losses')} · "
                f"Prep {self._record(summary, 'prepWins', 'prepLosses')} · "
                f"Streak {self._format_metric('streak', summary.get('currentStreak'))}"
            )
            for match in summary.get("recent") or []:
                lines.append(f"↳ Last: {self._format_compact_match(match)}")

        if head_to_head.get("ok"):
            recent = head_to_head["data"].get("recent") or []
            if recent:
                lines.append(f"**Direct (K{kingdom_a} view)**:")
                lines.extend(f"• {self._format_compact_match(match)}" for match in recent)
        return "\n".join(lines) or "Unavailable"

    def _format_compact_match(self, match: Dict[str, Any]) -> str:
        season = match.get("season_id")
        opponent = match.get("opponent")
        return (
            f"#{season if season is not None else '?'} vs K{opponent if opponent is not None else '?'} · "
            f"Castle {self._format_history_result(match.get('castleResult'))} · "
            f"Prep {self._format_history_result(match.get('prepResult'))}"
        )

    @staticmethod
    def _player_label(player: Dict[str, Any]) -> str:
        name = KVKHandler._escape(player.get("name") or "Unknown", 18)
        return f"{name} `[{player['abbr']}]`" if player.get("abbr") else name

    @staticmethod
    def _escape(text: str, limit: int) -> str:
        text = text.strip()
        if len(text) > limit:
            text = text[: limit - 1] + "…"
        return discord.utils.escape_markdown(discord.utils.escape_mentions(text))

    @staticmethod
    def _unavailable(section: Dict[str, Any] | None) -> str:
        reason = (section or {}).get("error")
        return f"Unavailable ({reason})" if reason else "Unavailable"

    @staticmethod
    def _compact_number(value: Any) -> str:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return "?"
        for divisor, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
            if abs(number) >= divisor:
                return f"{number / divisor:.2f}".rstrip("0").rstrip(".") + suffix
        return f"{int(number):,}"

    @staticmethod
    def _fit_embed(embed: discord.Embed) -> discord.Embed:
        """Keep every field and the total inside Discord's embed limits."""
        for index, field in enumerate(embed.fields):
            value = str(field.value) or "Unavailable"
            if len(value) > EMBED_FIELD_LIMIT:
                value = value[: EMBED_FIELD_LIMIT - 1] + "…"
            embed.set_field_at(index, name=str(field.name)[:256], value=value, inline=field.inline)
        while len(embed) > EMBED_TOTAL_LIMIT and embed.fields:
            index, field = max(enumerate(embed.fields), key=lambda item: len(str(item[1].value)))
            value = str(field.value)
            if len(value) <= 1:
                break
            keep = max(1, len(value) - (len(embed) - EMBED_TOTAL_LIMIT) - 1)
            embed.set_field_at(index, name=field.name, value=value[:keep] + "…", inline=field.inline)
        return embed

    # ------------------------------------------------------------ match history

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

        season = latest.get("season_id")
        opponent = latest.get("opponent")
        return (
            f"Latest: **KvK #{season if season is not None else '?'}** vs "
            f"**K{opponent if opponent is not None else '?'}** on **{latest.get('season_date') or 'Unknown date'}**\n"
            f"Castle {self._format_history_result(latest.get('castleResult'))} · "
            f"Prep {self._format_history_result(latest.get('prepResult'))}"
        )

    def _format_history_line(self, entry: Dict[str, Any]) -> str:
        """Format one normalized KVK match for Discord history (two short lines)."""
        season = entry.get("season_id")
        opponent = entry.get("opponent")
        return (
            f"#{season if season is not None else '?'} · {entry.get('season_date') or 'Unknown date'} · "
            f"vs **K{opponent if opponent is not None else '?'}** · {self._format_side(entry.get('side'))}\n"
            f"Castle {self._format_history_result(entry.get('castleResult'))} · "
            f"Prep {self._format_history_result(entry.get('prepResult'))}"
        )

    def _build_history_embed(
        self, kingdom_number: int, stats: Dict[str, Any], *, pictures: bool = False
    ) -> discord.Embed:
        """The record of a kingdom.

        With ``pictures`` the numbers and the last seasons are on the attached card;
        without it they are text fields, with the same last seasons listed.
        """
        history = stats.get("history") or []
        embed = discord.Embed(
            title=f"⚔️ KVK History · Kingdom {kingdom_number}",
            description=self._build_latest_match_summary(stats.get("latestMatch")) if stats.get("matchCount") is not None else "Match history unavailable.",
            color=EmbedColors.INFO,
        )
        embed.set_footer(text=f"{len(history)} matches tracked · Source: Kingshot KVK")
        if pictures:
            return embed
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
        # Newest first, as the service returns them.
        lines = [self._format_history_line(entry)[:200] for entry in history[:HISTORY_TEXT_MATCHES]]
        for start in range(0, len(lines), HISTORY_TEXT_MATCHES_PER_FIELD):
            embed.add_field(
                name="Last Matches" if start == 0 else "Last Matches (continued)",
                value="\n".join(lines[start:start + HISTORY_TEXT_MATCHES_PER_FIELD]),
                inline=False,
            )
        return self._fit_embed(embed)

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
            return "⚔️ Attack"
        if normalized == "defender":
            return "🛡️ Defense"
        return "Side unknown"

    @staticmethod
    def _format_history_result(result: Any) -> str:
        """Result with an icon and a word, so it reads without color or emoji."""
        normalized = str(result or "Unknown").strip().lower()
        if normalized == "win":
            return "✅ Win"
        if normalized == "loss":
            return "❌ Loss"
        if normalized == "unknown":
            return "❔ Unknown"
        return str(result or "❔ Unknown")
