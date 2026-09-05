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


@pytest.mark.asyncio
@pytest.mark.parametrize("persisted", [False, True])
async def test_stable_ids_survive_reordering_and_mutations_are_scoped_to_channel(
    persisted,
):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from db.models import ScheduledReminder

    engine = create_engine("sqlite://")
    ScheduledReminder.__table__.create(engine)

    @asynccontextmanager
    async def session():
        with Session(engine) as sync_session, sync_session.begin():

            async def execute(statement):
                return sync_session.execute(statement)

            async def flush():
                sync_session.flush()

            yield SimpleNamespace(add=sync_session.add, execute=execute, flush=flush)

    clock = Clock(datetime(2026, 5, 11, 12, 0, tzinfo=timezone.utc))
    service = EventSchedulerService(
        db_manager=SimpleNamespace(session=session) if persisted else None,
        now_provider=clock.now,
    )
    time = clock.value + timedelta(hours=1)
    assert await service.schedule_event(123, time, ["everyone"], "First")
    assert await service.schedule_event(123, time + timedelta(hours=1), ["officers"], "Second", 2)
    first, second = await service.get_events_for_channel(123)
    assert first.id is not None and second.id != first.id
    assert await service.schedule_event(123, time - timedelta(minutes=1), ["everyone"], "Earlier")
    assert not await service.cancel_event(456, second.id)
    assert not await service.update_event(456, second.id, time, "Wrong channel")
    assert await service.cancel_event(123, first.id)
    assert not await service.cancel_event(123, first.id)
    assert not await service.update_event(123, first.id, time, "Do not recreate")
    assert await service.update_event(123, second.id, time + timedelta(minutes=5), "Edited", 3)
    events = await service.get_events_for_channel(123)
    assert [event.message for event in events] == ["Earlier", "Edited"]
    assert events[1].id == second.id and events[1].role_names == ["officers"]
    assert events[1].repeat_every_days == 3
    assert not await service.update_event(123, second.id, clock.value, "Past")
    assert not await service.update_event(123, second.id, time, "", 3)
    assert not await service.update_event(123, second.id, time, "Invalid repeat", 366)
    assert not await service.update_event(123, second.id, time, "x" * 1901)
    clock.value = time + timedelta(minutes=5)
    await service.check_and_get_due_events()
    remaining = await service.get_events_for_channel(123)
    assert len(remaining) == 1 and remaining[0].id == second.id
    assert remaining[0].event_time == clock.value + timedelta(days=3)
    engine.dispose()
