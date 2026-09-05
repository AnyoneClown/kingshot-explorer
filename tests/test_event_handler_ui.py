from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from handlers.event_handler import EventHandler, EventListView, parse_event_time
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
    handler._admin_user_ids.clear()
    await preview.save.callback(interaction())
    assert await service.get_events_for_channel(123) == []
    handler._admin_user_ids.add(7)
    await preview.save.callback(interaction(8))
    assert await service.get_events_for_channel(123) == []
    await preview.save.callback(interaction())
    await preview.save.callback(interaction())
    events = await service.get_events_for_channel(123)
    assert len(events) == 1 and events[0].repeat_every_days == 2
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
    await view.cancel.callback(
        interaction(custom_id=f"event:cancel:{events[0].id}")
    )  # The stale first card must not cancel its new neighbor.
    assert len(await service.get_events_for_channel(123)) == 25
    view.index = 1
    cancelled_id = events[1].id
    await view.cancel.callback(interaction(custom_id=f"event:cancel:{cancelled_id}"))
    await view.cancel.callback(interaction(custom_id=f"event:cancel:{cancelled_id}"))
    assert len(await service.get_events_for_channel(123)) == 24
    assert events[1].id not in [event.id for event in await service.get_events_for_channel(123)]


@pytest.mark.asyncio
async def test_cancel_accepts_full_precision_ids_and_rejects_non_numeric_input():
    service = SimpleNamespace(cancel_event=AsyncMock(return_value=True))
    handler = EventHandler(service, Mock(), {7})
    await handler._handle_cancel_event(interaction(), "9007199254740993")
    service.cancel_event.assert_awaited_once_with(123, 9007199254740993)
    await handler._handle_cancel_event(interaction(), "not-an-id")
    service.cancel_event.assert_awaited_once()
