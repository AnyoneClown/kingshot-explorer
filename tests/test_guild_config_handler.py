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

    assert embed.title == "Bot settings"
    assert "Voice replies" not in embed.description
    assert "AI replies: **Disabled**" in embed.description
    # Nothing is set up yet: the panel opens with the checklist, first step marked.
    assert "**Setup: 0 of 3 steps done**" in embed.description
    assert _step(embed, 1).startswith("➡️ **1. Kingdom** — next: press **Set kingdom**")


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
    assert interaction.edited_original_embed.title == "Bot settings"
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
    assert interaction.edited_original_embed.title == "Bot settings"


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
    assert interaction.edited_original_embed.title == "Bot settings"
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


def _step(embed, number):
    """The line of one setup step in the checklist."""
    return next(line for line in embed.description.splitlines() if f"**{number}. " in line)


def test_setup_checklist_marks_done_steps_and_the_next_one():
    async def run_test(**values):
        view = _calendar_view(CalendarConfigService(**values))
        embed = await view.build_embed()
        return [_step(embed, number) for number in (1, 2, 3)], view.kingdom_button.label, embed.description

    steps, kingdom_label, description = asyncio.run(run_test())
    assert "**Setup: 0 of 3 steps done**" in description and kingdom_label == "Set kingdom"
    assert steps == [
        "➡️ **1. Kingdom** — next: press **Set kingdom** and enter the kingdom this server plays in.",
        "⬜ **2. Gift codes** — pick the channel for new codes and their results in the "
        "**Gift-code channel** menu.",
        "⬜ **3. Daily event calendar** — pick its channel in the **Event calendar channel** menu.",
    ]

    steps, kingdom_label, description = asyncio.run(run_test(kingdom_id=795))
    assert "**Setup: 1 of 3 steps done**" in description and kingdom_label == "Kingdom: 795"
    assert steps[0] == "✅ **1. Kingdom** — **795**"
    assert steps[1].startswith("➡️ **2. Gift codes** — next: pick the channel")
    assert steps[2].startswith("⬜ **3. Daily event calendar** — ")

    # A calendar channel without a kingdom is saved but not posted yet.
    steps, _, _ = asyncio.run(run_test(calendar_channel_id=555))
    assert steps[2] == "✅ **3. Daily event calendar** — <#555> · not posted yet: press **Set kingdom**"

    steps, _, description = asyncio.run(run_test(kingdom_id=795, calendar_channel_id=555))
    assert "**Setup: 2 of 3 steps done**" in description
    assert steps[2] == "✅ **3. Daily event calendar** — <#555> · every day at 08:00 UTC"
    # The other settings stay listed under the checklist.
    assert "Bot manager role: Not set\nReminder timezone: **UTC**\nAI replies: **Disabled**" in description


def test_finished_setup_reads_as_plain_settings():
    async def run_test():
        view = _calendar_view(
            CalendarConfigService(
                kingdom_id=795, gift_code_channel_id=444, calendar_channel_id=555, time_zone="Europe/Kyiv"
            )
        )
        return (await view.build_embed()).description.splitlines(), view

    lines, view = asyncio.run(run_test())
    assert lines[:7] == [
        "Server: **Test Guild**",
        "Kingdom: **795**",
        "Gift-code announcements: <#444>",
        "Event calendar: <#555> · every day at 08:00 UTC",
        "Bot manager role: Not set",
        "Reminder timezone: **Europe/Kyiv**",
        "AI replies: **Disabled**",
    ]
    assert not any("Setup:" in line or "**1. " in line for line in lines)
    # Refresh is gone; without a gift handler there is no panel button either.
    assert [item.label for item in view.children if hasattr(item, "label")] == [
        "Kingdom: 795", "Timezone: Europe/Kyiv", "AI Replies: Off",
    ]
    assert [item.placeholder for item in view.children if hasattr(item, "placeholder")] == [
        "Gift-code channel (new codes, results and the sign-up panel)",
        "Event calendar channel (daily post at 08:00 UTC)",
        "Bot manager role (optional)",
    ]


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
    embed = submit.edited_original_embed
    assert _step(embed, 1) == "✅ **1. Kingdom** — **795**"
    assert _step(embed, 3) == "✅ **3. Daily event calendar** — <#555> · every day at 08:00 UTC"
    assert "✅ Posted today's calendar in <#555>." in embed.description


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
    assert _step(interaction.edited_original_embed, 3).startswith("⬜ **3. Daily event calendar** — pick its channel")
    assert _step(interaction.edited_original_embed, 1) == "✅ **1. Kingdom** — **795**"


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
    assert _step(interaction.edited_original_embed, 1).startswith("➡️ **1. Kingdom** — next:")
    assert _step(interaction.edited_original_embed, 3).startswith("⬜ **3. Daily event calendar** — pick its channel")


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


# ---------------------------------------------------------- sign-up panel


class FakeGiftHandler:
    def __init__(self, error=None):
        self.error = error
        self.posted = []

    async def post_panel(self, channel):
        if self.error is not None:
            raise self.error
        self.posted.append(channel.id)


def _panel_view(service, gift, channels=None):
    guild = SimpleNamespace(
        get_channel=(channels or {}).get, get_role=lambda role_id: None, me=None,
    )
    return GuildConfigView(
        guild_id=999, author_id=111, guild_name="Test Guild", guild_config_service=service, gift=gift, guild=guild,
    )


def _button_labels(view):
    return [item.label for item in view.children if hasattr(item, "label")]


def test_sign_up_panel_button_needs_a_gift_handler_and_a_gift_channel():
    async def run_test(gift, **values):
        view = _panel_view(CalendarConfigService(**values), gift)
        embed = await view.build_embed()
        return view, embed

    # No handler injected (or one that cannot post panels yet): no button at all.
    for gift in (None, SimpleNamespace()):
        view, embed = asyncio.run(run_test(gift, gift_code_channel_id=444))
        assert "Post sign-up panel" not in _button_labels(view)
        assert _step(embed, 2) == "✅ **2. Gift codes** — <#444>"

    view, embed = asyncio.run(run_test(FakeGiftHandler()))
    assert _button_labels(view) == ["Set kingdom", "Post sign-up panel", "Timezone: UTC", "AI Replies: Off"]
    assert view.post_panel_button.disabled
    assert _step(embed, 2).endswith("menu, then post the sign-up panel there.")

    view, embed = asyncio.run(run_test(FakeGiftHandler(), gift_code_channel_id=444))
    assert not view.post_panel_button.disabled
    assert _step(embed, 2) == (
        "✅ **2. Gift codes** — <#444> · press **Post sign-up panel** to put the sign-up buttons there"
    )


def test_picking_the_gift_channel_points_at_the_sign_up_panel_button():
    service = CalendarConfigService(kingdom_id=795)
    interaction = ModalInteraction()

    async def run_test():
        view = _panel_view(service, FakeGiftHandler())
        await view.build_embed()
        view.gift_channel_select._values = [SimpleNamespace(id=444)]
        await view.gift_channel_select.callback(interaction)
        return view

    view = asyncio.run(run_test())

    assert service.updates == [{"gift_code_channel_id": 444}]
    description = interaction.edited_original_embed.description
    assert "**Setup: 2 of 3 steps done**" in description
    assert (
        "✅ New gift codes are announced in <#444>. Press **Post sign-up panel** to put the sign-up buttons there."
        in description
    )
    assert not view.post_panel_button.disabled


def test_sign_up_panel_is_posted_in_the_gift_channel():
    service = CalendarConfigService(kingdom_id=795, gift_code_channel_id=444)
    gift = FakeGiftHandler()
    interaction = ModalInteraction()

    async def run_test():
        view = _panel_view(service, gift, {444: SimpleNamespace(id=444)})
        await view.build_embed()
        await view.post_panel_button.callback(interaction)

    asyncio.run(run_test())

    assert gift.posted == [444]
    embed = interaction.edited_original_embed
    assert "✅ The sign-up panel is posted in <#444>. Pin it so members can always find it." in embed.description
    # Once posted, the checklist stops asking for it.
    assert _step(embed, 2) == "✅ **2. Gift codes** — <#444>"


def test_sign_up_panel_problems_are_explained_in_the_panel():
    import discord

    forbidden = discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "Missing Permissions")

    async def run_test(gift, channels, **values):
        interaction = ModalInteraction()
        view = _panel_view(CalendarConfigService(**values), gift, channels)
        await view.build_embed()
        await view.post_panel_button.callback(interaction)
        return interaction.edited_original_embed.description

    channel = {444: SimpleNamespace(id=444)}
    description = asyncio.run(run_test(FakeGiftHandler(forbidden), channel, gift_code_channel_id=444))
    assert "⚠️ I could not post the sign-up panel in <#444>." in description
    assert "View Channel, Send Messages and Embed Links" in description

    gift = FakeGiftHandler()
    description = asyncio.run(run_test(gift, {}, gift_code_channel_id=444))
    assert "I can't find <#444>. Pick the gift-code channel again." in description

    description = asyncio.run(run_test(gift, channel))
    assert "Pick the gift-code channel first, then post the sign-up panel." in description
    assert gift.posted == []


# ---------------------------------------------------------------- timezone


def test_timezone_button_opens_the_form_with_the_saved_timezone():
    async def run_test(**values):
        interaction = ModalInteraction()
        view = _calendar_view(CalendarConfigService(**values))
        await view.build_embed()
        await view.time_zone_button.callback(interaction)
        return view.time_zone_button.label, interaction.modal.time_zone.default

    assert asyncio.run(run_test()) == ("Timezone: UTC", "UTC")
    assert asyncio.run(run_test(time_zone="Europe/Kyiv")) == ("Timezone: Europe/Kyiv", "Europe/Kyiv")


def test_server_timezone_is_saved_under_its_tz_database_name():
    async def run_test(value):
        service = CalendarConfigService()
        interaction = ModalInteraction()
        view = _calendar_view(service)
        await view.apply_time_zone(interaction, value)
        return service.updates, interaction, view

    for typed, saved in [
        ("Europe/Kyiv", "Europe/Kyiv"),
        ("  europe/kyiv ", "Europe/Kyiv"),
        ("america/new york", "America/New_York"),
        ("Kyiv", "Europe/Kyiv"),
        ("utc", "UTC"),
    ]:
        updates, interaction, view = asyncio.run(run_test(typed))
        assert updates == [{"time_zone": saved}]
        description = interaction.edited_original_embed.description
        assert f"Reminder timezone: **{saved}**" in description
        assert f"✅ Reminder times are entered in **{saved}**" in description
        assert view.time_zone_button.label == f"Timezone: {saved}"


def test_unknown_timezones_are_rejected_with_examples():
    async def run_test(value, user_id=111):
        service = CalendarConfigService()
        interaction = ModalInteraction(user_id=user_id)
        await _calendar_view(service).apply_time_zone(interaction, value)
        return service.updates, interaction.response.sent_message

    for value in ("Kyiv time", "GMT+2", "Mars/Olympus", "`"):
        updates, (message, ephemeral) = asyncio.run(run_test(value))
        assert updates == [] and ephemeral is True
        assert "is not a timezone I know" in message
        assert "`Europe/Kyiv`" in message and "`America/New_York`" in message
        assert message.count("`") % 2 == 0  # what was typed cannot break the formatting

    # The form belongs to the panel's owner.
    assert asyncio.run(run_test("Europe/Kyiv", user_id=222)) == ([], None)
