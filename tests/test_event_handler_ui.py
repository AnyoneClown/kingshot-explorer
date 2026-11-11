from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from handlers.event_handler import EventHandler, EventListView, parse_event_time
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
        edit_original_response=AsyncMock(),
    )


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


@pytest.mark.asyncio
async def test_schedule_requires_preview_confirmation_and_rechecks_authorization():
    service = EventSchedulerService()
    handler = EventHandler(service, Mock(), {7})
    denied = interaction(8)
    await handler._handle_schedule_event(denied)
    denied.response.send_modal.assert_not_awaited()
    opened = interaction()
    await handler._handle_schedule_event(opened)
    modal = opened.response.send_modal.call_args.args[0]
    assert len(modal.children) == 5
    invalid = interaction()
    await handler._handle_schedule_event(invalid, message="x" * 1901)
    invalid.response.send_modal.assert_not_awaited()
    modal.when._value = "2099-01-01 18:30"
    modal.zone._value = "Europe/Kyiv"
    modal.message_input._value = "Alliance prep"
    modal.repeat._value = "2"
    modal.lead._value = "10"
    await modal.on_submit(denied)
    assert await service.get_events_for_channel(123) == []
    submitted = interaction()
    await modal.on_submit(submitted)
    preview = submitted.response.send_message.call_args.kwargs["view"]
    assert await service.get_events_for_channel(123) == []
    assert preview.event.event_time == datetime(2099, 1, 1, 16, 20, tzinfo=timezone.utc)
    handler._access = AccessPolicy(set())
    await preview.save.callback(interaction())
    assert await service.get_events_for_channel(123) == []
    handler._access = AccessPolicy({7})
    await preview.save.callback(interaction(8))
    assert await service.get_events_for_channel(123) == []
    await preview.save.callback(interaction())
    await preview.save.callback(interaction())
    events = await service.get_events_for_channel(123)
    assert len(events) == 1 and events[0].repeat_every_days == 2
    assert events[0].created_by_user_id == 7 and events[0].role_names == ["everyone"]
    slash = interaction()
    await handler._handle_schedule_event(slash, "2099-02-01", "18:30", "From slash options")
    assert slash.response.send_message.call_args.kwargs["view"].event.id is None
    assert len(await service.get_events_for_channel(123)) == 1


@pytest.mark.asyncio
async def test_event_cards_paginate_edit_reminder_time_and_cancel_stable_ids():
    service = EventSchedulerService()
    handler = EventHandler(service, Mock(), {7})
    time = datetime(2099, 1, 1, 12, 0, tzinfo=timezone.utc)
    for index in range(26):
        await service.schedule_event(123, time + timedelta(hours=index), ["everyone"], "x" * 1900)
    events = await service.get_events_for_channel(123)
    view = EventListView(handler, 7, 123, events, True)
    assert len(view.select_event.options) == 25
    assert all(len(field.value) <= 1024 for field in view.embed().fields)
    assert len(view.embed()) < 6000
    view.index = 24
    await view.next.callback(interaction())
    assert view.index == 25 and len(view.select_event.options) == 1 and view.next.disabled
    assert not await view.interaction_check(interaction(8))
    view.index = 0
    opened = interaction(custom_id=f"event:edit:{events[0].id}")
    await view.edit.callback(opened)
    modal = opened.response.send_modal.call_args.args[0]
    assert modal.when.label == "Reminder date/time" and len(modal.children) == 4
    modal.when._value = "2099-01-03 12:00"
    modal.zone._value = "UTC"
    modal.message_input._value = "Edited"
    modal.repeat._value = ""
    submitted = interaction()
    await modal.on_submit(submitted)
    preview = submitted.response.send_message.call_args.kwargs["view"]
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
    assert [item.label for item in prompt.children] == ["Cancel event", "Keep"]
    assert not await prompt.interaction_check(interaction(8))
    confirmed = interaction()
    await prompt.confirm_button.callback(confirmed)
    assert confirmed.edit_original_response.call_args.kwargs["embed"].title == "Event cancelled"
    await prompt.confirm_button.callback(interaction())  # A used prompt cannot cancel twice.
    assert len(await service.get_events_for_channel(123)) == 24
    assert cancelled_id not in [event.id for event in await service.get_events_for_channel(123)]
    assert cancelled_id not in [event.id for event in view.events]

    kept = interaction(custom_id=f"event:cancel:{view.events[0].id}")
    await view.cancel.callback(kept)
    await kept.response.send_message.call_args.kwargs["view"].keep_button.callback(interaction())
    assert len(await service.get_events_for_channel(123)) == 24


def guild_interaction(user_id=8, *, manage_guild=False, channel_id=123):
    item = interaction(user_id, channel_id)
    item.guild_id = 10
    item.user.guild_permissions = SimpleNamespace(manage_guild=manage_guild, administrator=False)
    item.user.roles = []
    item.followup.send = AsyncMock(return_value=SimpleNamespace(edit=AsyncMock()))
    item.delete_original_response = AsyncMock()
    return item


@pytest.mark.asyncio
async def test_managers_are_resolved_by_access_policy_and_commands_are_hidden():
    import discord
    from discord.ext import commands

    service = EventSchedulerService()
    handler = EventHandler(service, Mock(), {7})
    member = guild_interaction(manage_guild=False)
    await handler._handle_schedule_event(member)
    member.response.send_modal.assert_not_awaited()
    assert "Manage Server" in member.response.send_message.call_args.kwargs["embed"].description
    manager = guild_interaction(manage_guild=True)
    await handler._handle_schedule_event(manager)
    manager.response.send_modal.assert_awaited_once()

    bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
    EventHandler(service, bot, {7}).register_commands()
    assert bot.tree.get_command("remind").default_permissions.manage_guild
    assert bot.tree.get_command("cancel") is None  # cancelling is a button on the reminder list
    assert bot.tree.get_command("reminders").default_permissions is None
    await bot.close()


@pytest.mark.asyncio
async def test_saved_reminders_record_guild_and_creator():
    service = SimpleNamespace(schedule_event=AsyncMock(return_value=True))
    handler = EventHandler(service, Mock(), {7})
    submitted = guild_interaction(manage_guild=True)
    await handler._handle_schedule_event(submitted, "2099-02-01", "18:30", "Bear Trap")
    preview = submitted.response.send_message.call_args.kwargs["view"]
    await preview.save.callback(guild_interaction(manage_guild=True))
    kwargs = service.schedule_event.call_args.kwargs
    assert kwargs == {"guild_id": 10, "created_by_user_id": 8}

    service.schedule_event.side_effect = RuntimeError("database unavailable")
    again = guild_interaction(manage_guild=True)
    await handler._handle_schedule_event(again, "2099-02-01", "18:30", "Bear Trap")
    preview = again.response.send_message.call_args.kwargs["view"]
    failed = guild_interaction(manage_guild=True)
    failed.response.is_done = Mock(return_value=True)
    await preview.save.callback(failed)
    assert failed.followup.send.call_args.kwargs["ephemeral"]
    assert not preview.saving  # The draft can be saved again.


@pytest.mark.asyncio
async def test_events_list_handles_database_errors_and_posts_public_panel():
    service = SimpleNamespace(get_events_for_channel=AsyncMock(side_effect=RuntimeError("db down")))
    handler = EventHandler(service, Mock(), {7})
    failed = guild_interaction()
    failed.response.is_done = Mock(return_value=True)
    await handler._handle_list_events(failed)
    assert failed.response.defer.call_args.kwargs["ephemeral"]
    sent = failed.followup.send.call_args.kwargs
    assert sent["ephemeral"] and sent["embed"].title == "Events unavailable"

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
    assert sent["view"].cancel.disabled  # Members can browse but not manage.
    fields = {field.name: field.value for field in sent["embed"].fields}
    assert fields["Pings"] == "@Officers" and "Missing permission" in fields["Last problem"]
    member.delete_original_response.assert_awaited_once()
