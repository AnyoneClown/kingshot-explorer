from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from handlers.event_handler import EventHandler, EventListView, parse_event_time, when_label
from handlers.permissions import AccessPolicy
from services.event_scheduler_service import EventSchedulerService


def interaction(user_id=7, channel_id=123, custom_id=""):
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id),
        channel_id=channel_id,
        data={"custom_id": custom_id},
        response=SimpleNamespace(
            is_done=Mock(return_value=False),
            send_message=AsyncMock(),
            send_modal=AsyncMock(),
            defer=AsyncMock(),
            edit_message=AsyncMock(),
        ),
        followup=SimpleNamespace(send=AsyncMock()),
        original_response=AsyncMock(return_value=SimpleNamespace(edit=AsyncMock())),
        edit_original_response=AsyncMock(return_value=SimpleNamespace(edit=AsyncMock())),
    )


def guild_interaction(user_id=8, *, manage_guild=False, channel_id=123):
    item = interaction(user_id, channel_id)
    item.guild_id = 10
    item.user.guild_permissions = SimpleNamespace(manage_guild=manage_guild, administrator=False)
    item.user.roles = []
    item.followup.send = AsyncMock(return_value=SimpleNamespace(edit=AsyncMock()))
    item.delete_original_response = AsyncMock()
    return item


def zone_settings(time_zone):
    """A guild configuration service whose servers all use ``time_zone``."""
    return SimpleNamespace(get_settings=AsyncMock(return_value=SimpleNamespace(time_zone=time_zone)))


def labels(view):
    return [getattr(item, "label", None) or item.placeholder for item in view.children]


def test_timezone_conversion_rejects_dst_gaps_overlaps_and_bad_zones():
    assert parse_event_time("2099-01-01", "18:30", "Europe/Kyiv") == datetime(2099, 1, 1, 16, 30, tzinfo=timezone.utc)
    for date, time, zone, reason in [
        ("2026-03-08", "02:30", "America/New_York", "does not exist"),
        ("2026-11-01", "01:30", "America/New_York", "occurs twice"),
        ("2099-01-01", "12:00", "No/Such_Zone", "timezone"),
        ("not a date", "12:00", "UTC", "YYYY-MM-DD"),
    ]:
        with pytest.raises(ValueError, match=reason):
            parse_event_time(date, time, zone)


def test_the_date_field_names_the_timezone_within_discords_label_limit():
    assert when_label("UTC") == "Date and time (UTC)"
    assert when_label("Europe/Kyiv") == "Date and time (Europe/Kyiv)"
    longest = when_label("America/Argentina/ComodRivadavia")
    assert longest == "When (America/Argentina/ComodRivadavia)" and len(longest) <= 45


@pytest.mark.asyncio
async def test_creating_requires_preview_confirmation_and_rechecks_authorization():
    service = EventSchedulerService()
    handler = EventHandler(service, Mock(), {7})
    denied = interaction(8)
    await handler.open_reminder_form(denied)
    denied.response.send_modal.assert_not_awaited()
    opened = interaction()
    await handler.open_reminder_form(opened)
    modal = opened.response.send_modal.call_args.args[0]
    # When it is posted, the message, the repeat: no timezone and no "minutes before".
    assert modal.title == "Create reminder"
    assert [item.label for item in modal.children] == [
        "Date and time (UTC)", "Reminder message", "Repeat every N days (blank = once)",
    ]
    modal.when._value = "2099-01-01 18:30"
    modal.message_input._value = "Alliance prep"
    modal.repeat._value = "2"
    await modal.on_submit(denied)
    assert await service.get_events_for_channel(123) == []
    submitted = interaction()
    await modal.on_submit(submitted)
    sent = submitted.response.send_message.call_args.kwargs
    preview = sent["view"]
    assert sent["ephemeral"] and sent["embed"].title == "Preview reminder"
    assert await service.get_events_for_channel(123) == []
    # The reminder is posted at the entered time, not some minutes before it.
    assert preview.event.event_time == datetime(2099, 1, 1, 18, 30, tzinfo=timezone.utc)
    handler._access = AccessPolicy(set())
    await preview.save.callback(interaction())
    assert await service.get_events_for_channel(123) == []
    handler._access = AccessPolicy({7})
    await preview.save.callback(interaction(8))
    assert await service.get_events_for_channel(123) == []
    saved = interaction()
    await preview.save.callback(saved)
    await preview.save.callback(interaction())
    events = await service.get_events_for_channel(123)
    assert len(events) == 1 and events[0].repeat_every_days == 2
    assert events[0].created_by_user_id == 7 and events[0].role_names == ["everyone"]
    assert saved.edit_original_response.call_args.kwargs["embed"].title == "Reminder saved"


@pytest.mark.asyncio
async def test_bad_form_input_is_explained_and_nothing_is_previewed():
    handler = EventHandler(EventSchedulerService(), Mock(), {7})
    for when, repeat, message, reason in [
        ("tomorrow", "", "Bear Trap", "YYYY-MM-DD HH:MM"),
        ("2099-01-01 18:30", "weekly", "Bear Trap", "whole number of days"),
        ("2099-01-01 18:30", "400", "Bear Trap", "between 1 and 365 days"),
        ("2001-01-01 18:30", "", "Bear Trap", "must be in the future"),
        ("2099-01-01 18:30", "", "   ", "between 1 and 1900 characters"),
    ]:
        opened = interaction()
        await handler.open_reminder_form(opened)
        modal = opened.response.send_modal.call_args.args[0]
        modal.when._value, modal.repeat._value, modal.message_input._value = when, repeat, message
        submitted = interaction()
        await modal.on_submit(submitted)
        sent = submitted.response.send_message.call_args.kwargs
        assert sent["ephemeral"] and "view" not in sent
        assert sent["embed"].title == "Reminder not saved" and reason in sent["embed"].description


@pytest.mark.asyncio
async def test_reminder_times_are_entered_in_the_servers_timezone():
    service = EventSchedulerService()
    handler = EventHandler(service, Mock(), {7}, guild_configuration_service=zone_settings("Europe/Kyiv"))
    opened = guild_interaction(7)
    await handler.open_reminder_form(opened)
    modal = opened.response.send_modal.call_args.args[0]
    assert modal.when.label == "Date and time (Europe/Kyiv)" and len(modal.children) == 3
    modal.when._value = "2099-01-01 18:30"
    modal.message_input._value = "Bear Trap"
    modal.repeat._value = ""
    submitted = guild_interaction(7)
    await modal.on_submit(submitted)
    sent = submitted.response.send_message.call_args.kwargs
    assert sent["view"].event.event_time == datetime(2099, 1, 1, 16, 30, tzinfo=timezone.utc)
    fields = {field.name: field.value for field in sent["embed"].fields}
    assert fields["Server time"] == "2099-01-01 18:30 EET · Europe/Kyiv"
    assert "Europe/Kyiv" in sent["embed"].footer.text and "/settings" in sent["embed"].footer.text
    await sent["view"].save.callback(guild_interaction(7))

    # Editing shows the stored time in the same timezone.
    (event,) = await service.get_events_for_channel(123)
    view = EventListView(handler, 7, 123, [event], True)
    clicked = guild_interaction(7)
    clicked.data = {"custom_id": f"event:edit:{event.id}"}
    await view.edit.callback(clicked)
    edit_form = clicked.response.send_modal.call_args.args[0]
    assert edit_form.title == "Edit reminder" and edit_form.when.default == "2099-01-01 18:30"
    assert edit_form.when.label == "Date and time (Europe/Kyiv)"


@pytest.mark.asyncio
async def test_timezone_falls_back_to_utc_without_a_server_setting():
    for service in (
        None,
        zone_settings(None),
        zone_settings("Not/AZone"),
        SimpleNamespace(get_settings=AsyncMock(side_effect=RuntimeError("db down"))),
    ):
        handler = EventHandler(EventSchedulerService(), Mock(), {7}, guild_configuration_service=service)
        assert await handler._time_zone(guild_interaction(7)) == "UTC"
    # Outside a server there is no setting to read.
    handler = EventHandler(EventSchedulerService(), Mock(), {7}, guild_configuration_service=zone_settings("Asia/Tokyo"))
    assert await handler._time_zone(interaction()) == "UTC"
    assert await handler._time_zone(guild_interaction(7)) == "Asia/Tokyo"


@pytest.mark.asyncio
async def test_an_untouched_time_inside_a_clock_change_hour_can_still_be_saved():
    service = EventSchedulerService()
    handler = EventHandler(service, Mock(), {7}, guild_configuration_service=zone_settings("America/New_York"))
    # 01:30 local time happens twice on 2099-11-01; this is the second one.
    ambiguous = datetime(2099, 11, 1, 6, 30, tzinfo=timezone.utc)
    await service.schedule_event(123, ambiguous, ["everyone"], "Bear Trap")
    (event,) = await service.get_events_for_channel(123)
    view = EventListView(handler, 7, 123, [event], True)
    clicked = guild_interaction(7)
    clicked.data = {"custom_id": f"event:edit:{event.id}"}
    await view.edit.callback(clicked)
    form = clicked.response.send_modal.call_args.args[0]
    form.when._value = form.when.default
    form.message_input._value = "Bear Trap, bring heroes"
    form.repeat._value = ""
    submitted = guild_interaction(7)
    await form.on_submit(submitted)
    assert submitted.response.send_message.call_args.kwargs["view"].event.event_time == ambiguous

    # Typing that time anew cannot say which of the two is meant.
    form.when._value = "2099-11-01 01:31"
    retyped = guild_interaction(7)
    await form.on_submit(retyped)
    assert "occurs twice" in retyped.response.send_message.call_args.kwargs["embed"].description


@pytest.mark.asyncio
async def test_long_lists_stay_reachable_and_edit_and_cancel_use_stable_ids():
    service = EventSchedulerService()
    handler = EventHandler(service, Mock(), {7})
    time = datetime(2099, 1, 1, 12, 0, tzinfo=timezone.utc)
    for index in range(26):
        await service.schedule_event(123, time + timedelta(hours=index), ["everyone"], "x" * 1900)
    events = await service.get_events_for_channel(123)
    view = EventListView(handler, 7, 123, events, True)
    # No Previous/Next: the menu and the three actions are all there is.
    assert labels(view) == ["Choose a reminder", "Edit reminder", "Cancel reminder", "Create reminder"]
    # More than a menu holds: 23 reminders and an entry that opens the rest.
    options = view.select_event.options
    assert len(options) == 24 and options[-1].label == "Later reminders (24–26)"
    assert all(len(option.label) <= 100 for option in options)
    assert all(len(field.value) <= 1024 for field in view.embed().fields)
    assert len(view.embed()) < 6000 and "The menu lists 23 at a time" in view.embed().footer.text
    view.select_event._values = [options[-1].value]
    await view.select_event.callback(interaction())
    assert view.index == 23
    assert [option.label for option in view.select_event.options][0] == "Earlier reminders (1–23)"
    assert len(view.select_event.options) == 4
    view.select_event._values = [str(events[25].id)]
    await view.select_event.callback(interaction())
    assert view.index == 25 and view.embed().title == f"Reminder #{events[25].id}"
    assert not await view.interaction_check(interaction(8))

    view.index = 0
    view._refresh_controls()
    opened = interaction(custom_id=f"event:edit:{events[0].id}")
    await view.edit.callback(opened)
    modal = opened.response.send_modal.call_args.args[0]
    assert modal.when.label == "Date and time (UTC)" and modal.when.default == "2099-01-01 12:00"
    assert len(modal.children) == 3
    modal.when._value = "2099-01-03 12:00"
    modal.message_input._value = "Edited"
    modal.repeat._value = ""
    submitted = interaction()
    await modal.on_submit(submitted)
    preview = submitted.response.send_message.call_args.kwargs["view"]
    assert submitted.response.send_message.call_args.kwargs["embed"].title == "Preview reminder changes"
    assert preview.event.id == events[0].id
    assert preview.event.event_time == datetime(2099, 1, 3, 12, 0, tzinfo=timezone.utc)
    await service.cancel_event(123, events[0].id)
    await preview.save.callback(interaction())
    assert len(await service.get_events_for_channel(123)) == 25
    stale = interaction(custom_id=f"event:cancel:{events[0].id}")
    await view.cancel.callback(stale)
    stale_prompt = stale.response.send_message.call_args.kwargs["view"]
    await stale_prompt.confirm_button.callback(interaction())
    # The stale first card must not cancel its new neighbor.
    assert len(await service.get_events_for_channel(123)) == 25
    view.index = 1
    cancelled_id = events[1].id
    opened = interaction(custom_id=f"event:cancel:{cancelled_id}")
    await view.cancel.callback(opened)
    assert len(await service.get_events_for_channel(123)) == 25  # Nothing happens before confirming.
    prompt_kwargs = opened.response.send_message.call_args.kwargs
    assert prompt_kwargs["ephemeral"]
    assert prompt_kwargs["embed"].title.startswith(f"Cancel #{cancelled_id} '")
    prompt = prompt_kwargs["view"]
    assert [item.label for item in prompt.children] == ["Cancel reminder", "Keep"]
    assert not await prompt.interaction_check(interaction(8))
    confirmed = interaction()
    await prompt.confirm_button.callback(confirmed)
    assert confirmed.edit_original_response.call_args.kwargs["embed"].title == "Reminder cancelled"
    await prompt.confirm_button.callback(interaction())  # A used prompt cannot cancel twice.
    assert len(await service.get_events_for_channel(123)) == 24
    assert cancelled_id not in [event.id for event in await service.get_events_for_channel(123)]
    assert cancelled_id not in [event.id for event in view.events]
    # 24 fit one menu again, without the "earlier/later" entries.
    assert len(view.select_event.options) == 24
    assert not any(option.value.startswith("page:") for option in view.select_event.options)

    kept = interaction(custom_id=f"event:cancel:{view.events[0].id}")
    await view.cancel.callback(kept)
    keep_click = interaction()
    await kept.response.send_message.call_args.kwargs["view"].keep_button.callback(keep_click)
    assert keep_click.response.edit_message.call_args.kwargs["embed"].title == "Reminder kept"
    assert len(await service.get_events_for_channel(123)) == 24


@pytest.mark.asyncio
async def test_managers_are_resolved_by_access_policy_and_there_is_one_command():
    import discord
    from discord.ext import commands

    service = EventSchedulerService()
    handler = EventHandler(service, Mock(), {7})
    member = guild_interaction(manage_guild=False)
    await handler.open_reminder_form(member)
    member.response.send_modal.assert_not_awaited()
    assert "Manage Server" in member.response.send_message.call_args.kwargs["embed"].description
    manager = guild_interaction(manage_guild=True)
    await handler.open_reminder_form(manager)
    manager.response.send_modal.assert_awaited_once()

    bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
    EventHandler(service, bot, {7}).register_commands()
    # Creating, editing and cancelling are buttons of /reminders.
    assert bot.tree.get_command("remind") is None and bot.tree.get_command("cancel") is None
    command = bot.tree.get_command("reminders")
    assert command.default_permissions is None and command.parameters == []
    assert len(command.description) <= 100
    await bot.close()


@pytest.mark.asyncio
async def test_saved_reminders_record_guild_and_creator():
    service = SimpleNamespace(schedule_event=AsyncMock(return_value=True))
    handler = EventHandler(service, Mock(), {7})
    submitted = guild_interaction(manage_guild=True)
    await handler._show_preview(submitted, "2099-02-01", "18:30", "Bear Trap", None)
    preview = submitted.response.send_message.call_args.kwargs["view"]
    await preview.save.callback(guild_interaction(manage_guild=True))
    assert service.schedule_event.call_args.args == (
        123, datetime(2099, 2, 1, 18, 30, tzinfo=timezone.utc), ["everyone"], "Bear Trap", None,
    )
    assert service.schedule_event.call_args.kwargs == {"guild_id": 10, "created_by_user_id": 8}

    service.schedule_event.side_effect = RuntimeError("database unavailable")
    again = guild_interaction(manage_guild=True)
    await handler._show_preview(again, "2099-02-01", "18:30", "Bear Trap", None)
    preview = again.response.send_message.call_args.kwargs["view"]
    failed = guild_interaction(manage_guild=True)
    failed.response.is_done = Mock(return_value=True)
    await preview.save.callback(failed)
    assert failed.followup.send.call_args.kwargs["ephemeral"]
    assert not preview.saving  # The draft can be saved again.


@pytest.mark.asyncio
async def test_reminders_list_handles_database_errors_and_posts_public_panel():
    service = SimpleNamespace(get_events_for_channel=AsyncMock(side_effect=RuntimeError("db down")))
    handler = EventHandler(service, Mock(), {7})
    failed = guild_interaction()
    failed.response.is_done = Mock(return_value=True)
    await handler._handle_list_events(failed)
    assert failed.response.defer.call_args.kwargs["ephemeral"]
    sent = failed.followup.send.call_args.kwargs
    assert sent["ephemeral"] and sent["embed"].title == "Reminders unavailable"

    time = datetime(2099, 1, 1, 12, 0, tzinfo=timezone.utc)
    service.get_events_for_channel = AsyncMock(return_value=[
        SimpleNamespace(
            id=12, event_time=time, role_names=["Officers"], message="Bear Trap", repeat_every_days=None,
            last_sent_at=time - timedelta(days=1), last_error="Missing permission to send messages in this channel.",
        ),
    ])
    member = guild_interaction()
    await handler._handle_list_events(member)
    sent = member.followup.send.call_args.kwargs
    assert sent["ephemeral"] is False and sent["view"].timeout == 900
    # Members can browse but get no buttons to manage reminders.
    assert labels(sent["view"]) == ["Choose a reminder"]
    assert sent["embed"].title == "Reminder #12"
    fields = {field.name: field.value for field in sent["embed"].fields}
    assert fields["Pings"] == "@Officers" and "Missing permission" in fields["Last problem"]
    # The dashboard card of the channel's reminders is attached to the list.
    assert sent["embed"].image.url == "attachment://reminders.png"
    assert [file.filename for file in sent["files"]] == ["reminders.png"]
    assert sent["files"][0].fp.read(8) == b"\x89PNG\r\n\x1a\n"
    (row,) = sent["view"].card_rows()
    assert (row.reminder_id, row.title, row.targets, row.has_problem) == (12, "Bear Trap", "@Officers", True)
    member.delete_original_response.assert_awaited_once()

    manager = guild_interaction(manage_guild=True)
    await handler._handle_list_events(manager)
    assert labels(manager.followup.send.call_args.kwargs["view"]) == [
        "Choose a reminder", "Edit reminder", "Cancel reminder", "Create reminder",
    ]


@pytest.mark.asyncio
async def test_a_channel_without_reminders_still_offers_create_and_fills_in_after_saving():
    service = EventSchedulerService()
    handler = EventHandler(service, Mock(), {7})

    member = guild_interaction()
    await handler._handle_list_events(member)
    shown = member.edit_original_response.call_args.kwargs
    assert shown["embed"].title == "No reminders in this channel" and "view" not in shown
    assert "A server manager can create one with `/reminders`." in shown["embed"].description
    member.followup.send.assert_not_awaited()  # Nothing is posted in the channel.

    manager = guild_interaction(manage_guild=True)
    await handler._handle_list_events(manager)
    shown = manager.edit_original_response.call_args.kwargs
    panel = shown["view"]
    assert "Press **Create reminder** to add one." in shown["embed"].description
    assert labels(panel) == ["Create reminder"]
    manager.followup.send.assert_not_awaited()

    clicked = guild_interaction(manage_guild=True)
    await panel.create.callback(clicked)
    form = clicked.response.send_modal.call_args.args[0]
    form.when._value = "2099-01-01 18:30"
    form.message_input._value = "Bear Trap"
    form.repeat._value = ""
    submitted = guild_interaction(manage_guild=True)
    await form.on_submit(submitted)
    await submitted.response.send_message.call_args.kwargs["view"].save.callback(guild_interaction(manage_guild=True))

    # The panel that was empty now shows the new reminder with all its controls.
    (event,) = await service.get_events_for_channel(123)
    redrawn = panel.message.edit.call_args.kwargs
    assert redrawn["embed"].title == f"Reminder #{event.id}" and redrawn["view"] is panel
    assert labels(panel) == ["Choose a reminder", "Edit reminder", "Cancel reminder", "Create reminder"]

    # Cancelling the last one brings the empty state back, Create included.
    cancel_click = guild_interaction(manage_guild=True)
    cancel_click.data = {"custom_id": f"event:cancel:{event.id}"}
    await panel.cancel.callback(cancel_click)
    await cancel_click.response.send_message.call_args.kwargs["view"].confirm_button.callback(
        guild_interaction(manage_guild=True)
    )
    redrawn = panel.message.edit.call_args.kwargs
    assert redrawn["embed"].title == "No reminders in this channel" and redrawn["attachments"] == []
    assert labels(panel) == ["Create reminder"]


@pytest.mark.asyncio
async def test_saving_from_the_panel_selects_the_saved_reminder_and_survives_a_failed_reload():
    service = EventSchedulerService()
    handler = EventHandler(service, Mock(), {7})
    time = datetime(2099, 1, 1, 12, 0, tzinfo=timezone.utc)
    await service.schedule_event(123, time, ["everyone"], "First")
    panel = EventListView(handler, 7, 123, await service.get_events_for_channel(123), True)
    panel.message = SimpleNamespace(edit=AsyncMock())

    await handler._show_preview(interaction(), "2099-06-01", "09:00", "Second", None, panel=panel)
    submitted = interaction()
    await handler._show_preview(submitted, "2099-06-01", "09:00", "Second", None, panel=panel)
    await submitted.response.send_message.call_args.kwargs["view"].save.callback(interaction())
    assert [event.message for event in panel.events] == ["First", "Second"] and panel.index == 1
    assert panel.message.edit.call_args.kwargs["embed"].title == f"Reminder #{panel.events[1].id}"

    # A reload that fails leaves the panel as it was instead of raising into the save.
    handler._scheduler_service = SimpleNamespace(get_events_for_channel=AsyncMock(side_effect=RuntimeError("db")))
    await panel.reload()
    assert len(panel.events) == 2


@pytest.mark.asyncio
async def test_reminder_dashboard_follows_the_selection_and_falls_back_to_text(monkeypatch):
    time = datetime(2099, 1, 1, 12, 0, tzinfo=timezone.utc)
    events = [
        SimpleNamespace(
            id=index, event_time=time + timedelta(days=index), role_names=[], message=f"Reminder {index}",
            repeat_every_days=7 if index == 2 else None, last_sent_at=None, last_error=None,
        )
        for index in (1, 2, 3)
    ]
    selections = []

    def fake_render(rows, now, *, selected=0):
        selections.append((len(rows), selected))
        return b"\x89PNG\r\n\x1a\n card"

    monkeypatch.setattr("services.reminder_card_renderer.render_reminders_card", fake_render)
    view = EventListView(EventHandler(EventSchedulerService(), Mock(), {7}), 7, 123, events, True)

    click = interaction()
    view.select_event._values = ["2"]
    await view.select_event.callback(click)
    edited = click.response.edit_message.call_args.kwargs
    # Every selection redraws the card with that reminder highlighted and replaces the attachment.
    assert selections == [(3, 1)]
    assert [file.filename for file in edited["attachments"]] == ["reminders.png"]
    assert edited["embed"].title == "Reminder #2" and edited["embed"].image.url == "attachment://reminders.png"
    assert edited["embed"].footer.text == "Reminder 2/3 · Times use your Discord timezone"
    # With a card the short facts share one row.
    assert [field.inline for field in edited["embed"].fields[:3]] == [True, True, True]
    assert [option.default for option in view.select_event.options] == [False, True, False]

    def broken(rows, now, *, selected=0):
        raise RuntimeError("no font")

    monkeypatch.setattr("services.reminder_card_renderer.render_reminders_card", broken)
    click = interaction()
    view.select_event._values = ["3"]
    await view.select_event.callback(click)
    edited = click.response.edit_message.call_args.kwargs
    assert edited["attachments"] == [] and edited["embed"].image.url is None
    assert edited["embed"].title == "Reminder #3"
    assert [field.inline for field in edited["embed"].fields[:3]] == [False, False, False]
