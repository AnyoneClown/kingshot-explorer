import asyncio
from datetime import date, datetime, timezone

import pytest

from services.kingshot_calendar_service import (
    CalendarUnavailableError,
    KingdomNotTrackedError,
    KingshotCalendarService,
    build_digest,
    build_week,
    filter_categories,
    merge_events,
    parse_event,
    parse_status,
    start_of_week,
)


def utc(day, hour=0, minute=0, month=11):
    return datetime(2026, month, day, hour, minute, tzinfo=timezone.utc)


def raw_event(title, kind, start, end, *, main=False, confidence="confirmed", phase="event", precision="time"):
    """An entry shaped like kingshot.net's: ``end`` is the inclusive last millisecond."""
    return {
        "titleKey": title.replace(" ", ""),
        "title": title,
        "type": kind,
        "isMainEvent": main,
        "startUtc": start,
        "endUtc": end,
        "event": title.lower().replace(" ", "-").replace("'", ""),
        "phase": phase,
        "precision": precision,
        "confidence": confidence,
    }


WEEK_ONE = [
    raw_event("Swordland Summit League", "WAR", "2026-10-30T00:00:00.000Z", "2026-12-14T23:59:59.999Z", main=True),
    raw_event("Alliance Brawl", "ALLIANCE", "2026-11-09T00:00:00.000Z", "2026-11-15T11:59:59.999Z"),
    raw_event("Custom Selection", "PACK", "2026-11-09T00:00:00.000Z", "2026-11-11T23:59:59.999Z"),
    raw_event("Sanctuary Battle", "WAR", "2026-11-10T00:00:00.000Z", "2026-11-13T23:59:59.999Z", main=True),
    raw_event("Champion's Way", "GROWTH", "2026-11-11T00:00:00.000Z", "2026-11-13T23:59:59.999Z"),
    raw_event("Officer Project", "GROWTH", "2026-11-09T00:00:00.000Z", "2026-11-11T23:59:59.999Z"),
    raw_event("Master's Collection", "PACK", "2026-11-11T00:00:00.000Z", "2026-11-12T23:59:59.999Z"),
    raw_event(
        "Mystic Divination", "DEALS", "2026-11-13T00:00:00.000Z", "2026-11-14T23:59:59.999Z", confidence="probable"
    ),
    raw_event("Conqueror Pack", "PACK", "2026-11-12T00:00:00.000Z", "2026-11-14T23:59:59.999Z"),
]
WEEK_TWO = [
    raw_event("Swordland Summit League", "WAR", "2026-10-30T00:00:00.000Z", "2026-12-14T23:59:59.999Z", main=True),
    raw_event("Golden Glaives", "GROWTH", "2026-11-16T00:00:00.000Z", "2026-11-17T23:59:59.999Z"),
    raw_event("Castle Battle", "WAR", "2026-11-21T12:00:00.000Z", "2026-11-21T16:59:59.999Z", main=True),
]


def payload(events, *, current_week=1):
    return {
        "timestamp": "2026-11-11T08:00:00.000Z",
        "kvk": {
            "phase": "countdown",
            "phaseName": "Next KvK",
            "icon": "⚔️",
            "timeLeft": {"days": 18, "total": 18 * 86_400_000 + 16 * 3_600_000},
            "eventNumber": 21,
        },
        "transfer": {
            "phase": "invitational",
            "phaseName": "Invitational",
            "icon": "✉️",
            "timeLeft": {"total": 40 * 3_600_000},
            "eventNumber": 9,
        },
        "calendar": {"kingdom": 795, "currentWeek": current_week, "events": events},
    }


def parsed(rows):
    return [parse_event(row) for row in rows]


def two_weeks():
    return merge_events(parsed(WEEK_ONE), parsed(WEEK_TWO))


class FakeCalendarService(KingshotCalendarService):
    """The real cache and merge logic over canned responses."""

    def __init__(self, responses, **kwargs):
        super().__init__(**kwargs)
        self.responses = responses
        self.calls = []

    async def _fetch(self, kingdom, week=None):
        self.calls.append((kingdom, week))
        response = self.responses[(kingdom, week)]
        if isinstance(response, Exception):
            raise response
        return response


# ---------------------------------------------------------------- parsing


def test_parse_event_turns_the_inclusive_end_into_a_boundary():
    event = parse_event(WEEK_ONE[3])

    assert event.title == "Sanctuary Battle"
    assert event.key == "sanctuary-battle"
    assert event.category == "WAR"
    assert event.is_main is True
    assert event.estimated is False
    assert event.start == utc(10)
    assert event.end == utc(14)

    half_day = parse_event(WEEK_ONE[1])
    assert half_day.end == utc(15, 12)


def test_parse_event_reads_estimates_battles_and_unknown_categories():
    estimated = parse_event(WEEK_ONE[7])
    battle = parse_event(
        raw_event(
            "Tri-Alliance Clash",
            "WAR",
            "2026-11-28T00:00:00.000Z",
            "2026-11-28T23:59:59.999Z",
            phase="battle",
            precision="day",
        )
    )
    unknown = parse_event(raw_event("Mystery", "FESTIVAL", "2026-11-28T00:00:00.000Z", "2026-11-28T23:59:59.999Z"))

    assert estimated.estimated is True
    assert battle.is_battle and battle.day_precision
    assert battle.display_title == "Tri-Alliance Clash battle"
    assert unknown.category == "SPECIAL"


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "text",
        {"title": "", "startUtc": "2026-11-28T00:00:00.000Z", "endUtc": "2026-11-28T23:59:59.999Z"},
        {"title": "No dates"},
        {"title": "Bad date", "startUtc": "soon", "endUtc": "2026-11-28T23:59:59.999Z"},
        {"title": "Backwards", "startUtc": "2026-11-28T00:00:00.000Z", "endUtc": "2026-11-27T00:00:00.000Z"},
    ],
)
def test_parse_event_skips_unusable_entries(raw):
    assert parse_event(raw) is None


def test_parse_status_computes_when_the_phase_changes():
    data = payload([])
    timestamp = utc(11, 8)

    kvk = parse_status(data["kvk"], "kvk", timestamp)
    transfer = parse_status(data["transfer"], "transfer", timestamp)

    assert (kvk.number, kvk.phase, kvk.target) == (21, "countdown", utc(30))
    assert (transfer.phase_name, transfer.target) == ("Invitational", utc(13))
    assert parse_status({"phase": "battle", "timeLeft": {"total": 0}}, "kvk", timestamp).target is None
    assert parse_status(None, "kvk", timestamp) is None
    assert parse_status({}, "kvk", timestamp) is None


def test_merge_events_drops_events_repeated_across_weeks():
    merged = merge_events(parsed(WEEK_ONE), parsed(WEEK_TWO))

    titles = [event.title for event in merged]
    assert titles.count("Swordland Summit League") == 1
    assert len(merged) == len(WEEK_ONE) + len(WEEK_TWO) - 1
    assert [event.start for event in merged] == sorted(event.start for event in merged)


# ---------------------------------------------------------------- service


def test_get_calendar_loads_this_week_and_the_next_and_caches_them():
    service = FakeCalendarService({(795, None): payload(WEEK_ONE), (795, 2): payload(WEEK_TWO)})

    async def run():
        first = await service.get_calendar(795)
        second = await service.get_calendar(795)
        return first, second

    first, second = asyncio.run(run())

    assert service.calls == [(795, None), (795, 2)]
    assert first is second
    assert first.kingdom == 795
    assert first.includes_next_week is True
    assert first.kvk.number == 21
    assert {event.title for event in first.events} >= {"Sanctuary Battle", "Castle Battle"}


def test_get_calendar_wraps_from_the_fourth_week_to_the_first():
    service = FakeCalendarService({(795, None): payload(WEEK_ONE, current_week=4), (795, 1): payload([])})

    asyncio.run(service.get_calendar(795))

    assert service.calls == [(795, None), (795, 1)]


def test_concurrent_requests_share_one_load():
    service = FakeCalendarService({(795, None): payload(WEEK_ONE), (795, 2): payload(WEEK_TWO)})

    async def run():
        return await asyncio.gather(*(service.get_calendar(795) for _ in range(5)))

    calendars = asyncio.run(run())

    assert service.calls == [(795, None), (795, 2)]
    assert all(calendar is calendars[0] for calendar in calendars)


def test_this_week_is_still_returned_when_next_week_fails():
    service = FakeCalendarService(
        {(795, None): payload(WEEK_ONE), (795, 2): CalendarUnavailableError("kingshot.net timed out")}
    )

    calendar = asyncio.run(service.get_calendar(795))

    assert calendar.includes_next_week is False
    assert len(calendar.events) == len(WEEK_ONE)


def test_unknown_and_invalid_kingdoms_are_reported():
    service = FakeCalendarService({(99999, None): KingdomNotTrackedError(99999)})

    with pytest.raises(KingdomNotTrackedError):
        asyncio.run(service.get_calendar(99999))
    for invalid in (0, -5, 100000, True, "795"):
        with pytest.raises(KingdomNotTrackedError):
            asyncio.run(service.get_calendar(invalid))
    assert service.calls == [(99999, None)]


def test_an_expired_calendar_is_served_while_the_site_is_down():
    service = FakeCalendarService(
        {(795, None): payload(WEEK_ONE), (795, 2): payload(WEEK_TWO)},
        cache_ttl_seconds=0.0,
    )

    async def run():
        fresh = await service.get_calendar(795)
        service.responses[(795, None)] = CalendarUnavailableError("kingshot.net answered with HTTP 502")
        return fresh, await service.get_calendar(795)

    fresh, stale = asyncio.run(run())

    assert stale is fresh


def test_an_outage_without_a_cached_calendar_is_an_error():
    service = FakeCalendarService({(795, None): CalendarUnavailableError("kingshot.net timed out")})

    with pytest.raises(CalendarUnavailableError):
        asyncio.run(service.get_calendar(795))


def test_a_response_without_a_calendar_is_an_error():
    service = FakeCalendarService({(795, None): {"timestamp": "2026-11-11T08:00:00.000Z"}})

    with pytest.raises(CalendarUnavailableError):
        asyncio.run(service.get_calendar(795))


def test_the_cache_keeps_a_bounded_number_of_kingdoms():
    responses = {}
    for kingdom in (1, 2, 3):
        responses[(kingdom, None)] = payload(WEEK_ONE)
        responses[(kingdom, 2)] = payload(WEEK_TWO)
    service = FakeCalendarService(responses, max_cached_kingdoms=2)

    async def run():
        for kingdom in (1, 2, 3):
            await service.get_calendar(kingdom)

    asyncio.run(run())

    assert set(service._cache) == {2, 3}


# ------------------------------------------------------------- view model


def test_digest_sorts_events_into_the_sections_of_the_day():
    digest = build_digest(parsed(WEEK_ONE), utc(11, 8))

    assert digest.day == date(2026, 11, 11)
    assert [event.title for event in digest.starting_today] == ["Champion's Way"]
    assert [event.title for event in digest.ending_today] == ["Officer Project"]
    assert [event.title for event in digest.running] == [
        "Sanctuary Battle",
        "Alliance Brawl",
        "Swordland Summit League",
    ]
    assert [(day, [event.title for event in events]) for day, events in digest.upcoming] == [
        (date(2026, 11, 13), ["Mystic Divination"])
    ]
    assert [event.title for event in digest.packs_starting] == ["Master's Collection"]
    assert [event.title for event in digest.packs_ending] == ["Custom Selection"]
    assert digest.packs_running == ()
    assert digest.is_empty is False


def test_digest_keeps_a_later_start_today_and_drops_what_is_over():
    events = parsed(WEEK_TWO)

    before = build_digest(events, utc(21, 8))
    after = build_digest(events, utc(21, 18))

    assert [event.title for event in before.starting_today] == ["Castle Battle"]
    assert after.starting_today == ()
    assert build_digest([], utc(21, 8)).is_empty is True


def test_digest_limits_how_far_ahead_it_looks():
    events = two_weeks()

    near = build_digest(events, utc(11, 8), upcoming_days=3)
    far = build_digest(events, utc(11, 8), upcoming_days=7)

    assert [day for day, _ in near.upcoming] == [date(2026, 11, 13)]
    assert [day for day, _ in far.upcoming] == [date(2026, 11, 13), date(2026, 11, 16)]


def test_week_groups_events_by_the_day_they_start():
    week = build_week(two_weeks(), utc(11, 8))

    assert week.start == utc(9)
    assert week.end == utc(16)
    assert [event.title for event in week.carried_over] == ["Swordland Summit League"]
    by_day = {day: [event.title for event in events] for day, events in week.days}
    assert list(by_day) == [date(2026, 11, day) for day in range(9, 16)]
    assert by_day[date(2026, 11, 9)] == ["Alliance Brawl", "Officer Project", "Custom Selection"]
    assert by_day[date(2026, 11, 10)] == ["Sanctuary Battle"]
    assert by_day[date(2026, 11, 14)] == []


def test_next_week_carries_over_what_is_still_running():
    week = build_week(two_weeks(), utc(18))

    assert week.start == utc(16)
    assert [event.title for event in week.carried_over] == ["Swordland Summit League"]
    assert [event.title for day, events in week.days for event in events] == ["Golden Glaives", "Castle Battle"]
    assert build_week([], utc(18)).is_empty is True


def test_start_of_week_is_monday_midnight_utc():
    assert start_of_week(utc(15, 23, 59)) == utc(9)
    assert start_of_week(utc(16)) == utc(16)


def test_filter_categories_keeps_only_the_selected_ones():
    events = parsed(WEEK_ONE)

    assert len(filter_categories(events, None)) == len(events)
    assert {event.category for event in filter_categories(events, {"WAR", "PACK"})} == {"WAR", "PACK"}
    assert filter_categories(events, set()) == []


# ---------------------------------------------------------------- artwork


class FakeIconService(KingshotCalendarService):
    def __init__(self, icons, **kwargs):
        super().__init__(**kwargs)
        self.icons = icons
        self.requested = []

    async def _fetch_icon(self, key):
        self.requested.append(key)
        return self.icons.get(key)


def test_event_icons_are_downloaded_once_and_misses_are_remembered():
    service = FakeIconService({"hero-rally": b"\x89PNG..."})

    async def run():
        first = await service.get_event_icons(["hero-rally", "no-such-event", "hero-rally"])
        second = await service.get_event_icons(["hero-rally", "no-such-event"])
        return first, second

    first, second = asyncio.run(run())

    assert first == second == {"hero-rally": b"\x89PNG..."}
    assert sorted(service.requested) == ["hero-rally", "no-such-event"]


def test_event_icon_misses_are_retried_later():
    service = FakeIconService({}, missing_icon_ttl_seconds=0.0)

    async def run():
        await service.get_event_icons(["new-event"])
        service.icons["new-event"] = b"\x89PNG..."
        return await service.get_event_icons(["new-event"])

    assert asyncio.run(run()) == {"new-event": b"\x89PNG..."}
    assert service.requested == ["new-event", "new-event"]


def test_event_icon_keys_are_validated_and_limited():
    service = FakeIconService({})

    async def run():
        await service.get_event_icons(["../secret", "Hero Rally", "", None, "a/b", "ok-key"])
        await service.get_event_icons([f"event-{index}" for index in range(30)], limit=5)

    asyncio.run(run())

    assert service.requested[0] == "ok-key"
    assert len(service.requested) == 6
