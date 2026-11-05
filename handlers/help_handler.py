"""A private starting point for the bot's available commands."""

from functools import partial

import discord
from discord.ext import commands

from handlers.permissions import AccessPolicy, interaction_guild_id
from handlers.ui import EmbedColors, OwnedView, build_status_embed, send_ui_error


GROUPS = {
    "Players": (
        "Look up a Governor ID or browse a kingdom's Mystic Trial leaderboard.",
        (
            ("stats", "View player", "/stats player_id:123456"),
            ("scout", "Scout kingdom", "/scout kingdom_number:830 limit:5"),
            ("listplayers", "Server roster", "/listplayers"),
        ),
    ),
    "KVK": (
        "Browse match history, compare two kingdoms, or build a prep dossier before a KvK.",
        (
            ("kvk", "Kingdom history", "/kvk kingdom_number:830"),
            ("kvk_compare", "Compare kingdoms", "/kvk_compare kingdom_a:830 kingdom_b:831"),
            ("kvk_prep", "Prep dossier", "/kvk_prep kingdom_a:830 kingdom_b:831"),
        ),
    ),
    "Alliance": (
        "Track daily and weekly member power, roster changes and possibly inactive members. "
        "A server manager starts tracking with /alliance kid:830 alliance:<selection>; "
        "history starts with the first complete daily snapshot.",
        (("alliance", "Power trends", "/alliance"),),
    ),
    "Gifts": (
        "Find active codes and add your Governor ID with /addplayer to receive new codes "
        "automatically. Server managers can import alliances and run /redeem for this "
        "server's roster.",
        (
            ("giftcodes", "Available codes", "/giftcodes"),
            ("addplayer", "Add players", "/addplayer player_ids:123456,654321"),
            ("removeplayer", "Remove player", "/removeplayer player_id:123456"),
            ("addalliance", "Import alliance", "/addalliance kid:830 alliance:FKA"),
            ("redeem", "Redeem code", "/redeem gift_code:EXAMPLE"),
        ),
    ),
    "Events": (
        "Review upcoming reminders. Creating, editing, and cancelling reminders "
        "requires a server manager.",
        (
            ("events", "Upcoming events", "/events"),
            ("schedule", "Create event", "/schedule"),
            ("cancel", "Cancel event", "/cancel event_id:12"),
        ),
    ),
    "Knowledge": (
        "Ask questions answered from the stored Kingshot knowledge base. You can also mention the "
        "bot in chat to ask about this server's alliance, events or active gift codes.",
        (("kingshot_ask", "Ask a question", "/kingshot_ask question:How does Bear Trap work?"),),
    ),
    "Settings": (
        "Server-wide settings: gift-code and alliance report channels, the bot manager role, "
        "voice and AI replies. Available to server managers.",
        (("configure", "Open settings", "/configure"),),
    ),
}
# Actions only offered to server managers (Manage Server, the manager role, or bot operators).
ADMIN_COMMANDS = {"redeem", "schedule", "cancel", "configure", "addalliance"}
# Friendlier form labels for slash-command parameters.
FIELD_LABELS = {
    "player_ids": "Player IDs",
    "player_id": "Player ID",
    "kid": "Kingdom ID",
    "gift_code": "Gift code",
    "event_id": "Event ID",
    "kingdom_number": "Kingdom number",
    "kingdom_a": "First kingdom",
    "kingdom_b": "Second kingdom",
}


def field_label(name: str) -> str:
    return FIELD_LABELS.get(name) or name.replace("_", " ").capitalize().replace(" id", " ID")


class CommandInputModal(discord.ui.Modal):
    """Collect the required text/number inputs for a help action."""

    def __init__(self, handler, command, label: str, author_id: int):
        super().__init__(title=label, timeout=180)
        self.handler = handler
        self.command = command
        self.author_id = author_id
        self.inputs = []
        for parameter in command.parameters:
            if not parameter.required:
                continue
            field = discord.ui.TextInput(
                label=field_label(parameter.name)[:45],
                placeholder=parameter.description[:100],
                max_length=20 if parameter.type == discord.AppCommandOptionType.integer else 1000,
            )
            self.inputs.append((parameter, field))
            self.add_item(field)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Open your own /help to use this form.", ephemeral=True)
            return
        values = {}
        for parameter, field in self.inputs:
            label = field_label(parameter.name)
            value = field.value.strip()
            if not value:
                await interaction.response.send_message(f"Enter {label.lower()}.", ephemeral=True)
                return
            if parameter.type == discord.AppCommandOptionType.integer:
                try:
                    value = int(value)
                except ValueError:
                    await interaction.response.send_message(
                        f"{label} must be a whole number. Reopen the form to try again.", ephemeral=True
                    )
                    return
            values[parameter.name] = value
        await self.handler.run_command(interaction, self.command.name, values)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await send_ui_error(interaction, error)


class HelpView(OwnedView):
    def __init__(self, handler, interaction: discord.Interaction):
        super().__init__(interaction.user.id)
        self.handler = handler
        self.groups = handler.available_groups(interaction)
        self.category = next(iter(self.groups))
        self.category_select.options = [discord.SelectOption(label=name) for name in self.groups]
        self._update_actions()

    def _update_actions(self) -> None:
        for item in list(self.children):
            if item.row == 1:
                self.remove_item(item)
        for option in self.category_select.options:
            option.default = option.label == self.category
        for name, label, _ in self.groups[self.category][1]:
            button = discord.ui.Button(label=label, style=discord.ButtonStyle.primary, row=1)
            button.callback = partial(self.handler.open_command, name=name, label=label)
            self.add_item(button)

    def build_embed(self) -> discord.Embed:
        description, actions = self.groups[self.category]
        embed = build_status_embed(
            title=f"Help · {self.category}",
            description=description,
            color=EmbedColors.INFO,
            footer="Player and KVK reports are posted in this channel. Reopen /help when controls expire.",
        )
        for _, label, example in actions:
            embed.add_field(name=label, value=f"`{example}`", inline=False)
        return embed

    @discord.ui.select(placeholder="Choose a feature", row=0)
    async def category_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        self.category = select.values[0]
        self._update_actions()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="Bot status", style=discord.ButtonStyle.secondary, row=2)
    async def status_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handler.run_command(interaction, "status", {})


class HelpHandler:
    def __init__(
        self,
        bot: commands.Bot,
        admin_user_ids: set[int] | None = None,
        access_policy: AccessPolicy | None = None,
    ):
        self._bot = bot
        self._admin_user_ids = admin_user_ids if admin_user_ids is not None else set()
        self._access_policy = access_policy
        self._manager_cache: dict[tuple[int, int | None], bool] = {}

    @property
    def _access(self) -> AccessPolicy:
        # Without an injected policy, read the admin set live so changes apply immediately.
        return self._access_policy or AccessPolicy(self._admin_user_ids)

    async def refresh_access(self, interaction: discord.Interaction) -> bool:
        """Re-evaluate (and remember) whether the user may manage the bot here."""
        allowed = interaction.guild is not None and await self._access.can_manage(interaction)
        self._manager_cache[(interaction.user.id, interaction_guild_id(interaction))] = allowed
        return allowed

    def _is_manager(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            return False
        cached = self._manager_cache.get((interaction.user.id, interaction_guild_id(interaction)))
        if cached is not None:
            return cached
        return self._access.is_super_admin(interaction.user.id)

    def register_commands(self) -> None:
        @self._bot.tree.command(name="help", description="Browse available features and start a workflow")
        async def help_command(interaction: discord.Interaction):
            await self.refresh_access(interaction)
            view = HelpView(self, interaction)
            await interaction.response.send_message(embed=view.build_embed(), view=view, ephemeral=True)
            view.message = await interaction.original_response()

    def available_groups(self, interaction: discord.Interaction) -> dict:
        groups = {}
        is_admin = self._is_manager(interaction)
        for category, (description, actions) in GROUPS.items():
            available = tuple(
                action for action in actions
                if self._bot.tree.get_command(action[0]) is not None
                and (action[0] not in ADMIN_COMMANDS or is_admin)
                and (category not in {"Events", "Settings", "Gifts", "Alliance"} or interaction.guild is not None)
            )
            if available:
                groups[category] = (description, available)
        if self._bot.get_command("t") is not None:
            prefix = self._bot.command_prefix
            groups["Translation"] = (
                f"Reply to the message you want translated, then send `{prefix}en` for English "
                f"or `{prefix}t Spanish` for another language.\n\n"
                "Members with the configured Translator role have their messages translated to English automatically.",
                (),
            )
        return groups

    def _allowed(self, interaction: discord.Interaction, name: str) -> bool:
        return name == "status" or any(
            action[0] == name for _, actions in self.available_groups(interaction).values() for action in actions
        )

    async def open_command(self, interaction: discord.Interaction, *, name: str, label: str) -> None:
        await self.refresh_access(interaction)
        if not self._allowed(interaction, name):
            await interaction.response.send_message("That action is not available here. Reopen /help.", ephemeral=True)
            return
        command = self._bot.tree.get_command(name)
        if name == "addalliance":
            await interaction.response.send_message(
                embed=build_status_embed(
                    title="Import alliance",
                    description=(
                        "Run `/addalliance` in this channel. Enter the kingdom number in `kid`, "
                        "then choose `alliance` from Discord's autocomplete list.\n\n"
                        "The list shows the top 15 alliances by power as `[TAG] Name - N members`. "
                        "Options may take a moment to load; you can also enter the exact 3-character tag.\n\n"
                        "Example: `/addalliance kid:830 alliance:FKA`"
                    ),
                ),
                ephemeral=True,
            )
            return
        if any(parameter.required for parameter in command.parameters):
            await interaction.response.send_modal(CommandInputModal(self, command, label, interaction.user.id))
        else:
            await self.run_command(interaction, name, {})

    async def run_command(self, interaction: discord.Interaction, name: str, values: dict) -> None:
        command = self._bot.tree.get_command(name)
        await self.refresh_access(interaction)
        if command is None or not self._allowed(interaction, name):
            await interaction.response.send_message("That action is not available here. Reopen /help.", ephemeral=True)
            return
        # The registered callbacks retain the same validation and authorization as slash commands.
        await command.callback(interaction, **values)
