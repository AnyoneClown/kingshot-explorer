import asyncio
from types import SimpleNamespace

from services.kvk_dossier_service import KvKDossierService

CASTLE_555 = {
    "kid": 555, "owner_kid": 555, "foreign_owner": False, "castle_name": "MAFIA",
    "king": {"uid": 21473081, "name": "Nyan", "abbr": "TMN", "aname": "555MAFIA", "home_kid": 555},
    "count": 22,
    "history": [
        {"uid": 1, "name": "Old", "abbr": "OLD", "home_kid": 555, "ts": 1700000000},
        {"uid": 21242190, "name": "Zero", "abbr": "J4F", "home_kid": 556, "ts": 1762626159,
         "is_cross_mode": True, "is_invader": True},
        {"uid": 2, "name": "Mid", "abbr": "MID", "home_kid": 555, "ts": 1750000000},
        {"uid": 21473081, "name": "Nyan", "abbr": "TMN", "home_kid": 555, "ts": 1780759657},
    ],
    "kings": [
        {"uid": 18635742, "name": "Bunny", "home_kid": 481, "king_terms": 1, "high_king_terms": 1, "invader_terms": 1},
        {"uid": 21473081, "name": "Nyan", "abbr": "TMN", "home_kid": 555, "king_terms": 18, "high_king_terms": 3,
         "power": 1230074433},
    ],
    "kvk": {"aid": 56400075, "victories": 5, "opponents": [
        {"opponent_kid": 565, "ts": 1779549621}, {"opponent_kid": 552, "ts": 1760205601},
    ]},
}


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class FakeData:
    def __init__(self):
        self.calls = []
        self.castle_results = {}

    async def get_kingdom_castle(self, kid, **kwargs):
        self.calls.append(("castle", kid, kwargs))
        result = self.castle_results.get(kid)
        if isinstance(result, list):
            return result.pop(0)
        if result is not None:
            return result
        payload = dict(CASTLE_555, kid=kid)
        if kid == 831:
            payload.update(owner_kid=845, foreign_owner=True)
        return {"success": True, "data": payload}

    async def get_ranked_alliances(self, kid, limit, *, exhaustive=False):
        self.calls.append(("alliances", kid, limit, exhaustive))
        return {"success": True, "data": [
            {"aid": 7, "abbr": "TOP", "name": "Top Alliance", "power": 12_300_000_000, "member_count": 100, "rank": 1},
        ]}

    async def get_kingdom_board(self, board_type, kid, limit=100, resolve=False):
        self.calls.append(("board", board_type, kid, limit, resolve))
        return {"success": True, "data": {"entries": [
            {"rank": 2, "uid": 9, "score": 50, "name": "Second", "alliance": {"abbr": "TOP"}},
            {"rank": 1, "uid": 8, "score": 90, "name": "First"},
        ]}}


def _kvk(history_ok=True):
    summary = {"matchCount": 3, "wins": 2, "losses": 1, "prepWins": 1, "prepLosses": 2, "currentStreak": "W2",
               "history": [{"season_id": 13, "opponent": 892, "castleResult": "win", "prepResult": "loss"}]}
    calls = []

    async def get_kingdom_stats(kid):
        calls.append(("stats", kid))
        return {"success": history_ok, "data": summary, "message": "down"}

    async def get_head_to_head(kingdom_a, kingdom_b):
        calls.append(("h2h", kingdom_a, kingdom_b))
        return {"success": True, "data": {
            "matches": [{"season_id": 9, "opponent": kingdom_b, "castleResult": "loss", "prepResult": "win"}],
            "kingdom_a": {"matchCount": 1, "wins": 0, "losses": 1, "prepWins": 1, "prepLosses": 0},
            "kingdom_b": {"matchCount": 1, "wins": 1, "losses": 0, "prepWins": 0, "prepLosses": 1},
        }}

    return SimpleNamespace(get_kingdom_stats=get_kingdom_stats, get_head_to_head=get_head_to_head, calls=calls)


def test_dossier_assembles_both_kingdoms_and_head_to_head():
    data, kvk = FakeData(), _kvk()
    service = KvKDossierService(data, kvk, clock=FakeClock())

    dossier = asyncio.run(service.build_dossier(830, 831))

    a, b = dossier["kingdom_a"], dossier["kingdom_b"]
    castle = a["castle"]["data"]
    assert a["kid"] == 830 and castle["foreign_owner"] is False and castle["owner_kid"] == 555
    assert castle["king"] == {"name": "Nyan", "abbr": "TMN", "alliance_name": "555MAFIA", "home_kid": 555,
                              "is_foreign": True}
    assert [reign["name"] for reign in castle["recent_reigns"]] == ["Nyan", "Zero", "Mid"]
    assert castle["recent_reigns"][1]["is_cross_mode"] and castle["recent_reigns"][1]["is_invader"]
    assert [king["name"] for king in castle["top_kings"]] == ["Nyan", "Bunny"]
    assert castle["kvk"] == {"victories": 5, "last_opponent_kid": 565, "last_ts": 1779549621}
    assert b["castle"]["data"]["foreign_owner"] is True and b["castle"]["data"]["owner_kid"] == 845
    assert a["alliances"]["data"][0]["abbr"] == "TOP"
    assert [row["name"] for row in a["top_power"]["data"]] == ["First", "Second"]
    assert a["top_power"]["data"][1]["abbr"] == "TOP"
    assert a["kvk_history"]["data"]["wins"] == 2 and len(a["kvk_history"]["data"]["recent"]) == 1
    assert dossier["head_to_head"]["data"]["match_count"] == 1
    assert ("castle", 830, {"kvk": True, "resolve": True}) in data.calls
    assert ("alliances", 830, 5, True) in data.calls
    assert ("board", 3, 830, 5, True) in data.calls and ("board", 4, 831, 5, True) in data.calls
    assert ("h2h", 830, 831) in kvk.calls


def test_full_dossier_is_cached_for_ten_minutes():
    clock, data, kvk = FakeClock(), FakeData(), _kvk()
    service = KvKDossierService(data, kvk, clock=clock)

    asyncio.run(service.build_dossier(830, 831))
    assert (len(data.calls), len(kvk.calls)) == (8, 3)  # 4 Data API reads per kingdom; 2 stats + 1 h2h
    clock.now += 599
    asyncio.run(service.build_dossier(830, 831))
    assert (len(data.calls), len(kvk.calls)) == (8, 3)
    asyncio.run(service.build_dossier(831, 830))  # kingdoms cached; only the new ordered pair is fetched
    assert (len(data.calls), len(kvk.calls)) == (8, 4)
    clock.now += 2
    asyncio.run(service.build_dossier(830, 831))
    assert (len(data.calls), len(kvk.calls)) == (16, 7)


def test_partial_failures_become_unavailable_sections_and_retry_sooner():
    clock, data, kvk = FakeClock(), FakeData(), _kvk(history_ok=False)
    data.castle_results[830] = {"success": False, "status_code": 503, "error_code": "GATEWAY_ERROR"}

    async def broken_alliances(kid, limit, *, exhaustive=False):
        raise RuntimeError("boom")

    data.get_ranked_alliances = broken_alliances
    service = KvKDossierService(data, kvk, clock=clock)

    intel = asyncio.run(service.get_kingdom_intel(830))

    assert intel["castle"] == {"ok": False, "data": None, "error": "game gateway error"}
    assert intel["alliances"] == {"ok": False, "data": None, "error": "unexpected error"}
    assert intel["kvk_history"]["ok"] is False
    assert intel["top_power"]["ok"] and intel["top_kills"]["ok"]
    castle_calls = [call for call in data.calls if call[0] == "castle"]
    assert len(castle_calls) == 1  # 503 is not retried without kvk
    clock.now += service.PARTIAL_CACHE_TTL_SECONDS - 1
    asyncio.run(service.get_kingdom_intel(830))
    assert len([call for call in data.calls if call[0] == "castle"]) == 1
    clock.now += 2
    asyncio.run(service.get_kingdom_intel(830))
    assert len([call for call in data.calls if call[0] == "castle"]) == 2


def test_castle_falls_back_to_plain_snapshot_when_kvk_lookup_fails():
    data, kvk = FakeData(), _kvk()
    data.castle_results[830] = [
        {"success": False, "status_code": 502, "error_code": "GATEWAY_ERROR"},
        {"success": True, "data": dict(CASTLE_555, kvk=None)},
    ]
    service = KvKDossierService(data, kvk, clock=FakeClock())

    intel = asyncio.run(service.get_kingdom_intel(830))

    castle_calls = [call for call in data.calls if call[0] == "castle"]
    assert castle_calls == [("castle", 830, {"kvk": True, "resolve": True}), ("castle", 830, {})]
    assert intel["castle"]["ok"] and intel["castle"]["data"]["kvk_included"] is False
    assert intel["castle"]["data"]["kvk"] is None


def test_concurrent_requests_share_one_load_and_slow_sources_time_out():
    data, kvk = FakeData(), _kvk()
    release = asyncio.Event()

    async def hanging_board(board_type, kid, limit=100, resolve=False):
        data.calls.append(("board", board_type, kid, limit, resolve))
        await release.wait()

    data.get_kingdom_board = hanging_board
    service = KvKDossierService(data, kvk, clock=FakeClock())
    service.SOURCE_TIMEOUT_SECONDS = 0.05

    async def check():
        first, second = await asyncio.gather(service.get_kingdom_intel(830), service.get_kingdom_intel(830))
        release.set()
        return first, second

    first, second = asyncio.run(check())

    assert first is second
    assert len([call for call in data.calls if call[0] == "castle"]) == 1
    assert first["top_power"] == {"ok": False, "data": None, "error": "timed out"}
    assert first["castle"]["ok"]


def test_partial_alliance_list_is_not_cached_as_complete():
    from services.kvk_dossier_service import KvKDossierService

    class Data:
        def __init__(self):
            self.alliance_calls = 0

        async def get_kingdom_castle(self, kid, **kwargs):
            return {"success": True, "data": {}}

        async def get_ranked_alliances(self, kid, limit, **kwargs):
            self.alliance_calls += 1
            return {"success": True, "partial": True, "data": [{"aid": 1, "abbr": "AAA", "name": "A", "power": 1}]}

        async def get_kingdom_board(self, board_type, kid, **kwargs):
            return {"success": True, "data": {"entries": []}}

    class Kvk:
        async def get_kingdom_stats(self, kid):
            return {"matchCount": 0, "history": []}

        async def get_head_to_head(self, a, b):
            return {"matches": []}

    data = Data()
    now = [1000.0]
    service = KvKDossierService(data, Kvk())
    service._clock = lambda: now[0]

    async def run():
        first = await service.get_kingdom_intel(830)
        now[0] += KvKDossierService.PARTIAL_CACHE_TTL_SECONDS + 1
        await service.get_kingdom_intel(830)
        return first

    first = asyncio.run(run())
    assert first["alliances"]["partial"] is True
    # Partial results get the short TTL, so the second call (after 61 s) reloads.
    assert data.alliance_calls == 2
