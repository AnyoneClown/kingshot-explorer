import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from itertools import count
from typing import Callable, Dict, List, Optional

from sqlalchemy import and_, select, update

from db.models import ScheduledReminder
from db.session import DatabaseManager

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScheduledEvent:
    """A Discord reminder; event_time is its trigger time, not the original event time."""

    id: Optional[int]
    event_time: datetime
    role_names: List[str]
    message: str
    repeat_every_days: Optional[int] = None

    @property
    def is_recurring(self) -> bool:
        """Return whether this reminder repeats after it fires."""
        return self.repeat_every_days is not None


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
    ) -> bool:
        """Schedule a new event."""
        pass

    @abstractmethod
    async def get_events_for_channel(self, channel_id: int) -> List[ScheduledEvent]:
        """Get all events for a specific channel."""
        pass

    @abstractmethod
    async def check_and_get_due_events(
        self,
    ) -> Dict[int, List[ScheduledEvent]]:
        """Check and return events that are due."""
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
    """Service responsible for scheduling and managing timed events."""

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

    async def schedule_event(
        self,
        channel_id: int,
        event_time: datetime,
        role_names: List[str],
        message: str,
        repeat_every_days: Optional[int] = None,
    ) -> bool:
        """
        Schedule a new event.

        Args:
            channel_id: Discord channel ID
            event_time: When to trigger the event (UTC)
            role_names: List of role names to ping
            message: Message to send with the ping
            repeat_every_days: Optional recurrence interval in days

        Returns:
            True if scheduled successfully
        """
        event_time = self._ensure_utc(event_time)
        if not self._valid_schedule(event_time, message, repeat_every_days):
            return False

        if self._db_manager:
            async with self._db_manager.session() as session:
                reminder = ScheduledReminder(
                    channel_id=channel_id,
                    reminder_time=event_time,
                    role_names_json=json.dumps(role_names),
                    message=message,
                    repeat_every_days=repeat_every_days,
                    is_active=True,
                )
                session.add(reminder)
                await session.flush()

            logger.info(f"Persisted event for channel {channel_id} at {event_time}: {message[:50]}...")
            return True

        if channel_id not in self._scheduled_events:
            self._scheduled_events[channel_id] = []

        self._scheduled_events[channel_id].append(
            ScheduledEvent(
                id=next(self._event_ids),
                event_time=event_time,
                role_names=role_names,
                message=message,
                repeat_every_days=repeat_every_days,
            )
        )
        self._scheduled_events[channel_id].sort(key=lambda event: event.event_time)
        logger.info(f"Event scheduled for channel {channel_id} at {event_time}: {message[:50]}...")
        return True

    async def get_events_for_channel(self, channel_id: int) -> List[ScheduledEvent]:
        """
        Get all scheduled events for a channel.

        Args:
            channel_id: Discord channel ID

        Returns:
            List of events (event_time, role_names, message)
        """
        if self._db_manager:
            async with self._db_manager.session() as session:
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

        return self._scheduled_events.get(channel_id, []).copy()

    async def check_and_get_due_events(
        self,
    ) -> Dict[int, List[ScheduledEvent]]:
        """
        Check for due events, removing one-shot events and advancing recurring ones.

        Returns:
            Dictionary mapping channel_id to list of due events
        """
        current_time = self._now()
        due_events: Dict[int, List[ScheduledEvent]] = {}

        if self._db_manager:
            async with self._db_manager.session() as session:
                result = await session.execute(
                    select(ScheduledReminder)
                    .where(
                        and_(
                            ScheduledReminder.is_active.is_(True),
                            ScheduledReminder.reminder_time <= current_time,
                        )
                    )
                    .order_by(ScheduledReminder.reminder_time)
                )
                reminders = result.scalars().all()

                for reminder in reminders:
                    event = self._event_from_model(reminder)
                    due_events.setdefault(reminder.channel_id, []).append(event)

                    if event.is_recurring:
                        reminder.reminder_time = self._next_recurring_time(event, current_time)
                    else:
                        reminder.is_active = False

            return due_events

        for channel_id, events in list(self._scheduled_events.items()):
            channel_due_events = []
            remaining_events = []

            for event in events:
                if current_time >= event.event_time:
                    channel_due_events.append(event)
                    if event.is_recurring:
                        remaining_events.append(self._advance_recurring_event(event, current_time))
                else:
                    remaining_events.append(event)

            if channel_due_events:
                due_events[channel_id] = channel_due_events

            if remaining_events:
                remaining_events.sort(key=lambda event: event.event_time)
                self._scheduled_events[channel_id] = remaining_events
            else:
                del self._scheduled_events[channel_id]

        return due_events

    async def cancel_event(self, channel_id: int, event_id: int) -> bool:
        """Cancel by stable ID so reordered or outdated lists cannot target another event."""
        if self._db_manager:
            async with self._db_manager.session() as session:
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

        events = self._scheduled_events.get(channel_id, [])
        for index, event in enumerate(events):
            if event.id == event_id:
                events.pop(index)
                if not events:
                    del self._scheduled_events[channel_id]
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
            async with self._db_manager.session() as session:
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

    def _valid_schedule(self, event_time: datetime, message: str, repeat_every_days: Optional[int]) -> bool:
        return (
            event_time > self._now()
            and bool(message.strip())
            and len(message) <= 1900
            and (repeat_every_days is None or 1 <= repeat_every_days <= 365)
        )

    def _advance_recurring_event(self, event: ScheduledEvent, current_time: datetime) -> ScheduledEvent:
        """Advance a recurring event to the next future trigger time."""
        if event.repeat_every_days is None:
            return event

        return ScheduledEvent(
            id=event.id,
            event_time=self._next_recurring_time(event, current_time),
            role_names=event.role_names,
            message=event.message,
            repeat_every_days=event.repeat_every_days,
        )

    def _next_recurring_time(self, event: ScheduledEvent, current_time: datetime) -> datetime:
        """Calculate the next future trigger time for a recurring event."""
        if event.repeat_every_days is None:
            return event.event_time

        interval = timedelta(days=event.repeat_every_days)
        next_time = event.event_time + interval
        while next_time <= current_time:
            next_time += interval
        return next_time

    def _event_from_model(self, reminder: ScheduledReminder) -> ScheduledEvent:
        """Convert a persisted reminder row into the scheduler event shape."""
        try:
            role_names = json.loads(reminder.role_names_json)
            if not isinstance(role_names, list):
                role_names = ["everyone"]
        except json.JSONDecodeError:
            role_names = ["everyone"]

        return ScheduledEvent(
            id=reminder.id,
            event_time=self._ensure_utc(reminder.reminder_time),
            role_names=[str(role_name) for role_name in role_names],
            message=reminder.message,
            repeat_every_days=reminder.repeat_every_days,
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
