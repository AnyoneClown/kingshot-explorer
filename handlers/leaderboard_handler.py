"""``/leaderboard``: the game's ranking boards for a kingdom or for all kingdoms."""

from __future__ import annotations

import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from handlers.permissions import interaction_guild_id
from handlers.ui import EmbedColors, OwnedView, build_status_embed, card_files, render_card
from services import ranking_card_renderer
from services.leaderboard_service import (
    BOARDS,
    BOARDS_BY_TYPE,
    DEFAULT_BOARD,
    Board,
    BoardEntry,
    BoardSnapshot,
    LeaderboardService,
)

logger = logging.getLogger(__name__)

MAX_KINGDOM_NUMBER = 99999
PAGE_SIZE = ranking_card_renderer.MAX_LEADERBOARD_ROWS
# Public browse panels stay usable for a typical channel discussion.
BROWSE_PANEL_TIMEOUT_SECONDS = 900
SCOPE_KINGDOM = "kingdom"
SCOPE_GLOBAL = "global"


def entry_name(board: Board, entry: BoardEntry) -> str:
    """A row's name; an id stands in for a name that could not be found."""
    if entry.name:
        return entry.name
    if board.alliance:
        # A tag alone is a fine label; without one the id is all there is.
        return "Alliance" if entry.tag else f"Alliance {entry.id or 'unknown'}"
    return f"Player {entry.id}" if entry.id else "Unknown player"


class LeaderboardView(OwnedView):
    """One message that pages through a board and switches board and scope."""

    def __init__(
        self,
        handler: "LeaderboardHandler",
        author_id: int,
        *,
        board: Board,
        kingdom: Optional[int],
        global_scope: bool,
    ):
        super().__init__(author_id, timeout=BROWSE_PANEL_TIMEOUT_SECONDS)
        self.handler = handler
        self.board = board
        # The kingdom of the kingdom scope; None when the panel was opened without one.
        self.kingdom = kingdom
        self.global_scope = global_scope or kingdom is None
        self.snapshot = BoardSnapshot(board=board, kingdom=None if self.global_scope else kingdom)
        self.page = 0
        self.card: bytes | None = None
        self._loading = False
        self._update_controls()

    # ------------------------------------------------------------------ state

    @property
    def page_count(self) -> int:
        return max(1, -(-len(self.snapshot.entries) // PAGE_SIZE))

    def page_entries(self) -> list[BoardEntry]:
        start = self.page * PAGE_SIZE
        return self.snapshot.entries[start:start + PAGE_SIZE]

    def rows(self) -> list[ranking_card_renderer.RankedRow]:
        return [
            ranking_card_renderer.RankedRow(
                name=entry_name(self.board, entry),
                value=entry.value,
                text=entry.text,
                rank=entry.rank,
                tag=entry.tag,
            )
            for entry in self.page_entries()
        ]

    def _update_controls(self) -> None:
        self.board_select.options = [
            discord.SelectOption(
                label=board.name,
                value=str(board.type),
                description=("Alliances" if board.alliance else "Players") + f" · {board.unit}",
                default=board.type == self.board.type,
            )
            for board in BOARDS
        ]
        self.previous_button.disabled = self.page == 0
        self.next_button.disabled = self.page >= self.page_count - 1
        if self.global_scope:
            self.scope_button.label = f"Kingdom {self.kingdom}" if self.kingdom is not None else "Kingdom"
            self.scope_button.disabled = self.kingdom is None
        else:
            self.scope_button.label = "All kingdoms"
            self.scope_button.disabled = False

    async def load(self) -> None:
        """Fetch the selected board and draw its first page."""
        self.snapshot = await self.handler.service.get_board(
            self.board, None if self.global_scope else self.kingdom
        )
        self.page = 0
        await self.draw()

    async def draw(self) -> None:
        """Name the alliances of the current page and draw its card."""
        entries = self.page_entries()
        self.card = None
        if entries:
            await self.handler.service.name_alliances(self.snapshot, entries)
            self.card = await render_card(
                ranking_card_renderer.render_leaderboard_card,
                self.snapshot.title,
                self.rows(),
                note=self.board.unit,
                scale=self.snapshot.top_value,
                count=len(self.snapshot.entries),
            )
        self._update_controls()

    # ---------------------------------------------------------------- message

    def files(self) -> list[discord.File]:
        return card_files(self.card, ranking_card_renderer.LEADERBOARD_FILENAME)

    def build_embed(self) -> discord.Embed:
        title = f"🏆 {self.snapshot.title}"
        if self.snapshot.error or not self.snapshot.entries:
            return build_status_embed(
                title=title,
                description=self.snapshot.error or "No ranking was returned.",
                color=EmbedColors.WARNING,
                footer="Pick another board below",
            )
        entries = self.page_entries()
        embed = discord.Embed(title=title, color=EmbedColors.INFO)
        if self.card:
            # The ranking is on the attached card.
            embed.set_image(url=f"attachment://{ranking_card_renderer.LEADERBOARD_FILENAME}")
        else:
            lines = []
            for entry in entries:
                name = discord.utils.escape_markdown(entry_name(self.board, entry)[:80])
                tag = f"`[{entry.tag}]` " if entry.tag else ""
                lines.append(f"**#{entry.rank}** {tag}{name} · {entry.text}")
            embed.description = "\n".join(lines)
        embed.set_footer(
            text=(
                f"Ranks {entries[0].rank}–{entries[-1].rank} of {len(self.snapshot.entries)} · "
                f"{self.board.unit} · Page {self.page + 1}/{self.page_count}"
            )
        )
        return embed

    # --------------------------------------------------------------- controls

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not await super().interaction_check(interaction):
            return False
        if self._loading:
            await interaction.response.send_message("Still loading. Try again in a moment.", ephemeral=True)
            return False
        return True

    # Not named ``_refresh``: discord.py calls ``View._refresh(components)`` on every
    # edit of the panel's message, and an override with another signature stops the bot.
    async def _show(self, interaction: discord.Interaction, *, reload: bool) -> None:
        self._loading = True
        try:
            await interaction.response.defer()
            if reload:
                await self.load()
            else:
                await self.draw()
            await interaction.edit_original_response(embed=self.build_embed(), attachments=self.files(), view=self)
        finally:
            self._loading = False

    @discord.ui.select(placeholder="Pick a board", row=0)
    async def board_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        self.board = BOARDS_BY_TYPE.get(int(select.values[0]), self.board)
        await self._show(interaction, reload=True)

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary, row=1)
    async def previous_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = max(0, self.page - 1)
        await self._show(interaction, reload=False)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary, row=1)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = min(self.page_count - 1, self.page + 1)
        await self._show(interaction, reload=False)

    @discord.ui.button(label="All kingdoms", style=discord.ButtonStyle.primary, row=1)
    async def scope_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.global_scope = not self.global_scope
        await self._show(interaction, reload=True)


class LeaderboardHandler:
    """Registers ``/leaderboard`` and opens its panel."""

    def __init__(
        self,
        leaderboard_service: LeaderboardService,
        bot: commands.Bot,
        guild_configuration_service=None,
    ):
        self.service = leaderboard_service
        self._bot = bot
        self._guild_configuration_service = guild_configuration_service
        logger.info("LeaderboardHandler initialized")

    def register_commands(self) -> None:
        @self._bot.tree.command(
            name="leaderboard",
            description="Rankings of a kingdom or of all kingdoms: power, kills, alliances, trials",
        )
        @app_commands.describe(
            board="Which ranking to show. Personal Power when left empty",
            kingdom="Kingdom number. Leave empty to use this server's kingdom",
            scope="One kingdom or all kingdoms together",
        )
        @app_commands.choices(
            board=[app_commands.Choice(name=board.name, value=board.type) for board in BOARDS],
            scope=[
                app_commands.Choice(name="Kingdom", value=SCOPE_KINGDOM),
                app_commands.Choice(name="All kingdoms", value=SCOPE_GLOBAL),
            ],
        )
        @app_commands.checks.cooldown(4, 30.0)
        async def leaderboard(
            interaction: discord.Interaction,
            board: Optional[app_commands.Choice[int]] = None,
            kingdom: Optional[app_commands.Range[int, 1, MAX_KINGDOM_NUMBER]] = None,
            scope: Optional[app_commands.Choice[str]] = None,
        ):
            await self._handle_leaderboard(
                interaction,
                board.value if board is not None else None,
                kingdom,
                scope.value if scope is not None else None,
            )

    async def _server_kingdom(self, guild_id: Optional[int]) -> Optional[int]:
        if guild_id is None or self._guild_configuration_service is None:
            return None
        try:
            settings = await self._guild_configuration_service.get_settings(guild_id)
        except Exception:
            logger.warning("Could not load settings for guild %s", guild_id, exc_info=True)
            return None
        return getattr(settings, "kingdom_id", None)

    async def _handle_leaderboard(
        self,
        interaction: discord.Interaction,
        board_type: Optional[int] = None,
        kingdom: Optional[int] = None,
        scope: Optional[str] = None,
    ) -> None:
        guild_id = interaction_guild_id(interaction)
        # Acknowledge first: the settings lookup below may wait on the database.
        await interaction.response.defer(thinking=True, ephemeral=True)
        board = BOARDS_BY_TYPE.get(board_type, DEFAULT_BOARD) if board_type is not None else DEFAULT_BOARD
        global_scope = scope == SCOPE_GLOBAL
        if kingdom is None:
            # Also for the global board: its panel can then switch to the server's kingdom.
            kingdom = await self._server_kingdom(guild_id)
        if kingdom is None and not global_scope:
            hint = (
                " A server manager can set this server's kingdom in `/settings`, "
                "then `/leaderboard` works without a number."
                if guild_id is not None
                else ""
            )
            await interaction.edit_original_response(
                embed=build_status_embed(
                    title="Which kingdom?",
                    description=(
                        "Add the kingdom number: `/leaderboard kingdom:830`, "
                        f"or choose `scope: All kingdoms`.{hint}"
                    ),
                    color=EmbedColors.INFO,
                ),
            )
            return

        logger.info(
            "Leaderboard %s for %s requested by %s in %s",
            board.name,
            "all kingdoms" if global_scope else f"kingdom {kingdom}",
            interaction.user.id,
            guild_id if guild_id is not None else "DM",
        )
        view = LeaderboardView(
            self, interaction.user.id, board=board, kingdom=kingdom, global_scope=global_scope
        )
        await view.load()
        # Complete the private defer before posting the public panel.
        await interaction.edit_original_response(content="Leaderboard ready.")
        view.message = await interaction.followup.send(
            embed=view.build_embed(), files=view.files(), view=view, wait=True
        )
        try:
            await interaction.delete_original_response()
        except discord.HTTPException:
            logger.debug("Could not remove the private leaderboard receipt", exc_info=True)
