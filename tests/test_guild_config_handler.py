import asyncio
from types import SimpleNamespace

from handlers.guild_config_handler import GuildConfigHandler, GuildConfigView


class FakeGuildConfigService:
    def __init__(self, initial_use_random_replies=False):
        self.guild_config = SimpleNamespace(
            guild_id=999,
            use_random_replies=initial_use_random_replies,
        )
        self.random_reply_updates = []

    async def get_or_create_guild_configuration(self, guild_id):
        assert guild_id == 999
        return self.guild_config

    async def get_or_create_for_guild(self, guild_id):
        return await self.get_or_create_guild_configuration(guild_id)

    async def set_use_random_replies(self, guild_id, use_random_replies):
        assert guild_id == 999
        self.random_reply_updates.append(use_random_replies)
        self.guild_config.use_random_replies = use_random_replies
        return self.guild_config

    async def set_use_random_replies_for_guild(self, guild_id, use_random_replies):
        return await self.set_use_random_replies(guild_id, use_random_replies)


class FakeResponse:
    def __init__(self):
        self.edited_embed = None
        self.edited_view = None
        self.sent_message = None
        self.sent_embed = None
        self.sent_view = None
        self.deferred = None

    async def edit_message(self, *, embed=None, view=None):
        self.edited_embed = embed
        self.edited_view = view

    async def send_message(self, content=None, *, embed=None, view=None, ephemeral=False):
        self.sent_message = (content, ephemeral)
        self.sent_embed = embed
        self.sent_view = view

    async def defer(self, *, ephemeral=False, thinking=False):
        self.deferred = (ephemeral, thinking)

    def is_done(self):
        return self.deferred is not None or self.sent_message is not None


class FakeInteraction:
    def __init__(self, user_id, guild=None):
        self.user = SimpleNamespace(id=user_id)
        self.guild = guild
        self.response = FakeResponse()
        self.edited_original_embed = None
        self.edited_original_view = None

    async def edit_original_response(self, *, embed=None, view=None):
        self.edited_original_embed = embed
        self.edited_original_view = view


async def _create_view(service=None):
    return GuildConfigView(
        guild_id=999,
        author_id=111,
        guild_name="Test Guild",
        guild_config_service=service or FakeGuildConfigService(),
    )


def test_guild_config_view_builds_embed_with_default_off_statuses():
    async def run_test():
        view = await _create_view()
        return await view.build_embed()

    embed = asyncio.run(run_test())

    assert embed.title == "Bot Configuration"
    assert "Voice replies" not in embed.description
    assert "AI replies: **Disabled**" in embed.description
    assert "✅ Everything is set up." not in embed.description


def test_toggle_random_replies_button_updates_config_and_message():
    service = FakeGuildConfigService(initial_use_random_replies=False)
    interaction = FakeInteraction(user_id=111)

    async def run_test():
        view = await _create_view(service)
        await view.toggle_random_replies_button.callback(interaction)
        return interaction

    interaction = asyncio.run(run_test())

    assert service.random_reply_updates == [True]
    assert interaction.response.deferred == (False, False)
    assert interaction.edited_original_embed.title == "Bot Configuration"
    assert "AI replies: **Enabled**" in interaction.edited_original_embed.description


def test_interaction_check_rejects_other_users():
    interaction = FakeInteraction(user_id=222)

    async def run_test():
        view = await _create_view(FakeGuildConfigService())
        return await view.interaction_check(interaction)

    allowed = asyncio.run(run_test())

    assert allowed is False
    assert interaction.response.sent_message == ("Only the command user can control this panel.", True)


def test_configure_rejects_members_without_manage_server():
    handler = GuildConfigHandler(
        bot=SimpleNamespace(),
        guild_config_service=FakeGuildConfigService(),
        admin_user_ids={111},
    )
    interaction = FakeInteraction(
        user_id=222,
        guild=SimpleNamespace(id=999, name="Test Guild"),
    )
    interaction.user.guild_permissions = SimpleNamespace(administrator=False, manage_guild=False)

    asyncio.run(handler._handle_configure(interaction))

    assert interaction.response.sent_message == (None, True)
    assert interaction.response.sent_embed.title == "🔒 Not allowed"
    assert interaction.response.deferred is None


def test_configure_allows_server_managers():
    handler = GuildConfigHandler(
        bot=SimpleNamespace(),
        guild_config_service=FakeGuildConfigService(),
        admin_user_ids={111},
    )
    interaction = FakeInteraction(
        user_id=222,
        guild=SimpleNamespace(id=999, name="Test Guild"),
    )
    interaction.user.guild_permissions = SimpleNamespace(administrator=False, manage_guild=True)

    asyncio.run(handler._handle_configure(interaction))

    assert interaction.response.deferred == (True, True)
    assert interaction.edited_original_embed.title == "Bot Configuration"


def test_configure_allows_env_admin_without_server_admin_permission():
    handler = GuildConfigHandler(
        bot=SimpleNamespace(),
        guild_config_service=FakeGuildConfigService(),
        admin_user_ids={111},
    )
    interaction = FakeInteraction(
        user_id=111,
        guild=SimpleNamespace(id=999, name="Test Guild"),
    )
    interaction.user.guild_permissions = SimpleNamespace(administrator=False)

    asyncio.run(handler._handle_configure(interaction))

    assert interaction.response.deferred == (True, True)
    assert interaction.edited_original_embed.title == "Bot Configuration"
    assert interaction.edited_original_view is not None


class FakeRole:
    def __init__(self, role_id, *, default=False, managed=False):
        self.id = role_id
        self.managed = managed
        self._default = default

    def is_default(self):
        return self._default


def _select_interaction(user_id, *, manage_guild):
    interaction = FakeInteraction(user_id=user_id)
    interaction.user.guild_permissions = SimpleNamespace(administrator=False, manage_guild=manage_guild)
    interaction.followup = SimpleNamespace(send=None)
    return interaction


class UpdatingGuildConfigService(FakeGuildConfigService):
    def __init__(self):
        super().__init__()
        self.guild_config.manager_role_id = None
        self.guild_config.gift_code_channel_id = None
        self.guild_config.alliance_report_channel_id = None
        self.updates = []

    async def update_for_guild(self, guild_id, **changes):
        self.updates.append(changes)
        for name, value in changes.items():
            setattr(self.guild_config, name, value)
        return self.guild_config


def test_manager_role_select_requires_manage_server():
    service = UpdatingGuildConfigService()
    interaction = _select_interaction(111, manage_guild=False)

    async def run_test():
        view = await _create_view(service)
        view.manager_role_select._values = [FakeRole(5)]
        await view.manager_role_select.callback(interaction)

    asyncio.run(run_test())

    assert service.updates == []
    assert interaction.response.sent_embed.title == "🔒 Not allowed"


def test_manager_role_select_saves_role_and_rejects_everyone():
    service = UpdatingGuildConfigService()

    async def run_test(role):
        interaction = _select_interaction(111, manage_guild=True)
        view = await _create_view(service)
        view.manager_role_select._values = [role]
        await view.manager_role_select.callback(interaction)
        return interaction

    asyncio.run(run_test(FakeRole(1, default=True)))
    assert service.updates == []
    interaction = asyncio.run(run_test(FakeRole(5)))
    assert service.updates == [{"manager_role_id": 5}]
    assert "<@&5>" in interaction.edited_original_embed.description


# ------------------------------------------------------------ event calendar


class CalendarConfigService(FakeGuildConfigService):
    """Also stores the settings changed through ``update_for_guild``."""

    def __init__(self, **values):
        super().__init__()
        self.updates = []
        for name, value in values.items():
            setattr(self.guild_config, name, value)

    async def update_for_guild(self, guild_id, **changes):
        assert guild_id == 999
        self.updates.append(changes)
        for name, value in changes.items():
            setattr(self.guild_config, name, value)
        return self.guild_config


class FakeCalendar:
    def __init__(self, verify_error=None, post_result=(True, "Posted today's calendar in <#555>.")):
        self.verify_error = verify_error
        self.post_result = post_result
        self.verified = []
        self.posted = []

    async def verify_kingdom(self, kingdom):
        self.verified.append(kingdom)
        if self.verify_error is not None:
            raise self.verify_error

    async def post_now(self, guild_id):
        self.posted.append(guild_id)
        return self.post_result


class ModalInteraction(FakeInteraction):
    def __init__(self, user_id=111):
        super().__init__(user_id, guild=SimpleNamespace(id=999))
        self.guild_id = 999
        self.user.guild_permissions = SimpleNamespace(administrator=False, manage_guild=True)
        self.followups = []
        self.modal = None
        self.response.send_modal = self._send_modal
        self.followup = SimpleNamespace(send=self._followup)

    async def _send_modal(self, modal):
        self.modal = modal

    async def _followup(self, content=None, **kwargs):
        self.followups.append((content, kwargs))


def _calendar_view(service, calendar=None):
    return GuildConfigView(
        guild_id=999,
        author_id=111,
        guild_name="Test Guild",
        guild_config_service=service,
        calendar=calendar,
    )


def _calendar_line(embed):
    return next(line for line in embed.description.splitlines() if line.startswith("Event calendar:"))


def test_config_embed_describes_the_calendar_setup():
    async def run_test(**values):
        view = _calendar_view(CalendarConfigService(**values))
        embed = await view.build_embed()
        kingdom_line = next(line for line in embed.description.splitlines() if line.startswith("Kingdom:"))
        return kingdom_line, _calendar_line(embed), view.kingdom_button.label

    assert asyncio.run(run_test()) == ("Kingdom: Not set", "Event calendar: Not set", "Set kingdom")
    assert asyncio.run(run_test(calendar_channel_id=555, kingdom_id=795)) == (
        "Kingdom: **795**",
        "Event calendar: <#555> · every day at 08:00 UTC",
        "Kingdom: 795",
    )
    assert "press **Set kingdom**" in asyncio.run(run_test(calendar_channel_id=555))[1]
    assert asyncio.run(run_test(kingdom_id=795))[:2] == ("Kingdom: **795**", "Event calendar: Not set")


def test_picking_a_calendar_channel_without_a_kingdom_asks_for_the_kingdom():
    service = CalendarConfigService()
    calendar = FakeCalendar()
    interaction = ModalInteraction()

    async def run_test():
        view = _calendar_view(service, calendar)
        await view.build_embed()
        view.calendar_channel_select._values = [SimpleNamespace(id=555)]
        await view.calendar_channel_select.callback(interaction)
        assert interaction.modal is not None and service.updates == []

        interaction.modal.kingdom._value = " 795 "
        submit = ModalInteraction()
        await interaction.modal.on_submit(submit)
        return submit

    submit = asyncio.run(run_test())

    assert calendar.verified == [795]
    assert service.updates == [
        {"kingdom_id": 795, "calendar_last_posted_on": None, "calendar_channel_id": 555}
    ]
    assert calendar.posted == [999]
    description = submit.edited_original_embed.description
    assert "Kingdom: **795**" in description and "Event calendar: <#555> · every day at 08:00 UTC" in description
    assert "✅ Posted today's calendar in <#555>." in description


def test_picking_a_calendar_channel_with_a_kingdom_saves_and_posts():
    service = CalendarConfigService(kingdom_id=795, calendar_last_posted_on="yesterday")
    calendar = FakeCalendar(post_result=(False, "Could not post the calendar: I need Send Messages in <#777>."))
    interaction = ModalInteraction()

    async def run_test():
        view = _calendar_view(service, calendar)
        await view.build_embed()
        view.calendar_channel_select._values = [SimpleNamespace(id=777)]
        await view.calendar_channel_select.callback(interaction)

    asyncio.run(run_test())

    assert interaction.modal is None
    assert service.updates == [{"calendar_channel_id": 777, "calendar_last_posted_on": None}]
    assert calendar.posted == [999]
    assert "⚠️ Could not post the calendar: I need Send Messages in <#777>." in (
        interaction.edited_original_embed.description
    )


def test_clearing_the_calendar_channel_stops_the_daily_post():
    service = CalendarConfigService(calendar_channel_id=555, kingdom_id=795)
    calendar = FakeCalendar()
    interaction = ModalInteraction()

    async def run_test():
        view = _calendar_view(service, calendar)
        await view.build_embed()
        view.calendar_channel_select._values = []
        await view.calendar_channel_select.callback(interaction)

    asyncio.run(run_test())

    assert service.updates == [{"calendar_channel_id": None}]
    assert calendar.posted == []
    assert _calendar_line(interaction.edited_original_embed) == "Event calendar: Not set"
    assert "Kingdom: **795**" in interaction.edited_original_embed.description


def test_kingdom_button_opens_the_form_with_the_saved_kingdom():
    service = CalendarConfigService(kingdom_id=795)
    interaction = ModalInteraction()

    async def run_test():
        view = _calendar_view(service, FakeCalendar())
        await view.build_embed()
        await view.kingdom_button.callback(interaction)

    asyncio.run(run_test())

    assert interaction.modal.kingdom.default == "795"


def test_server_kingdom_must_be_a_kingdom_number():
    service = CalendarConfigService()
    calendar = FakeCalendar()

    async def run_test(value):
        view = _calendar_view(service, calendar)
        interaction = ModalInteraction()
        await view.apply_kingdom(interaction, value)
        return interaction

    for value in ("", "abc", "0", "-5", "100000", "7.5"):
        interaction = asyncio.run(run_test(value))
        assert interaction.response.sent_message == ("Enter the kingdom number, for example `795`.", True)
    assert service.updates == [] and calendar.verified == []


def test_server_kingdom_unknown_to_the_site_is_not_saved():
    from services.kingshot_calendar_service import KingdomNotTrackedError

    service = CalendarConfigService()
    calendar = FakeCalendar(verify_error=KingdomNotTrackedError(4242))
    interaction = ModalInteraction()

    async def run_test():
        await _calendar_view(service, calendar).apply_kingdom(interaction, "4242", 555)

    asyncio.run(run_test())

    assert service.updates == [] and calendar.posted == []
    assert "Kingdom **4242** is not on the kingshot.net calendar" in interaction.followups[0][0]
    assert interaction.followups[0][1] == {"ephemeral": True}
    assert _calendar_line(interaction.edited_original_embed) == "Event calendar: Not set"


def test_server_kingdom_is_saved_when_the_site_cannot_be_asked():
    from services.kingshot_calendar_service import CalendarUnavailableError

    service = CalendarConfigService()
    calendar = FakeCalendar(verify_error=CalendarUnavailableError("down"))
    interaction = ModalInteraction()

    async def run_test():
        await _calendar_view(service, calendar).apply_kingdom(interaction, "795")

    asyncio.run(run_test())

    assert service.updates == [{"kingdom_id": 795, "calendar_last_posted_on": None}]
    # No channel yet, so nothing is posted.
    assert calendar.posted == []
    assert "could not be checked. It is saved anyway." in interaction.edited_original_embed.description


def test_server_kingdom_form_is_only_for_the_panel_owner():
    service = CalendarConfigService()
    interaction = ModalInteraction(user_id=222)

    async def run_test():
        await _calendar_view(service, FakeCalendar()).apply_kingdom(interaction, "795")

    asyncio.run(run_test())

    assert service.updates == []
