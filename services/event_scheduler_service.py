import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from itertools import count
from typing import Awaitable, Callable, Dict, List, Optional, TypeVar

from sqlalchemy import and_, select, update

from db.models import ScheduledReminder
from db.session import DatabaseManager

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Reminders that could not be delivered within this window are reported as missed
# instead of firing late (for example after the bot was offline).
DEFAULT_GRACE_PERIOD = timedelta(minutes=30)
MAX_ERROR_LENGTH = 500


@dataclass(frozen=True)
class ScheduledEvent:
    """A Discord reminder; event_time is its trigger time, not the original event time."""

    id: Optional[int]
    event_time: datetime
    role_names: List[str]
    message: str
    repeat_every_days: Optional[int] = None
    channel_id: Optional[int] = None
    guild_id: Optional[int] = None
    created_by_user_id: Optional[int] = None
    last_sent_at: Optional[datetime] = None
    last_error: Optional[str] = None

    @property
    def is_recurring(self) -> bool:
        """Return whether this reminder repeats after it fires."""
        return self.repeat_every_days is not None


@dataclass(frozen=True)
class DueReminders:
    """Result of one scheduler pickup.

    ``due`` must be delivered and then reported with ``record_delivery``. ``missed``
    reminders were already deactivated (one-time) or advanced (recurring) because
    they were overdue by more than the grace window; they must not be posted.
    """

    due: List[ScheduledEvent] = field(default_factory=list)
    missed: List[ScheduledEvent] = field(default_factory=list)


class IEventSchedulerService(ABC):
    """Interface for event scheduling - Interface Segregation Principle."""

    @abstractmethod
    async def schedule_event(
        self,
        channel_id: int,
        event_time: datetime,
        role_names: List[str],
        message: str,
        repeat_every_days: Optional[int] = None,
        *,
        guild_id: Optional[int] = None,
        created_by_user_id: Optional[int] = None,
    ) -> bool:
        """Schedule a new event."""
        pass

    @abstractmethod
    async def get_events_for_channel(self, channel_id: int) -> List[ScheduledEvent]:
        """Get all events for a specific channel."""
        pass

    @abstractmethod
    async def claim_due_events(self, grace: timedelta = DEFAULT_GRACE_PERIOD) -> DueReminders:
        """Return reminders that should be sent now; overdue ones are marked missed."""
        pass

    @abstractmethod
    async def record_delivery(
        self,
        event: ScheduledEvent,
        *,
        delivered: bool,
        error: Optional[str] = None,
        retry: bool = False,
        deactivate: bool = False,
    ) -> bool:
        """Record one delivery attempt for a claimed occurrence."""
        pass

    @abstractmethod
    async def cancel_event(self, channel_id: int, event_id: int) -> bool:
        """Cancel an active reminder by its stable ID within the channel."""
        pass

    @abstractmethod
    async def update_event(
        self,
        channel_id: int,
        event_id: int,
        event_time: datetime,
        message: str,
        repeat_every_days: Optional[int] = None,
    ) -> bool:
        """Update an active reminder without changing its ID or mention targets."""
        pass


class EventSchedulerService(IEventSchedulerService):
    """Service responsible for scheduling and managing timed events.

    Delivery is two-phase: ``claim_due_events`` selects what is due without
    consuming it, the caller posts the reminder, and ``record_delivery`` advances
    (recurring) or deactivates (one-time) the reminder only after the attempt. A
    transient failure keeps the occurrence due, so it is retried on the next pass
    until the grace window expires and it is recorded as missed.
    """

    def __init__(
        self,
        db_manager: Optional[DatabaseManager] = None,
        now_provider: Optional[Callable[[], datetime]] = None,
    ):
        """Initialize the event scheduler."""
        self._db_manager = db_manager
        self._now_provider = now_provider or (lambda: datetime.now(timezone.utc))
        self._scheduled_events: Dict[int, List[ScheduledEvent]] = {}
        self._event_ids = count(1)
        logger.info("EventSchedulerService initialized")

    async def _transaction(self, operation: Callable[[object], Awaitable[T]]) -> T:
        """Run ``operation`` in a retried transaction when the manager supports it."""
        run_in_transaction = getattr(self._db_manager, "run_in_transaction", None)
        if run_in_transaction is not None:
            return await run_in_transaction(operation)
        async with self._db_manager.session() as session:
            return await operation(session)

    async def schedule_event(
        self,
        channel_id: int,
        event_time: datetime,
        role_names: List[str],
        message: str,
        repeat_every_days: Optional[int] = None,
        *,
        guild_id: Optional[int] = None,
        created_by_user_id: Optional[int] = None,
    ) -> bool:
        """
        Schedule a new event.

        Args:
            channel_id: Discord channel ID
            event_time: When to trigger the event (UTC)
            role_names: List of role names to ping ("everyone" pings @everyone)
            message: Message to send with the ping
            repeat_every_days: Optional recurrence interval in days
            guild_id: Discord guild that owns the channel
            created_by_user_id: Discord user who created the reminder

        Returns:
            True if scheduled successfully
        """
        event_time = self._ensure_utc(event_time)
        if not self._valid_schedule(event_time, message, repeat_every_days):
            return False

        if self._db_manager:

            async def insert(session) -> None:
                session.add(
                    ScheduledReminder(
                        guild_id=guild_id,
                        channel_id=channel_id,
                        reminder_time=event_time,
                        role_names_json=json.dumps(role_names),
                        message=message,
                        repeat_every_days=repeat_every_days,
                        is_active=True,
                        created_by_user_id=created_by_user_id,
                    )
                )
                await session.flush()

            await self._transaction(insert)
            logger.info(f"Persisted event for channel {channel_id} at {event_time}: {message[:50]}...")
            return True

        self._scheduled_events.setdefault(channel_id, []).append(
            ScheduledEvent(
                id=next(self._event_ids),
                event_time=event_time,
                role_names=list(role_names),
                message=message,
                repeat_every_days=repeat_every_days,
                channel_id=channel_id,
                guild_id=guild_id,
                created_by_user_id=created_by_user_id,
            )
        )
        self._scheduled_events[channel_id].sort(key=lambda event: event.event_time)
        logger.info(f"Event scheduled for channel {channel_id} at {event_time}: {message[:50]}...")
        return True

    async def get_events_for_channel(self, channel_id: int) -> List[ScheduledEvent]:
        """Return the channel's active reminders ordered by their next trigger time."""
        if self._db_manager:

            async def load(session) -> List[ScheduledEvent]:
                result = await session.execute(
                    select(ScheduledReminder)
                    .where(
                        and_(
                            ScheduledReminder.channel_id == channel_id,
                            ScheduledReminder.is_active.is_(True),
                        )
                    )
                    .order_by(ScheduledReminder.reminder_time)
                )
                return [self._event_from_model(reminder) for reminder in result.scalars().all()]

            return await self._transaction(load)

        return self._scheduled_events.get(channel_id, []).copy()

    async def claim_due_events(self, grace: timedelta = DEFAULT_GRACE_PERIOD) -> DueReminders:
        """Select due reminders without consuming them.

        Occurrences overdue by more than ``grace`` are not returned for delivery:
        one-time reminders are deactivated and recurring reminders advance to their
        next future occurrence, both with a ``last_error`` explaining the miss. A
        recurring reminder whose latest occurrence is still within ``grace`` is
        returned once for that occurrence; older missed occurrences are skipped.
        """
        now = self._now()
        if self._db_manager:

            async def claim(session) -> DueReminders:
                result = await session.execute(
                    select(ScheduledReminder)
                    .where(
                        and_(
                            ScheduledReminder.is_active.is_(True),
                            ScheduledReminder.reminder_time <= now,
                        )
                    )
                    .order_by(ScheduledReminder.reminder_time)
                    # READ COMMITTED: lock the rows this pass may rewrite (missed or
                    # skipped occurrences) so a concurrent edit is not overwritten.
                    .with_for_update()
                )
                due, missed = [], []
                for reminder in result.scalars().all():
                    event = self._event_from_model(reminder)
                    occurrence = self._latest_occurrence(event, now)
                    if now - occurrence > grace:
                        changes = self._missed_changes(event, occurrence, now, grace)
                        for name, value in changes.items():
                            setattr(reminder, name, value)
                        missed.append(self._apply_changes(event, changes))
                        continue
                    if occurrence != event.event_time:
                        # Skip earlier missed occurrences; deliver only the latest one.
                        reminder.reminder_time = occurrence
                    due.append(replace(event, event_time=occurrence))
                return DueReminders(due=due, missed=missed)

            return await self._transaction(claim)

        due, missed = [], []
        for channel_id, events in list(self._scheduled_events.items()):
            remaining = []
            for event in events:
                if event.event_time > now:
                    remaining.append(event)
                    continue
                occurrence = self._latest_occurrence(event, now)
                if now - occurrence > grace:
                    changes = self._missed_changes(event, occurrence, now, grace)
                    updated = self._apply_changes(event, changes)
                    missed.append(updated)
                    if changes.get("is_active", True):
                        remaining.append(updated)
                    continue
                event = replace(event, event_time=occurrence)
                remaining.append(event)
                due.append(event)
            remaining.sort(key=lambda item: item.event_time)
            self._scheduled_events[channel_id] = remaining
            self._cleanup_channel(channel_id)
        return DueReminders(due=due, missed=missed)

    async def record_delivery(
        self,
        event: ScheduledEvent,
        *,
        delivered: bool,
        error: Optional[str] = None,
        retry: bool = False,
        deactivate: bool = False,
    ) -> bool:
        """Record an attempt for the occurrence returned by ``claim_due_events``.

        ``delivered`` sets ``last_sent_at``; ``error`` is stored as ``last_error``
        (it may be a warning for a delivered reminder). A non-retryable outcome
        advances or deactivates the reminder; ``retry=True`` keeps it due, and
        ``deactivate=True`` stops even a recurring reminder (its channel is gone).
        Returns False when the reminder was cancelled or edited meanwhile, in
        which case the newer state is left untouched.
        """
        if event.id is None:
            return False
        now = self._now()
        error = error[:MAX_ERROR_LENGTH] if error else None
        changes: dict = {"last_error": error}
        if delivered:
            changes["last_sent_at"] = now
        if deactivate:
            changes["is_active"] = False
        elif delivered or not retry:
            if event.is_recurring:
                changes["reminder_time"] = self._next_recurring_time(event, now)
            else:
                changes["is_active"] = False

        if self._db_manager:

            async def record(session) -> bool:
                # FOR UPDATE: under READ COMMITTED an edit or cancel committed between
                # this check and the write below would otherwise be overwritten.
                reminder = (
                    await session.execute(
                        select(ScheduledReminder).where(ScheduledReminder.id == event.id).with_for_update()
                    )
                ).scalar_one_or_none()
                if (
                    reminder is None
                    or not reminder.is_active
                    or self._ensure_utc(reminder.reminder_time) != event.event_time
                ):
                    return False
                for name, value in changes.items():
                    setattr(reminder, name, value)
                return True

            return await self._transaction(record)

        channel_id = event.channel_id
        events = self._scheduled_events.get(channel_id, []) if channel_id is not None else []
        for index, current in enumerate(events):
            if current.id == event.id and current.event_time == event.event_time:
                if changes.get("is_active", True):
                    events[index] = self._apply_changes(current, changes)
                else:
                    events.pop(index)
                events.sort(key=lambda item: item.event_time)
                self._cleanup_channel(channel_id)
                return True
        return False

    async def cancel_event(self, channel_id: int, event_id: int) -> bool:
        """Cancel by stable ID so reordered or outdated lists cannot target another event."""
        if self._db_manager:

            async def cancel(session) -> bool:
                result = await session.execute(
                    update(ScheduledReminder)
                    .where(
                        ScheduledReminder.id == event_id,
                        ScheduledReminder.channel_id == channel_id,
                        ScheduledReminder.is_active.is_(True),
                    )
                    .values(is_active=False)
                    .returning(ScheduledReminder.id)
                )
                return result.scalar_one_or_none() is not None

            return await self._transaction(cancel)

        events = self._scheduled_events.get(channel_id, [])
        for index, event in enumerate(events):
            if event.id == event_id:
                events.pop(index)
                self._cleanup_channel(channel_id)
                return True
        return False

    async def update_event(
        self,
        channel_id: int,
        event_id: int,
        event_time: datetime,
        message: str,
        repeat_every_days: Optional[int] = None,
    ) -> bool:
        """Update a channel's active reminder, preserving identity and mention targets."""
        event_time = self._ensure_utc(event_time)
        if not self._valid_schedule(event_time, message, repeat_every_days):
            return False
        if self._db_manager:

            async def edit(session) -> bool:
                result = await session.execute(
                    update(ScheduledReminder)
                    .where(
                        ScheduledReminder.id == event_id,
                        ScheduledReminder.channel_id == channel_id,
                        ScheduledReminder.is_active.is_(True),
                    )
                    .values(
                        reminder_time=event_time,
                        message=message,
                        repeat_every_days=repeat_every_days,
                    )
                    .returning(ScheduledReminder.id)
                )
                return result.scalar_one_or_none() is not None

            return await self._transaction(edit)
        events = self._scheduled_events.get(channel_id, [])
        for index, event in enumerate(events):
            if event.id == event_id:
                events[index] = replace(
                    event,
                    event_time=event_time,
                    message=message,
                    repeat_every_days=repeat_every_days,
                )
                events.sort(key=lambda item: item.event_time)
                return True
        return False

    def _cleanup_channel(self, channel_id: Optional[int]) -> None:
        if channel_id is not None and not self._scheduled_events.get(channel_id, True):
            del self._scheduled_events[channel_id]

    def _valid_schedule(self, event_time: datetime, message: str, repeat_every_days: Optional[int]) -> bool:
        return (
            event_time > self._now()
            and bool(message.strip())
            and len(message) <= 1900
            and (repeat_every_days is None or 1 <= repeat_every_days <= 365)
        )

    def _missed_changes(
        self,
        event: ScheduledEvent,
        occurrence: datetime,
        now: datetime,
        grace: timedelta,
    ) -> dict:
        """Row changes for an occurrence that is too late to post."""
        minutes = int(grace.total_seconds() // 60)
        due = occurrence.strftime("%Y-%m-%d %H:%M UTC")
        reason = (
            f"Missed: not delivered within {minutes} minutes of {due} "
            "(bot offline or Discord unavailable)."
        )
        if event.is_recurring:
            return {"reminder_time": self._next_recurring_time(event, now), "last_error": reason}
        return {"is_active": False, "last_error": reason}

    @staticmethod
    def _apply_changes(event: ScheduledEvent, changes: dict) -> ScheduledEvent:
        """Apply row changes (column names) to the in-memory event shape."""
        public = {key: value for key, value in changes.items() if key != "is_active"}
        if "reminder_time" in public:
            public["event_time"] = public.pop("reminder_time")
        return replace(event, **public)

    @staticmethod
    def _latest_occurrence(event: ScheduledEvent, now: datetime) -> datetime:
        """Latest trigger time that is not in the future (the stored time for one-time events)."""
        if event.repeat_every_days is None or event.event_time > now:
            return event.event_time
        interval = timedelta(days=event.repeat_every_days)
        return event.event_time + interval * ((now - event.event_time) // interval)

    def _next_recurring_time(self, event: ScheduledEvent, current_time: datetime) -> datetime:
        """Calculate the next future trigger time for a recurring event."""
        if event.repeat_every_days is None:
            return event.event_time

        interval = timedelta(days=event.repeat_every_days)
        next_time = event.event_time + interval
        if next_time <= current_time:
            next_time += interval * ((current_time - next_time) // interval + 1)
        return next_time

    def _event_from_model(self, reminder: ScheduledReminder) -> ScheduledEvent:
        """Convert a persisted reminder row into the scheduler event shape."""
        try:
            role_names = json.loads(reminder.role_names_json)
            if not isinstance(role_names, list):
                role_names = ["everyone"]
        except (TypeError, json.JSONDecodeError):
            role_names = ["everyone"]

        last_sent_at = getattr(reminder, "last_sent_at", None)
        return ScheduledEvent(
            id=reminder.id,
            event_time=self._ensure_utc(reminder.reminder_time),
            role_names=[str(role_name) for role_name in role_names],
            message=reminder.message,
            repeat_every_days=reminder.repeat_every_days,
            channel_id=reminder.channel_id,
            guild_id=getattr(reminder, "guild_id", None),
            created_by_user_id=getattr(reminder, "created_by_user_id", None),
            last_sent_at=self._ensure_utc(last_sent_at) if last_sent_at else None,
            last_error=getattr(reminder, "last_error", None),
        )

    def _now(self) -> datetime:
        """Return timezone-aware UTC now."""
        now = self._now_provider()
        return self._ensure_utc(now)

    @staticmethod
    def _ensure_utc(value: datetime) -> datetime:
        """Return a timezone-aware UTC datetime."""
        now = value
        if now.tzinfo is None:
            return now.replace(tzinfo=timezone.utc)
        return now.astimezone(timezone.utc)
