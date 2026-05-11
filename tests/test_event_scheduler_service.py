from datetime import datetime, timedelta, timezone

import pytest

from services.event_scheduler_service import EventSchedulerService


class Clock:
    def __init__(self, value: datetime):
        self.value = value

    def now(self) -> datetime:
        return self.value


@pytest.mark.asyncio
async def test_one_time_event_is_removed_after_it_is_due():
    clock = Clock(datetime(2026, 5, 11, 12, 0, tzinfo=timezone.utc))
    service = EventSchedulerService(now_provider=clock.now)
    event_time = clock.value + timedelta(minutes=5)

    assert await service.schedule_event(123, event_time, ["everyone"], "Show up")

    clock.value = event_time
    due_events = await service.check_and_get_due_events()

    assert due_events[123][0].message == "Show up"
    assert await service.get_events_for_channel(123) == []


@pytest.mark.asyncio
async def test_recurring_event_is_rescheduled_after_it_is_due():
    clock = Clock(datetime(2026, 5, 11, 12, 0, tzinfo=timezone.utc))
    service = EventSchedulerService(now_provider=clock.now)
    event_time = clock.value + timedelta(minutes=5)

    assert await service.schedule_event(123, event_time, ["everyone"], "Collect rewards", repeat_every_days=2)

    clock.value = event_time
    due_events = await service.check_and_get_due_events()
    scheduled_events = await service.get_events_for_channel(123)

    assert due_events[123][0].message == "Collect rewards"
    assert due_events[123][0].repeat_every_days == 2
    assert len(scheduled_events) == 1
    assert scheduled_events[0].event_time == event_time + timedelta(days=2)


@pytest.mark.asyncio
async def test_recurring_event_advances_to_next_future_time_after_late_check():
    clock = Clock(datetime(2026, 5, 11, 12, 0, tzinfo=timezone.utc))
    service = EventSchedulerService(now_provider=clock.now)
    event_time = clock.value + timedelta(minutes=5)

    assert await service.schedule_event(123, event_time, ["everyone"], "Every other day", repeat_every_days=2)

    clock.value = event_time + timedelta(days=5)
    due_events = await service.check_and_get_due_events()
    scheduled_events = await service.get_events_for_channel(123)

    assert len(due_events[123]) == 1
    assert scheduled_events[0].event_time == event_time + timedelta(days=6)


@pytest.mark.asyncio
async def test_invalid_recurrence_is_rejected():
    clock = Clock(datetime(2026, 5, 11, 12, 0, tzinfo=timezone.utc))
    service = EventSchedulerService(now_provider=clock.now)
    event_time = clock.value + timedelta(minutes=5)

    assert not await service.schedule_event(123, event_time, ["everyone"], "Bad repeat", repeat_every_days=0)
    assert await service.get_events_for_channel(123) == []
