"""Kingdom event calendar from the kingshot.net public API.

``GET https://kingshot.net/api/events?kingdom=<n>&week=<1-4>`` returns the running or
next KvK and transfer, plus one week of a kingdom's event calendar. No API key is
needed; requests are limited per IP address (120 per window) and cached upstream for
60 seconds.

The service loads the current week and the one after it, merges them into one
``KingdomCalendar`` and caches it per kingdom, so a busy channel or a daily post for
many servers costs two requests per kingdom every few minutes at most.

The functions at the bottom turn a calendar into what a reader wants to know at a
given moment (what starts today, what ends today, what is coming). They are pure, so
the Discord layer only formats their result.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Optional

import aiohttp

logger = logging.getLogger(__name__)

API_BASE_URL = "https://kingshot.net/api"
# Event artwork: the file name is the API's ``event`` id.
EVENT_ICON_URL = "https://kingshot.net/images/events/{key}.png"
EVENT_KEY_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
MAX_ICON_BYTES = 512 * 1024
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_KINGDOM_NUMBER = 99999
WEEKS_IN_CYCLE = 4

CATEGORY_WAR = "WAR"
CATEGORY_ALLIANCE = "ALLIANCE"
CATEGORY_GROWTH = "GROWTH"
CATEGORY_DEALS = "DEALS"
CATEGORY_SPECIAL = "SPECIAL"
CATEGORY_PACK = "PACK"
# Display order, most important first.
CATEGORIES = (
    CATEGORY_WAR,
    CATEGORY_ALLIANCE,
    CATEGORY_GROWTH,
    CATEGORY_DEALS,
    CATEGORY_SPECIAL,
    CATEGORY_PACK,
)
_CATEGORY_ORDER = {name: index for index, name in enumerate(CATEGORIES)}


class CalendarError(Exception):
    """The calendar could not be loaded."""


class KingdomNotTrackedError(CalendarError):
    """kingshot.net has no calendar for this kingdom (or the number is invalid)."""

    def __init__(self, kingdom: int):
        super().__init__(f"Kingdom {kingdom} is not on the kingshot.net calendar")
        self.kingdom = kingdom


class CalendarUnavailableError(CalendarError):
    """kingshot.net did not answer, answered with an error, or sent unreadable data."""


@dataclass(frozen=True)
class CalendarEvent:
    """One entry of a kingdom's calendar. ``end`` is exclusive."""

    key: str
    title: str
    category: str
    start: datetime
    end: datetime
    is_main: bool = False
    # The date follows the usual rotation but is not confirmed for every kingdom yet.
    estimated: bool = False
    # "battle" marks the fighting window of a longer event (KvK, Tri-Alliance Clash).
    phase: str = "event"
    # Only the day is known, not the hour.
    day_precision: bool = False

    @property
    def is_pack(self) -> bool:
        return self.category == CATEGORY_PACK

    @property
    def is_battle(self) -> bool:
        return self.phase == "battle"

    @property
    def display_title(self) -> str:
        return f"{self.title} battle" if self.is_battle else self.title

    def is_active(self, now: datetime) -> bool:
        return self.start <= now < self.end

    def has_ended(self, now: datetime) -> bool:
        return self.end <= now


@dataclass(frozen=True)
class CalendarStatus:
    """The running or next KvK or transfer."""

    kind: str  # "kvk" or "transfer"
    phase: str
    phase_name: str
    number: Optional[int] = None
    icon: str = ""
    # When the current phase ends (for "countdown": when the next one starts).
    target: Optional[datetime] = None


@dataclass(frozen=True)
class KingdomCalendar:
    kingdom: int
    fetched_at: datetime
    current_week: int
    events: tuple[CalendarEvent, ...] = ()
    kvk: Optional[CalendarStatus] = None
    transfer: Optional[CalendarStatus] = None
    # False when the week after the current one could not be loaded.
    includes_next_week: bool = True


# --------------------------------------------------------------------- parsing


def _parse_time(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _exclusive_end(inclusive_end: datetime) -> datetime:
    """The API's ``endUtc`` is inclusive (``23:59:59.999``); round it up to the boundary."""
    if inclusive_end.second == 59:
        return (inclusive_end + timedelta(seconds=1)).replace(microsecond=0)
    return inclusive_end


def parse_event(raw: Any) -> Optional[CalendarEvent]:
    """Read one calendar entry; entries without a usable title or dates are skipped."""
    if not isinstance(raw, dict):
        return None
    title = str(raw.get("title") or "").strip()
    start = _parse_time(raw.get("startUtc"))
    end = _parse_time(raw.get("endUtc"))
    if not title or start is None or end is None:
        return None
    end = _exclusive_end(end)
    if end <= start:
        return None
    category = str(raw.get("type") or "").strip().upper()
    return CalendarEvent(
        key=str(raw.get("event") or raw.get("titleKey") or title).strip(),
        title=title,
        category=category if category in _CATEGORY_ORDER else CATEGORY_SPECIAL,
        start=start,
        end=end,
        is_main=raw.get("isMainEvent") is True,
        estimated=str(raw.get("confidence") or "").lower() == "probable",
        phase=str(raw.get("phase") or "event").lower(),
        day_precision=str(raw.get("precision") or "").lower() == "day",
    )


def parse_status(raw: Any, kind: str, timestamp: Optional[datetime]) -> Optional[CalendarStatus]:
    if not isinstance(raw, dict):
        return None
    phase = str(raw.get("phase") or "").strip().lower()
    if not phase:
        return None
    target = None
    time_left = raw.get("timeLeft")
    total = time_left.get("total") if isinstance(time_left, dict) else None
    if timestamp is not None and isinstance(total, (int, float)) and not isinstance(total, bool) and total > 0:
        target = (timestamp + timedelta(milliseconds=total)).replace(microsecond=0)
    number = raw.get("eventNumber")
    return CalendarStatus(
        kind=kind,
        phase=phase,
        phase_name=str(raw.get("phaseName") or "").strip(),
        number=number if isinstance(number, int) and not isinstance(number, bool) else None,
        icon=str(raw.get("icon") or "").strip(),
        target=target,
    )


def _sort_key(event: CalendarEvent):
    return (event.start, _CATEGORY_ORDER.get(event.category, len(CATEGORIES)), not event.is_main, event.title)


def merge_events(*groups: Iterable[CalendarEvent]) -> tuple[CalendarEvent, ...]:
    """Join weeks into one list: long events appear in every week they touch."""
    unique: dict[tuple, CalendarEvent] = {}
    for group in groups:
        for event in group:
            unique.setdefault((event.key, event.phase, event.start, event.end), event)
    return tuple(sorted(unique.values(), key=_sort_key))


# --------------------------------------------------------------------- service


@dataclass
class _CacheEntry:
    stored_at: float
    calendar: KingdomCalendar


class KingshotCalendarService:
    """Loads and caches kingdom calendars from kingshot.net."""

    def __init__(
        self,
        api_base_url: str = API_BASE_URL,
        *,
        cache_ttl_seconds: float = 600.0,
        stale_ttl_seconds: float = 6 * 3600.0,
        timeout_seconds: float = 10.0,
        max_cached_kingdoms: int = 256,
        max_concurrent_requests: int = 3,
        icon_url: str = EVENT_ICON_URL,
        missing_icon_ttl_seconds: float = 6 * 3600.0,
        max_cached_icons: int = 128,
    ):
        self._events_url = f"{api_base_url.rstrip('/')}/reminders"
        self._cache_ttl_seconds = cache_ttl_seconds
        self._stale_ttl_seconds = stale_ttl_seconds
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self._max_cached_kingdoms = max_cached_kingdoms
        self._cache: dict[int, _CacheEntry] = {}
        self._loading: dict[int, asyncio.Task] = {}
        self._limit = asyncio.Semaphore(max_concurrent_requests)
        self._session: Optional[aiohttp.ClientSession] = None
        self._icon_url = icon_url
        self._missing_icon_ttl_seconds = missing_icon_ttl_seconds
        self._max_cached_icons = max_cached_icons
        # key -> (stored at, PNG bytes, or None when the site has no such picture)
        self._icons: dict[str, tuple[float, Optional[bytes]]] = {}

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    @staticmethod
    def is_valid_kingdom(kingdom: Any) -> bool:
        return isinstance(kingdom, int) and not isinstance(kingdom, bool) and 1 <= kingdom <= MAX_KINGDOM_NUMBER

    async def get_calendar(self, kingdom: int) -> KingdomCalendar:
        """Return the kingdom's calendar for the current and the next week.

        Raises ``KingdomNotTrackedError`` for a kingdom kingshot.net does not know and
        ``CalendarUnavailableError`` when the site cannot be reached and nothing
        recent is cached.
        """
        if not self.is_valid_kingdom(kingdom):
            raise KingdomNotTrackedError(kingdom)
        entry = self._cache.get(kingdom)
        if entry is not None and time.monotonic() - entry.stored_at <= self._cache_ttl_seconds:
            return entry.calendar
        # Requests for the same kingdom share one load (a daily post for many servers,
        # or several members opening the calendar at once).
        task = self._loading.get(kingdom)
        if task is None:
            task = asyncio.create_task(self._load(kingdom))
            self._loading[kingdom] = task
            task.add_done_callback(lambda done, key=kingdom: self._finish_loading(key, done))
        try:
            return await asyncio.shield(task)
        except CalendarUnavailableError:
            # The calendar changes slowly: a few hours old is better than nothing.
            entry = self._cache.get(kingdom)
            if entry is not None and time.monotonic() - entry.stored_at <= self._stale_ttl_seconds:
                logger.warning("Serving a cached calendar for kingdom %s: kingshot.net is unavailable", kingdom)
                return entry.calendar
            raise

    def _finish_loading(self, kingdom: int, task: asyncio.Task) -> None:
        if self._loading.get(kingdom) is task:
            self._loading.pop(kingdom, None)
        if not task.cancelled():
            # Every waiter gets the exception from its own await; mark it retrieved so
            # a load nobody waited for does not log "exception was never retrieved".
            task.exception()

    async def _load(self, kingdom: int) -> KingdomCalendar:
        payload = await self._fetch(kingdom)
        calendar = payload.get("calendar")
        if not isinstance(calendar, dict):
            raise CalendarUnavailableError("kingshot.net sent a calendar in an unknown format")
        timestamp = _parse_time(payload.get("timestamp")) or datetime.now(timezone.utc)
        current_week = calendar.get("currentWeek")
        if not isinstance(current_week, int) or isinstance(current_week, bool):
            current_week = 1
        events = self._events(calendar)

        next_events: tuple[CalendarEvent, ...] = ()
        includes_next_week = True
        try:
            next_payload = await self._fetch(kingdom, week=current_week % WEEKS_IN_CYCLE + 1)
            next_events = self._events(next_payload.get("calendar"))
        except CalendarError:
            # This week alone is still useful; the views say what is missing.
            includes_next_week = False
            logger.warning("Could not load next week's calendar for kingdom %s", kingdom, exc_info=True)

        result = KingdomCalendar(
            kingdom=kingdom,
            fetched_at=timestamp,
            current_week=current_week,
            events=merge_events(events, next_events),
            kvk=parse_status(payload.get("kvk"), "kvk", timestamp),
            transfer=parse_status(payload.get("transfer"), "transfer", timestamp),
            includes_next_week=includes_next_week,
        )
        self._remember(result)
        return result

    @staticmethod
    def _events(calendar: Any) -> tuple[CalendarEvent, ...]:
        rows = calendar.get("events") if isinstance(calendar, dict) else None
        if not isinstance(rows, list):
            return ()
        return tuple(event for event in (parse_event(row) for row in rows) if event is not None)

    def _remember(self, calendar: KingdomCalendar) -> None:
        self._cache.pop(calendar.kingdom, None)
        while len(self._cache) >= self._max_cached_kingdoms:
            self._cache.pop(next(iter(self._cache)))
        self._cache[calendar.kingdom] = _CacheEntry(time.monotonic(), calendar)

    async def _fetch(self, kingdom: int, week: Optional[int] = None) -> dict:
        params = {"kingdom": str(kingdom)}
        if week is not None:
            params["week"] = str(week)
        try:
            async with self._limit:
                if self._session is None:
                    self._session = aiohttp.ClientSession()
                async with self._session.get(self._events_url, params=params, timeout=self._timeout) as response:
                    if response.status in (400, 404):
                        raise KingdomNotTrackedError(kingdom)
                    if response.status != 200:
                        raise CalendarUnavailableError(f"kingshot.net answered with HTTP {response.status}")
                    data = await response.json(content_type=None)
        except asyncio.TimeoutError as error:
            # ClientTimeout(total=...) raises a bare TimeoutError, not an aiohttp.ClientError.
            raise CalendarUnavailableError("kingshot.net timed out") from error
        except aiohttp.ClientError as error:
            raise CalendarUnavailableError(f"kingshot.net could not be reached: {error}") from error
        except ValueError as error:
            # Not JSON (for example an HTML error page served with status 200).
            raise CalendarUnavailableError("kingshot.net sent an unreadable calendar") from error
        if not isinstance(data, dict):
            raise CalendarUnavailableError("kingshot.net sent a calendar in an unknown format")
        return data


    # ---------------------------------------------------------------- artwork

    async def get_event_icons(self, keys: Iterable[str], *, limit: int = 12) -> dict[str, bytes]:
        """Download artwork for events that are not bundled with the bot.

        Returns only what could be loaded. Never raises: a card without a picture is
        better than no card. Results (and misses) are remembered, so a new event costs
        one request, not one per render.
        """
        wanted = list(dict.fromkeys(key for key in keys if EVENT_KEY_PATTERN.fullmatch(key or "")))[:limit]
        if not wanted:
            return {}
        loaded = await asyncio.gather(*(self._get_event_icon(key) for key in wanted), return_exceptions=True)
        return {key: data for key, data in zip(wanted, loaded) if isinstance(data, bytes)}

    async def _get_event_icon(self, key: str) -> Optional[bytes]:
        cached = self._icons.get(key)
        if cached is not None:
            stored_at, data = cached
            if data is not None or time.monotonic() - stored_at <= self._missing_icon_ttl_seconds:
                return data
        data = await self._fetch_icon(key)
        self._icons.pop(key, None)
        while len(self._icons) >= self._max_cached_icons:
            self._icons.pop(next(iter(self._icons)))
        self._icons[key] = (time.monotonic(), data)
        return data

    async def _fetch_icon(self, key: str) -> Optional[bytes]:
        try:
            async with self._limit:
                if self._session is None:
                    self._session = aiohttp.ClientSession()
                async with self._session.get(self._icon_url.format(key=key), timeout=self._timeout) as response:
                    if response.status != 200:
                        return None
                    data = b""
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        data += chunk
                        if len(data) > MAX_ICON_BYTES:
                            return None
        except (asyncio.TimeoutError, aiohttp.ClientError):
            logger.debug("Could not download artwork for event %s", key, exc_info=True)
            return None
        return data if data.startswith(_PNG_SIGNATURE) else None


# ------------------------------------------------------------------ view model


def start_of_day(moment: datetime) -> datetime:
    return moment.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)


def start_of_week(moment: datetime) -> datetime:
    """Monday 00:00 UTC of the game week ``moment`` falls in."""
    day = start_of_day(moment)
    return day - timedelta(days=day.weekday())


def filter_categories(events: Iterable[CalendarEvent], categories: Optional[Iterable[str]]) -> list[CalendarEvent]:
    if categories is None:
        return list(events)
    allowed = set(categories)
    return [event for event in events if event.category in allowed]


@dataclass(frozen=True)
class CalendarDigest:
    """What matters on one game day: new, ending, running and upcoming events."""

    day: date
    starting_today: tuple[CalendarEvent, ...] = ()
    ending_today: tuple[CalendarEvent, ...] = ()
    running: tuple[CalendarEvent, ...] = ()
    upcoming: tuple[tuple[date, tuple[CalendarEvent, ...]], ...] = ()
    # Packs are kept apart: there are many and most readers only skim them.
    packs_starting: tuple[CalendarEvent, ...] = ()
    packs_ending: tuple[CalendarEvent, ...] = ()
    packs_running: tuple[CalendarEvent, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not any(
            (
                self.starting_today,
                self.ending_today,
                self.running,
                self.upcoming,
                self.packs_starting,
                self.packs_ending,
                self.packs_running,
            )
        )


def build_digest(
    events: Iterable[CalendarEvent],
    now: datetime,
    *,
    upcoming_days: int = 3,
) -> CalendarDigest:
    """Sort events into the sections of the daily summary for the game day of ``now``."""
    day_start = start_of_day(now)
    day_end = day_start + timedelta(days=1)
    horizon = day_end + timedelta(days=upcoming_days)

    starting: list[CalendarEvent] = []
    ending: list[CalendarEvent] = []
    running: list[CalendarEvent] = []
    upcoming: dict[date, list[CalendarEvent]] = {}
    for event in events:
        if event.has_ended(now):
            continue
        if day_start <= event.start < day_end:
            starting.append(event)
        elif event.start >= day_end:
            # Packs would bury the events worth planning for.
            if event.start < horizon and not event.is_pack:
                upcoming.setdefault(event.start.date(), []).append(event)
        elif event.end <= day_end:
            ending.append(event)
        else:
            running.append(event)

    def split(items: list[CalendarEvent], key) -> tuple[tuple[CalendarEvent, ...], tuple[CalendarEvent, ...]]:
        ordered = sorted(items, key=key)
        return (
            tuple(event for event in ordered if not event.is_pack),
            tuple(event for event in ordered if event.is_pack),
        )

    def by_end(event: CalendarEvent):
        return (event.end, _CATEGORY_ORDER.get(event.category, len(CATEGORIES)), event.title)

    starting_events, starting_packs = split(starting, _sort_key)
    ending_events, ending_packs = split(ending, by_end)
    running_events, running_packs = split(running, by_end)
    return CalendarDigest(
        day=day_start.date(),
        starting_today=starting_events,
        ending_today=ending_events,
        running=running_events,
        upcoming=tuple((day, tuple(sorted(items, key=_sort_key))) for day, items in sorted(upcoming.items())),
        packs_starting=starting_packs,
        packs_ending=ending_packs,
        packs_running=running_packs,
    )


@dataclass(frozen=True)
class CalendarWeek:
    """One game week (Monday to Sunday, UTC) as a day-by-day agenda."""

    start: datetime
    # Events that began before this week and still run during it.
    carried_over: tuple[CalendarEvent, ...] = ()
    days: tuple[tuple[date, tuple[CalendarEvent, ...]], ...] = field(default_factory=tuple)

    @property
    def end(self) -> datetime:
        return self.start + timedelta(days=7)

    @property
    def is_empty(self) -> bool:
        return not self.carried_over and not any(events for _, events in self.days)


def build_week(events: Iterable[CalendarEvent], week_start: datetime) -> CalendarWeek:
    """Group events by the game day they start on within the week of ``week_start``."""
    week_start = start_of_week(week_start)
    week_end = week_start + timedelta(days=7)
    carried: list[CalendarEvent] = []
    by_day: dict[date, list[CalendarEvent]] = {
        (week_start + timedelta(days=offset)).date(): [] for offset in range(7)
    }
    for event in events:
        if event.end <= week_start or event.start >= week_end:
            continue
        if event.start < week_start:
            carried.append(event)
        else:
            by_day[event.start.date()].append(event)
    return CalendarWeek(
        start=week_start,
        carried_over=tuple(sorted(carried, key=lambda event: (event.end, event.title))),
        days=tuple((day, tuple(sorted(items, key=_sort_key))) for day, items in by_day.items()),
    )
