from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from db.models import ScheduledReminder
from services.event_scheduler_service import EventSchedulerService


class Clock:
    def __init__(self, value: datetime):
        self.value = value

    def now(self) -> datetime:
        return self.value


class FakeDb:
    """SQLite-backed stand-in for DatabaseManager (session + run_in_transaction)."""

    def __init__(self, engine):
        self.engine = engine
        self.transactions = 0

    @asynccontextmanager
    async def session(self):
        with Session(self.engine) as sync_session, sync_session.begin():

            async def execute(statement):
                return sync_session.execute(statement)

            async def flush():
                sync_session.flush()

            yield SimpleNamespace(add=sync_session.add, execute=execute, flush=flush)

    async def run_in_transaction(self, operation):
        self.transactions += 1
        async with self.session() as session:
            return await operation(session)


@pytest.fixture
def make_service():
    engines = []

    def build(persisted: bool, start=datetime(2026, 5, 11, 12, 0, tzinfo=timezone.utc)):
        clock = Clock(start)
        db = None
        if persisted:
            engine = create_engine("sqlite://")
            ScheduledReminder.__table__.create(engine)
            engines.append(engine)
            db = FakeDb(engine)
        return EventSchedulerService(db_manager=db, now_provider=clock.now), clock, db

    yield build
    for engine in engines:
        engine.dispose()


def _rows(db):
    with Session(db.engine) as session:
        return {row.id: row for row in session.scalars(select(ScheduledReminder))}


@pytest.mark.asyncio
@pytest.mark.parametrize("persisted", [False, True])
async def test_one_time_event_is_removed_only_after_delivery_is_recorded(make_service, persisted):
    service, clock, db = make_service(persisted)
    event_time = clock.value + timedelta(minutes=5)
    assert await service.schedule_event(
        123, event_time, ["everyone"], "Show up", guild_id=99, created_by_user_id=7
    )

    clock.value = event_time
    batch = await service.claim_due_events()
    assert [event.message for event in batch.due] == ["Show up"] and batch.missed == []
    due = batch.due[0]
    assert due.channel_id == 123 and due.guild_id == 99 and due.created_by_user_id == 7
    # Claiming does not consume: a crash before sending leaves the reminder due.
    assert len(await service.get_events_for_channel(123)) == 1

    assert await service.record_delivery(due, delivered=True)
    assert await service.get_events_for_channel(123) == []
    assert (await service.claim_due_events()).due == []
    assert not await service.record_delivery(due, delivered=True)
    if persisted:
        row = _rows(db)[due.id]
        assert row.guild_id == 99 and row.created_by_user_id == 7
        assert not row.is_active and row.last_sent_at is not None and row.last_error is None
        assert db.transactions > 0


@pytest.mark.asyncio
@pytest.mark.parametrize("persisted", [False, True])
async def test_recurring_event_is_rescheduled_after_delivery(make_service, persisted):
    service, clock, _ = make_service(persisted)
    event_time = clock.value + timedelta(minutes=5)
    assert await service.schedule_event(123, event_time, ["everyone"], "Collect rewards", repeat_every_days=2)

    clock.value = event_time
    due = (await service.claim_due_events()).due
    assert due[0].message == "Collect rewards" and due[0].repeat_every_days == 2
    assert await service.record_delivery(due[0], delivered=True)
    scheduled_events = await service.get_events_for_channel(123)
    assert len(scheduled_events) == 1
    assert scheduled_events[0].event_time == event_time + timedelta(days=2)
    assert scheduled_events[0].last_sent_at == event_time


@pytest.mark.asyncio
@pytest.mark.parametrize("persisted", [False, True])
async def test_transient_failure_keeps_reminder_due_until_grace_window_expires(make_service, persisted):
    service, clock, db = make_service(persisted)
    event_time = clock.value + timedelta(minutes=5)
    assert await service.schedule_event(123, event_time, ["everyone"], "Bear Trap")

    clock.value = event_time
    due = (await service.claim_due_events()).due[0]
    assert await service.record_delivery(due, delivered=False, error="Discord error 503", retry=True)
    [still_active] = await service.get_events_for_channel(123)
    assert still_active.last_error == "Discord error 503" and still_active.event_time == event_time

    clock.value = event_time + timedelta(minutes=1)
    assert [event.id for event in (await service.claim_due_events()).due] == [due.id]

    clock.value = event_time + timedelta(minutes=31)
    batch = await service.claim_due_events()
    assert batch.due == [] and [event.id for event in batch.missed] == [due.id]
    assert batch.missed[0].last_error.startswith("Missed: not delivered within 30 minutes")
    assert await service.get_events_for_channel(123) == []
    if persisted:
        row = _rows(db)[due.id]
        assert not row.is_active and row.last_sent_at is None and "Missed" in row.last_error


@pytest.mark.asyncio
@pytest.mark.parametrize("persisted", [False, True])
async def test_permanent_failure_records_error_and_advances_instead_of_retrying(make_service, persisted):
    service, clock, _ = make_service(persisted)
    event_time = clock.value + timedelta(minutes=5)
    assert await service.schedule_event(123, event_time, ["everyone"], "Daily", repeat_every_days=1)

    clock.value = event_time
    due = (await service.claim_due_events()).due[0]
    assert await service.record_delivery(due, delivered=False, error="Missing permission")
    [event] = await service.get_events_for_channel(123)
    assert event.event_time == event_time + timedelta(days=1)
    assert event.last_error == "Missing permission" and event.last_sent_at is None
    assert (await service.claim_due_events()).due == []

    clock.value = event.event_time
    due = (await service.claim_due_events()).due[0]
    assert await service.record_delivery(due, delivered=True)
    [event] = await service.get_events_for_channel(123)
    assert event.last_error is None and event.last_sent_at == clock.value


@pytest.mark.asyncio
@pytest.mark.parametrize("persisted", [False, True])
async def test_downtime_marks_overdue_one_time_missed_and_skips_recurring_catch_up(make_service, persisted):
    service, clock, _ = make_service(persisted)
    start = clock.value
    assert await service.schedule_event(1, start + timedelta(hours=1), ["everyone"], "One-time")
    assert await service.schedule_event(2, start + timedelta(hours=1), ["everyone"], "Every 2 days", 2)
    assert await service.schedule_event(3, start + timedelta(minutes=10), ["everyone"], "Daily", 1)

    # Offline for ~3 days; the daily reminder's latest occurrence was 5 minutes ago.
    clock.value = start + timedelta(days=3, minutes=15)
    batch = await service.claim_due_events()
    assert sorted(event.message for event in batch.missed) == ["Every 2 days", "One-time"]
    assert [event.message for event in batch.due] == ["Daily"]
    assert batch.due[0].event_time == start + timedelta(days=3, minutes=10)

    assert await service.get_events_for_channel(1) == []
    [every_two] = await service.get_events_for_channel(2)
    assert every_two.event_time == start + timedelta(days=4, hours=1)
    assert "Missed" in every_two.last_error

    assert await service.record_delivery(batch.due[0], delivered=True)
    [daily] = await service.get_events_for_channel(3)
    assert daily.event_time == start + timedelta(days=4, minutes=10)
    assert (await service.claim_due_events()).due == []


@pytest.mark.asyncio
@pytest.mark.parametrize("persisted", [False, True])
async def test_recording_does_not_clobber_an_edit_or_cancel_made_after_pickup(make_service, persisted):
    service, clock, _ = make_service(persisted)
    event_time = clock.value + timedelta(minutes=5)
    assert await service.schedule_event(123, event_time, ["everyone"], "Original")
    assert await service.schedule_event(123, event_time, ["everyone"], "To cancel")
    clock.value = event_time
    original, to_cancel = (await service.claim_due_events()).due

    assert await service.update_event(123, original.id, event_time + timedelta(hours=2), "Edited")
    assert await service.cancel_event(123, to_cancel.id)
    assert not await service.record_delivery(original, delivered=True)
    assert not await service.record_delivery(to_cancel, delivered=True)
    [event] = await service.get_events_for_channel(123)
    assert event.message == "Edited" and event.event_time == event_time + timedelta(hours=2)


@pytest.mark.asyncio
async def test_invalid_recurrence_is_rejected():
    clock = Clock(datetime(2026, 5, 11, 12, 0, tzinfo=timezone.utc))
    service = EventSchedulerService(now_provider=clock.now)
    event_time = clock.value + timedelta(minutes=5)

    assert not await service.schedule_event(123, event_time, ["everyone"], "Bad repeat", repeat_every_days=0)
    assert await service.get_events_for_channel(123) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("persisted", [False, True])
async def test_stable_ids_survive_reordering_and_mutations_are_scoped_to_channel(make_service, persisted):
    service, clock, _ = make_service(persisted)
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
    for event in (await service.claim_due_events()).due:
        await service.record_delivery(event, delivered=True)
    remaining = await service.get_events_for_channel(123)
    assert len(remaining) == 1 and remaining[0].id == second.id
    assert remaining[0].event_time == clock.value + timedelta(days=3)
