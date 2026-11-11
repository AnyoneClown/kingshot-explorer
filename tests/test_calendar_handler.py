import asyncio
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import discord.ext.commands

from handlers.calendar_handler import (
    CALENDAR_GOLD,
    TAB_NEXT,
    TAB_TODAY,
    TAB_WEEK,
    CalendarHandler,
    CalendarTabButton,
    CalendarView,
    _join_lines,
    build_calendar_embed,
    build_digest_view,
    build_today_embed,
    build_week_embed,
    calendar_error_message,
)
from handlers.permissions import AccessPolicy
from handlers.ui import EmbedColors
from services.kingshot_calendar_service import (
    CATEGORY_ALLIANCE,
    CATEGORY_DEALS,
    CATEGORY_GROWTH,
    CATEGORY_PACK,
    CATEGORY_SPECIAL,
    CATEGORY_WAR,
    CalendarEvent,
    CalendarStatus,
    CalendarUnavailableError,
    KingdomCalendar,
    KingdomNotTrackedError,
)

GUILD_ID = 999
CHANNEL_ID = 555
KINGDOM = 795


def utc(day, hour=0, minute=0, month=11):
    return datetime(2026, month, day, hour, minute, tzinfo=timezone.utc)


NOW = utc(11, 8)


def event(title, category, start, end, **kwargs):
    return CalendarEvent(key=title.lower(), title=title, category=category, start=start, end=end, **kwargs)


def make_calendar(events=None, *, kvk="default", transfer="default", includes_next_week=True):
    if events is None:
        events = [
            event("Swordland Summit League", CATEGORY_WAR, utc(30, month=10), utc(15, month=12), is_main=True),
            event("Alliance Brawl", CATEGORY_ALLIANCE, utc(9), utc(15, 12)),
            event("Hero Rally", CATEGORY_DEALS, utc(9), utc(23)),
            event("Custom Selection", CATEGORY_PACK, utc(9), utc(12)),
            event("Enhance Gear", CATEGORY_PACK, utc(9), utc(16)),
            event("Sanctuary Battle", CATEGORY_WAR, utc(10), utc(14), is_main=True),
            event("Officer Project", CATEGORY_GROWTH, utc(10), utc(12)),
            event("Champion's Way", CATEGORY_GROWTH, utc(11), utc(14)),
            event("Master's Collection", CATEGORY_PACK, utc(11), utc(13)),
            event("Mystic Divination", CATEGORY_DEALS, utc(13), utc(15), estimated=True),
            event("Golden Glaives", CATEGORY_GROWTH, utc(16), utc(18)),
            event("Eternity's Reach", CATEGORY_SPECIAL, utc(17), utc(18)),
            event("Castle Battle", CATEGORY_WAR, utc(21, 12), utc(21, 17), is_main=True),
        ]
    if kvk == "default":
        kvk = CalendarStatus(kind="kvk", phase="countdown", phase_name="Next KvK", number=21, icon="⚔️", target=utc(30))
    if transfer == "default":
        transfer = CalendarStatus(
            kind="transfer", phase="invitational", phase_name="Invitational", number=9, icon="✉️", target=utc(13)
        )
    return KingdomCalendar(
        kingdom=KINGDOM,
        fetched_at=NOW,
        current_week=1,
        events=tuple(events),
        kvk=kvk,
        transfer=transfer,
        includes_next_week=includes_next_week,
    )


def fields(embed):
    return {field.name: field.value for field in embed.fields}


def stamp(moment, style="R"):
    return f"<t:{int(moment.timestamp())}:{style}>"


class FakeCalendarService:
    def __init__(self, calendar=None, error=None):
        self.calendar = calendar or make_calendar()
        self.error = error
        self.calls = []

    async def get_calendar(self, kingdom):
        self.calls.append(kingdom)
        if self.error is not None:
            raise self.error
        return self.calendar


class FakeGuildConfig:
    def __init__(self, *rows):
        self.rows = {row.guild_id: row for row in rows}
        self.updates = []
        self.fail_updates = False

    async def get_settings(self, guild_id):
        return self.rows.get(guild_id) or SimpleNamespace(guild_id=guild_id)

    async def list_all(self):
        return list(self.rows.values())

    async def update_for_guild(self, guild_id, **changes):
        if self.fail_updates:
            raise RuntimeError("database is down")
        self.updates.append((guild_id, changes))
        row = self.rows.setdefault(guild_id, SimpleNamespace(guild_id=guild_id))
        for name, value in changes.items():
            setattr(row, name, value)
        return row


def settings(guild_id=GUILD_ID, channel_id=CHANNEL_ID, kingdom=KINGDOM, posted_on=None):
    return SimpleNamespace(
        guild_id=guild_id,
        calendar_channel_id=channel_id,
        kingdom_id=kingdom,
        calendar_last_posted_on=posted_on,
    )


def make_handler(*rows, service=None, now=NOW, guild_ids=(GUILD_ID,), channel_guild_id=GUILD_ID):
    channel = SimpleNamespace(id=CHANNEL_ID, guild=SimpleNamespace(id=channel_guild_id), send=AsyncMock())
    bot = SimpleNamespace(
        guilds=[SimpleNamespace(id=guild_id) for guild_id in guild_ids],
        get_channel=MagicMock(return_value=channel),
        fetch_channel=AsyncMock(return_value=channel),
    )
    config = FakeGuildConfig(*rows)
    handler = CalendarHandler(
        service or FakeCalendarService(),
        bot,
        guild_configuration_service=config,
        now_provider=lambda: now,
    )
    return handler, config, channel


def make_interaction(user_id=1, guild_id=GUILD_ID):
    message = SimpleNamespace(id=42, edit=AsyncMock())
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id, guild_permissions=SimpleNamespace(administrator=False, manage_guild=False)),
        guild_id=guild_id,
        guild=SimpleNamespace(id=guild_id) if guild_id else None,
        channel=SimpleNamespace(id=CHANNEL_ID),
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock(), is_done=lambda: True),
        followup=SimpleNamespace(send=AsyncMock(return_value=message)),
        edit_original_response=AsyncMock(),
        delete_original_response=AsyncMock(),
    )


# ------------------------------------------------------------------ embeds


def image_names(embeds):
    return [embed.image.url for embed in embeds]


def test_today_embed_shows_what_starts_ends_runs_and_comes_next():
    embed = build_today_embed(make_calendar(), NOW)
    sections = fields(embed)

    assert embed.title == "📅 Kingdom 795 · Wednesday, Nov 11"
    assert embed.url is None
    assert embed.description.splitlines() == [
        f"⚔️ **KvK #21** starts {stamp(utc(30))}",
        f"✉️ **Transfer #9** · Invitational — ends {stamp(utc(13))}",
    ]
    assert sections["🆕 Starts today"] == f"📈 Champion's Way · ends {stamp(utc(14))}"
    assert sections["⏳ Last day"] == f"📈 Officer Project · ends {stamp(utc(12))}"
    assert sections["▶️ Running"].splitlines() == [
        f"⚔️ **Sanctuary Battle** · ends {stamp(utc(14))}",
        f"🤝 Alliance Brawl · ends {stamp(utc(15, 12))}",
        f"💎 Hero Rally · ends {stamp(utc(23))}",
        f"⚔️ **Swordland Summit League** · ends {stamp(utc(15, month=12))}",
    ]
    assert sections["🔜 Coming up"] == "**Fri, Nov 13** · 💎 Mystic Divination *(est.)*"
    assert sections["🛍️ Packs"].splitlines() == [
        "**New today:** Master's Collection",
        "**Last day:** Custom Selection",
        "**Also on sale:** Enhance Gear",
    ]
    assert embed.footer.text == (
        "Times are in your time zone · est. = usual rotation, not confirmed yet"
    )
    assert embed.timestamp == NOW
    assert embed.color == EmbedColors.NEUTRAL


def test_daily_post_footer_names_the_post_time():
    embed = build_today_embed(make_calendar(), NOW, daily=True)

    assert embed.footer.text.startswith("Posted daily at 08:00 UTC · ")


def test_today_embed_highlights_a_main_event_that_starts_later_today():
    embed = build_today_embed(make_calendar(), utc(21, 8))

    assert embed.color == CALENDAR_GOLD
    assert fields(embed)["🆕 Starts today"] == (
        f"⚔️ **Castle Battle** · starts {stamp(utc(21, 12), 't')} ({stamp(utc(21, 12))})"
    )


def test_today_embed_applies_the_category_filter():
    embed = build_today_embed(make_calendar(), NOW, {CATEGORY_WAR})

    assert list(fields(embed)) == ["▶️ Running"]
    assert "Alliance Brawl" not in fields(embed)["▶️ Running"]
    # No estimated date is shown, so the footer does not explain the mark.
    assert "est." not in embed.footer.text


def test_today_embed_explains_an_empty_calendar():
    empty = build_today_embed(make_calendar([], kvk=None, transfer=None), NOW)
    filtered = build_today_embed(make_calendar([]), NOW, {CATEGORY_WAR})

    assert empty.description.strip() == "Nothing is scheduled for this kingdom right now."
    assert not empty.fields
    assert filtered.description.endswith("Nothing in the selected categories right now.")


def test_status_lines_follow_the_phase():
    def describe(**kwargs):
        calendar = make_calendar([], transfer=None, kvk=CalendarStatus(kind="kvk", number=21, **kwargs))
        return build_today_embed(calendar, NOW).description.splitlines()[0]

    assert describe(phase="preparation", phase_name="KvK prep", icon="🛡️", target=utc(14, 10)) == (
        f"🛡️ **KvK #21** · KvK prep — battle {stamp(utc(14, 10))}"
    )
    assert describe(phase="battle", phase_name="KvK battle", target=utc(14, 22)) == (
        f"⚔️ **KvK #21** · KvK battle — ends {stamp(utc(14, 22))}"
    )
    assert describe(phase="battle", phase_name="KvK battle") == "⚔️ **KvK #21** · KvK battle"
    assert describe(phase="countdown", phase_name="Next KvK") == "⚔️ **KvK #21** is next"


def test_week_embed_is_a_day_by_day_agenda():
    embed = build_week_embed(make_calendar(), utc(9), NOW)
    sections = fields(embed)

    assert embed.title == "🗓️ Kingdom 795 · Nov 9 – Nov 15"
    assert "**Running from earlier**\n⚔️ **Swordland Summit League** · → Dec 14" in embed.description
    assert list(sections) == ["Mon, Nov 9", "Tue, Nov 10", "Wed, Nov 11 · today", "Fri, Nov 13"]
    assert sections["Mon, Nov 9"].splitlines() == [
        "🤝 Alliance Brawl · → Sun 15, 12:00 UTC",
        "💎 Hero Rally · → Nov 22",
        "🛍️ Custom Selection, Enhance Gear",
    ]
    assert sections["Tue, Nov 10"].splitlines() == [
        "⚔️ **Sanctuary Battle** · → Fri 13",
        "📈 Officer Project · → Wed 11",
    ]
    assert sections["Wed, Nov 11 · today"].splitlines() == ["📈 Champion's Way · → Fri 13", "🛍️ Master's Collection"]
    assert sections["Fri, Nov 13"] == "💎 Mystic Divination *(est.)* · → Sat 14"
    assert embed.footer.text.startswith("Game days (UTC), events start at 00:00 unless a time is shown")


def test_week_embed_strikes_what_is_over_and_shows_short_events_with_times():
    this_week = build_week_embed(make_calendar(), utc(9), utc(12, 8))
    next_week = build_calendar_embed(make_calendar(), TAB_NEXT, NOW)
    sections = fields(next_week)

    assert "📈 ~~Officer Project~~ · → Wed 11" in fields(this_week)["Tue, Nov 10"]
    assert next_week.title == "🗓️ Kingdom 795 · Nov 16 – Nov 22"
    assert "💎 Hero Rally · → Sun 22" in next_week.description
    assert sections["Tue, Nov 17"] == "✨ Eternity's Reach · 1 day"
    assert sections["Sat, Nov 21"] == "⚔️ **Castle Battle** · 12:00–17:00 UTC"
    assert not any("today" in name for name in sections)


def test_week_embed_hides_carried_over_events_that_already_ended():
    calendar = make_calendar([event("Cesares Fury", CATEGORY_ALLIANCE, utc(7), utc(10))])

    embed = build_week_embed(calendar, utc(9), NOW)

    assert "Cesares Fury" not in (embed.description or "")
    assert "Nothing is scheduled" not in (embed.description or "")


def test_week_embed_says_why_a_week_is_empty():
    events = [event("Sanctuary Battle", CATEGORY_WAR, utc(10), utc(14))]

    unknown = build_calendar_embed(make_calendar(events), TAB_NEXT, NOW)
    failed = build_calendar_embed(make_calendar(events, includes_next_week=False), TAB_NEXT, NOW)
    current = build_calendar_embed(make_calendar([]), TAB_WEEK, NOW)

    assert unknown.description.endswith("There are no dates for this week yet.")
    assert failed.description.endswith("Next week could not be loaded. Try again in a few minutes.")
    assert current.description.endswith("Nothing is scheduled for this week.")


def test_embeds_stay_within_discord_limits():
    many = [
        event(f"Extremely Long Event Name Number {index:03d} " + "x" * 40, CATEGORY_GROWTH, utc(9), utc(20))
        for index in range(120)
    ]
    calendar = make_calendar(many)

    for embed in (build_today_embed(calendar, NOW), build_week_embed(calendar, utc(9), NOW)):
        assert len(embed) <= 6000
        assert all(len(field.value) <= 1024 for field in embed.fields)
        assert len(embed.description or "") <= 4096
    assert "more" in fields(build_today_embed(calendar, NOW))["▶️ Running"].splitlines()[-1]


def test_join_lines_never_cuts_a_line_and_counts_what_is_left():
    lines = [f"line {index}" for index in range(10)]

    assert _join_lines(lines, 1000) == "\n".join(lines)
    clipped = _join_lines(lines, 40).splitlines()
    assert clipped[-1] == f"… and {10 - (len(clipped) - 1)} more"
    assert all(line in lines for line in clipped[:-1])
    assert len("\n".join(clipped)) <= 40


def test_error_messages_tell_members_what_to_do():
    assert "is not on the kingshot.net calendar" in calendar_error_message(KingdomNotTrackedError(5), 5)
    assert "Try again in a few minutes" in calendar_error_message(CalendarUnavailableError("down"), 5)


# ------------------------------------------------------------------- panel


def test_calendar_view_switches_tabs_and_filters():
    async def check():
        handler, _, _ = make_handler()
        view = CalendarView(handler, handler.service.calendar, author_id=1)
        interaction = make_interaction()

        assert view.today_button.disabled and not view.week_button.disabled
        assert [option.default for option in view.category_select.options] == [True] * 6
        assert view.category_select.max_values == 6

        def edited():
            sent = interaction.edit_original_response.call_args.kwargs
            assert sent["view"] is view
            return sent["embeds"], sent["attachments"]

        await view.week_button.callback(interaction)
        interaction.response.defer.assert_awaited()
        embeds, files = edited()
        assert embeds[0].title == "🗓️ Kingdom 795 · Nov 9 – Nov 15"
        # Events and packs are two timelines, each in an embed of its own.
        assert image_names(embeds) == ["attachment://calendar-week-1.png", "attachment://calendar-week-2.png"]
        assert [file.filename for file in files] == ["calendar-week-1.png", "calendar-week-2.png"]
        assert not embeds[0].fields
        assert view.week_button.disabled and not view.today_button.disabled
        assert view.week_button.style is discord.ButtonStyle.primary

        view.category_select._values = [CATEGORY_WAR]
        await view.category_select.callback(interaction)
        embeds, files = edited()
        assert image_names(embeds) == ["attachment://calendar-week-1.png"]
        assert [option.default for option in view.category_select.options] == [True] + [False] * 5

        await view.next_button.callback(interaction)
        assert edited()[0][0].title.endswith("Nov 16 – Nov 22")
        await view.today_button.callback(interaction)
        embeds, files = edited()
        assert embeds[0].title.startswith("📅 Kingdom 795")
        assert [file.filename for file in files] == ["calendar-today-1.png"]

    asyncio.run(check())


def test_calendar_view_reports_an_outage_without_touching_the_panel():
    async def check():
        handler, _, _ = make_handler()
        view = CalendarView(handler, handler.service.calendar, author_id=1)
        handler.service.error = CalendarUnavailableError("down")
        interaction = make_interaction()

        await view.week_button.callback(interaction)

        interaction.edit_original_response.assert_not_awaited()
        assert "not answering" in interaction.followup.send.call_args.args[0]
        assert interaction.followup.send.call_args.kwargs["ephemeral"] is True

    asyncio.run(check())


def test_another_member_gets_a_private_copy_instead_of_a_refusal():
    async def check():
        handler, _, _ = make_handler()
        view = CalendarView(handler, handler.service.calendar, author_id=1, tab=TAB_WEEK)
        owner, stranger = make_interaction(user_id=1), make_interaction(user_id=2)

        assert await view.interaction_check(owner) is True
        assert await view.interaction_check(stranger) is False

        stranger.response.defer.assert_awaited_once_with(thinking=True, ephemeral=True)
        sent = stranger.followup.send.call_args.kwargs
        assert sent["ephemeral"] is True
        assert sent["view"].author_id == 2 and sent["view"].tab == TAB_WEEK
        assert sent["embeds"][0].title.startswith("🗓️ Kingdom 795")
        assert len(sent["files"]) == len(sent["embeds"]) == 2

    asyncio.run(check())


def test_panel_has_no_link_to_the_source_and_expires_like_other_panels():
    async def check():
        handler, _, _ = make_handler()
        view = CalendarView(handler, handler.service.calendar, author_id=1)
        view.message = SimpleNamespace(edit=AsyncMock())

        assert not any(getattr(item, "url", None) for item in view.children)
        await view.on_timeout()

        assert all(item.disabled for item in view.children)
        view.message.edit.assert_awaited_once()

    asyncio.run(check())


def test_daily_post_buttons_survive_restarts():
    async def check():
        view = build_digest_view(KINGDOM)
        buttons = view.to_components()[0]["components"]

        assert view.is_persistent()
        assert [button.get("custom_id") for button in buttons] == ["calendar:week:795", "calendar:next:795"]
        assert not any("url" in button for button in buttons)

        match = CalendarTabButton.__discord_ui_compiled_template__.fullmatch("calendar:next:795")
        restored = await CalendarTabButton.from_custom_id(None, None, match)
        assert (restored.tab, restored.kingdom) == (TAB_NEXT, 795)
        assert CalendarTabButton.__discord_ui_compiled_template__.fullmatch("calendar:week:abc") is None

        handler, _, _ = make_handler()
        bot = handler._bot
        bot.add_dynamic_items = MagicMock()
        handler.register_persistent_views()
        handler.register_persistent_views()
        bot.add_dynamic_items.assert_called_once_with(CalendarTabButton)

        interaction = make_interaction(user_id=7)
        await restored.callback(interaction)
        assert interaction.followup.send.call_args.kwargs["view"].tab == TAB_NEXT

    try:
        asyncio.run(check())
    finally:
        CalendarTabButton.handler = None


def test_banned_members_cannot_open_the_calendar_from_a_button():
    async def check():
        handler, _, _ = make_handler()
        handler._access = AccessPolicy(banned_user_ids={7})
        interaction = make_interaction(user_id=7)
        interaction.response.is_done = lambda: False

        await handler.send_private_panel(interaction, KINGDOM, TAB_TODAY)

        interaction.response.defer.assert_not_awaited()
        assert interaction.response.send_message.call_args.kwargs["embed"].title == "🔒 Not allowed"
        assert handler.service.calls == []

    asyncio.run(check())


# ----------------------------------------------------------------- command


def test_calendar_command_posts_the_servers_kingdom_publicly():
    async def check():
        handler, _, _ = make_handler(settings())
        interaction = make_interaction()

        await handler._handle_calendar(interaction)

        interaction.response.defer.assert_awaited_once_with(thinking=True, ephemeral=True)
        assert handler.service.calls == [KINGDOM]
        sent = interaction.followup.send.call_args.kwargs
        first, second = sent["embeds"]
        assert first.title == "📅 Kingdom 795 · Wednesday, Nov 11"
        assert first.description.splitlines()[0] == f"⚔️ **KvK #21** starts {stamp(utc(30))}"
        assert not first.fields and not first.footer.text
        assert image_names(sent["embeds"]) == ["attachment://calendar-today-1.png", "attachment://calendar-today-2.png"]
        assert [file.filename for file in sent["files"]] == ["calendar-today-1.png", "calendar-today-2.png"]
        assert second.footer.text == (
            "Game time (UTC) · est. = usual rotation, not confirmed yet"
        )
        assert second.timestamp == NOW and second.color == first.color
        assert isinstance(sent["view"], CalendarView) and sent["view"].message is not None
        assert "ephemeral" not in sent
        interaction.delete_original_response.assert_awaited_once()

    asyncio.run(check())


def test_calendar_command_prefers_the_kingdom_that_was_typed():
    async def check():
        handler, _, _ = make_handler(settings())
        interaction = make_interaction(guild_id=None)

        await handler._handle_calendar(interaction, 830)

        assert handler.service.calls == [830]

    asyncio.run(check())


def test_calendar_command_asks_for_a_kingdom_when_none_is_known():
    async def check():
        handler, _, _ = make_handler()
        in_server, in_dm = make_interaction(), make_interaction(guild_id=None)

        await handler._handle_calendar(in_server)
        await handler._handle_calendar(in_dm)

        assert handler.service.calls == []
        server_embed = in_server.edit_original_response.call_args.kwargs["embed"]
        assert server_embed.title == "Which kingdom?"
        assert "/calendar kingdom:795" in server_embed.description and "/configure" in server_embed.description
        assert "/configure" not in in_dm.edit_original_response.call_args.kwargs["embed"].description
        in_server.followup.send.assert_not_awaited()

    asyncio.run(check())


def test_calendar_command_explains_errors_privately():
    async def check():
        handler, _, _ = make_handler(service=FakeCalendarService(error=KingdomNotTrackedError(4242)))
        interaction = make_interaction()

        await handler._handle_calendar(interaction, 4242)

        assert "Kingdom **4242** is not on the kingshot.net calendar" in (
            interaction.edit_original_response.call_args.kwargs["content"]
        )
        interaction.followup.send.assert_not_awaited()

    asyncio.run(check())


def test_accepting_the_channel_offer_saves_the_kingdom_and_posts():
    async def check():
        handler, config, channel = make_handler(settings(kingdom=None))

        await handler._after_channel_setup(GUILD_ID, 830)

        assert (GUILD_ID, {"kingdom_id": 830}) in config.updates
        channel.send.assert_awaited_once()
        assert handler.service.calls == [830]

    asyncio.run(check())


def test_accepting_the_channel_offer_keeps_a_kingdom_that_is_already_set():
    async def check():
        handler, config, _ = make_handler(settings())

        await handler._after_channel_setup(GUILD_ID, 830)

        assert all("kingdom_id" not in changes for _, changes in config.updates)
        assert handler.service.calls == [KINGDOM]

    asyncio.run(check())


# -------------------------------------------------------------- daily post


def test_daily_post_goes_out_once_per_day_at_the_post_time():
    async def check():
        handler, config, channel = make_handler(settings())

        assert await handler.post_due_digests(utc(11, 7, 59)) == 0
        channel.send.assert_not_awaited()

        assert await handler.post_due_digests(utc(11, 8)) == 1
        sent = channel.send.call_args.kwargs
        assert sent["embeds"][0].title == "📅 Kingdom 795 · Wednesday, Nov 11"
        assert sent["embeds"][-1].footer.text.startswith("Posted daily at 08:00 UTC · Game time (UTC)")
        assert [file.filename for file in sent["files"]] == ["calendar-today-1.png", "calendar-today-2.png"]
        assert all(file.fp.read(8) == b"\x89PNG\r\n\x1a\n" for file in sent["files"])
        assert sent["view"].is_persistent()
        assert sent["allowed_mentions"].to_dict() == discord.AllowedMentions.none().to_dict()
        assert config.updates == [(GUILD_ID, {"calendar_last_posted_on": date(2026, 11, 11)})]

        assert await handler.post_due_digests(utc(11, 8, 10)) == 0
        assert await handler.post_due_digests(utc(11, 23, 50)) == 0
        assert await handler.post_due_digests(utc(12, 7, 50)) == 0
        assert channel.send.await_count == 1

        assert await handler.post_due_digests(utc(12, 8)) == 1
        assert channel.send.await_count == 2

    asyncio.run(check())


def test_daily_post_catches_up_after_a_restart_but_never_repeats_a_day():
    async def check():
        late, _, late_channel = make_handler(settings())
        done, _, done_channel = make_handler(settings(posted_on=date(2026, 11, 11)))

        assert await late.post_due_digests(utc(11, 15, 30)) == 1
        assert await done.post_due_digests(utc(11, 15, 30)) == 0
        late_channel.send.assert_awaited_once()
        done_channel.send.assert_not_awaited()

    asyncio.run(check())


def test_daily_post_skips_servers_that_are_not_set_up_or_not_connected():
    async def check():
        handler, _, channel = make_handler(
            settings(guild_id=1, channel_id=None),
            settings(guild_id=2, kingdom=None),
            settings(guild_id=3),
            guild_ids=(1, 2),
        )

        assert await handler.post_due_digests(utc(11, 9)) == 0
        channel.send.assert_not_awaited()
        assert handler.service.calls == []

    asyncio.run(check())


def test_daily_post_retries_after_an_outage():
    async def check():
        service = FakeCalendarService(error=CalendarUnavailableError("down"))
        handler, config, channel = make_handler(settings(), service=service)

        assert await handler.post_due_digests(utc(11, 8)) == 0
        assert config.updates == []
        assert "kingshot.net is not answering" in handler._post_failures[GUILD_ID]

        service.error = None
        assert await handler.post_due_digests(utc(11, 8, 10)) == 1
        channel.send.assert_awaited_once()
        assert GUILD_ID not in handler._post_failures

    asyncio.run(check())


def test_daily_post_is_not_repeated_when_the_day_cannot_be_saved():
    async def check():
        handler, config, channel = make_handler(settings())
        config.fail_updates = True

        assert await handler.post_due_digests(utc(11, 8)) == 1
        assert await handler.post_due_digests(utc(11, 8, 10)) == 0
        assert channel.send.await_count == 1

    asyncio.run(check())


def test_daily_post_reports_channel_problems():
    async def check():
        response = SimpleNamespace(status=403, reason="Forbidden")
        handler, _, channel = make_handler(settings())
        channel.send.side_effect = discord.Forbidden(response, "Missing Permissions")
        assert await handler.post_now(GUILD_ID) == (
            False,
            f"Could not post the calendar: I need View Channel, Send Messages and Embed Links in <#{CHANNEL_ID}>.",
        )

        missing, _, _ = make_handler(settings())
        missing._bot.get_channel.return_value = None
        missing._bot.fetch_channel.side_effect = discord.NotFound(SimpleNamespace(status=404, reason="x"), "gone")
        assert (await missing.post_now(GUILD_ID))[1].endswith("the calendar channel no longer exists.")

        foreign, _, foreign_channel = make_handler(settings(), channel_guild_id=1234)
        assert (await foreign.post_now(GUILD_ID))[1].endswith("the calendar channel belongs to another server.")
        foreign_channel.send.assert_not_awaited()

        unknown, _, _ = make_handler(settings(), service=FakeCalendarService(error=KingdomNotTrackedError(KINGDOM)))
        assert "kingdom 795 is not on the kingshot.net calendar" in (await unknown.post_now(GUILD_ID))[1]

    asyncio.run(check())


def test_post_now_posts_immediately_and_counts_as_todays_post():
    async def check():
        handler, config, channel = make_handler(settings(), now=utc(11, 6))

        assert await handler.post_now(GUILD_ID) == (True, f"Posted today's calendar in <#{CHANNEL_ID}>.")
        assert config.updates == [(GUILD_ID, {"calendar_last_posted_on": date(2026, 11, 11)})]
        # The scheduled post two hours later does not repeat it.
        assert await handler.post_due_digests(utc(11, 8)) == 0
        assert channel.send.await_count == 1

        incomplete, _, incomplete_channel = make_handler(settings(kingdom=None))
        posted, message = await incomplete.post_now(GUILD_ID)
        assert posted is False and "Pick both a calendar channel and a kingdom" in message
        incomplete_channel.send.assert_not_awaited()

    asyncio.run(check())


def test_next_post_time_is_the_next_eight_oclock_utc():
    handler, _, _ = make_handler()

    assert handler.next_post_time(utc(11, 7, 59)) == utc(11, 8)
    assert handler.next_post_time(utc(11, 8)) == utc(12, 8)


def test_daily_task_reports_whether_it_runs():
    handler, _, _ = make_handler()

    assert handler.is_daily_post_running() is False
    handler.stop_daily_task()


def test_channel_offer_goes_to_a_manager_once_per_server(monkeypatch):
    offers = []

    async def fake_offer(interaction, **kwargs):
        offers.append((interaction.user.id, kwargs["setting"]))
        return True

    monkeypatch.setattr("handlers.calendar_handler.offer_channel_setup", fake_offer)

    async def check():
        handler, _, _ = make_handler(settings(channel_id=None))
        member, manager = make_interaction(user_id=1), make_interaction(user_id=2)
        manager.user.guild_permissions = SimpleNamespace(administrator=False, manage_guild=True)

        await handler._handle_calendar(member)
        assert offers == []
        await handler._handle_calendar(manager)
        await handler._handle_calendar(manager)
        assert offers == [(2, "calendar_channel_id")]

    asyncio.run(check())


def test_calendar_command_registers_with_an_optional_kingdom():
    async def check():
        bot = discord.ext.commands.Bot(command_prefix="!", intents=discord.Intents.none())
        handler = CalendarHandler(FakeCalendarService(), bot)
        handler.register_commands()

        command = bot.tree.get_command("calendar")
        (kingdom,) = command.parameters
        assert (kingdom.name, kingdom.required, kingdom.min_value, kingdom.max_value) == ("kingdom", False, 1, 99999)
        assert kingdom.type is discord.AppCommandOptionType.integer
        assert len(command.description) <= 100

        handler.register_persistent_views()
        assert CalendarTabButton.handler is handler

    try:
        asyncio.run(check())
    finally:
        CalendarTabButton.handler = None


# ---------------------------------------------------------------- pictures


def test_pictures_fall_back_to_text_when_they_cannot_be_drawn(monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("no font")

    monkeypatch.setattr("handlers.calendar_handler.render_calendar_cards", broken)

    async def check():
        handler, _, _ = make_handler()
        embeds, files = await handler.build_message(handler.service.calendar)

        assert files == [] and len(embeds) == 1
        assert "🆕 Starts today" in fields(embeds[0])
        assert embeds[0].footer.text.startswith("Times are in your time zone")
        assert embeds[0].image.url is None

    asyncio.run(check())


def test_daily_post_is_text_where_the_bot_cannot_attach_files():
    async def check():
        handler, _, channel = make_handler(settings())
        channel.guild.me = SimpleNamespace(id=1)
        channel.permissions_for = lambda member: SimpleNamespace(attach_files=False)

        assert await handler.post_due_digests(utc(11, 8)) == 1

        sent = channel.send.call_args.kwargs
        assert sent["files"] == [] and len(sent["embeds"]) == 1
        assert "▶️ Running" in fields(sent["embeds"][0])
        assert sent["embeds"][0].footer.text.startswith("Posted daily at 08:00 UTC · est.")

    asyncio.run(check())


def test_an_empty_day_is_a_single_text_embed():
    async def check():
        handler, _, _ = make_handler(service=FakeCalendarService(make_calendar([], kvk=None, transfer=None)))
        embeds, files = await handler.build_message(handler.service.calendar)

        assert files == [] and len(embeds) == 1
        assert embeds[0].description.strip() == "Nothing is scheduled for this kingdom right now."

    asyncio.run(check())


def test_cards_are_drawn_once_per_calendar_view_and_minute(monkeypatch):
    calls = []

    def fake_render(calendar, tab, now, categories, extra_icons):
        calls.append((tab, categories, extra_icons))
        return [b"png"]

    monkeypatch.setattr("handlers.calendar_handler.render_calendar_cards", fake_render)

    async def check():
        handler, _, _ = make_handler()
        calendar = handler.service.calendar

        assert await handler.render_cards(calendar, TAB_TODAY, utc(11, 8, 0)) == [b"png"]
        await handler.render_cards(calendar, TAB_TODAY, utc(11, 8, 0))
        assert len(calls) == 1
        await handler.render_cards(calendar, TAB_TODAY, utc(11, 8, 1))
        await handler.render_cards(calendar, TAB_WEEK, utc(11, 8, 1))
        await handler.render_cards(calendar, TAB_TODAY, utc(11, 8, 1), {CATEGORY_WAR})
        assert len(calls) == 4

    asyncio.run(check())


def test_artwork_for_events_that_are_not_bundled_is_downloaded(monkeypatch):
    seen = {}

    def fake_render(calendar, tab, now, categories, extra_icons):
        seen["extra_icons"] = extra_icons
        return [b"png"]

    monkeypatch.setattr("handlers.calendar_handler.render_calendar_cards", fake_render)

    async def check():
        # Keys are the API ids, which are also the artwork file names.
        calendar = KingdomCalendar(
            kingdom=KINGDOM,
            fetched_at=NOW,
            current_week=1,
            events=(
                CalendarEvent("sanctuary-battle", "Sanctuary Battle", CATEGORY_WAR, utc(10), utc(14)),
                CalendarEvent("brand-new-festival", "Brand New Festival", CATEGORY_SPECIAL, utc(10), utc(14)),
            ),
        )
        service = FakeCalendarService(calendar)
        service.get_event_icons = AsyncMock(return_value={"brand-new-festival": b"icon"})
        handler, _, _ = make_handler(service=service)

        await handler.render_cards(calendar, TAB_TODAY, NOW)

        service.get_event_icons.assert_awaited_once_with(["brand-new-festival"])
        assert seen["extra_icons"] == {"brand-new-festival": b"icon"}

    asyncio.run(check())
