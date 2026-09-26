import httpx
import pytest

from services.mightpulse_service import (
    MightPulseAllianceDirectory,
    MightPulseService,
    MightPulseUnavailableError,
)


@pytest.mark.asyncio
async def test_public_directory_returns_named_aids_for_requested_kingdom(monkeypatch):
    requests = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={
            "ok": True,
            "kid": 830,
            "alliances": [
                {"aid": 83900009, "kid": 830, "rank": 3, "power": 200,
                 "abbr": "FKA", "name": "FateKillsAll", "member_count": 93},
                {"aid": 83900004, "kid": 830, "rank": 1, "power": 300,
                 "abbr": "KOR", "name": "Serendipity", "member_count": 98},
                {"aid": 83900009, "kid": 831, "rank": 2, "power": 999,
                 "abbr": "BAD", "name": "Wrong kingdom"},
            ],
        })

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        "services.mightpulse_service.httpx.AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(handle), **kwargs),
    )

    result = await MightPulseAllianceDirectory(base_url="https://pulse.example").get_ranked_alliances(830)

    assert [(row["aid"], row["name"], row["abbr"]) for row in result["data"]] == [
        ("83900004", "Serendipity", "KOR"),
        ("83900009", "FateKillsAll", "FKA"),
    ]
    assert requests[0].url.path == "/api/kingdoms/830"
    assert dict(requests[0].url.params) == {"players": "1", "alliances": "100"}
    assert "X-Api-Key" not in requests[0].headers


@pytest.mark.asyncio
async def test_public_directory_rejects_mismatched_kingdom(monkeypatch):
    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        "services.mightpulse_service.httpx.AsyncClient",
        lambda **kwargs: original_client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, json={"ok": True, "kid": 831, "alliances": []})
            ),
            **kwargs,
        ),
    )

    with pytest.raises(MightPulseUnavailableError, match="invalid kingdom"):
        await MightPulseAllianceDirectory(base_url="https://pulse.example").get_ranked_alliances(830)


@pytest.mark.asyncio
async def test_resolves_governor_ids_through_verified_aid_and_roster(monkeypatch):
    requests = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/v1/kingdoms/830/ranks":
            return httpx.Response(200, json={"boards": [{"entries": [
                {"aid": 83900009, "abbr": "FKA", "name": "Fate Kills All"},
            ]}]})
        if request.url.path == "/v1/alliances/830/FKA":
            return httpx.Response(200, json={
                "alliance": {"aid": 83900009, "kid": 830, "name": "Fate Kills All"},
                "members": [
                    {"uid": 30669791, "governor_id": 123456789, "kid": 830},
                    {"uid": 30669792, "fid": 987654321, "kid": 830},
                    {"uid": 30669793, "governor_id": None, "kid": 830},
                    {"uid": 30669794, "governor_id": 444444444, "kid": 831},
                    {"uid": 30669795, "governor_id": 555555555, "fid": 666666666, "kid": 830},
                ],
            })
        raise AssertionError(request.url.path)

    original_client = httpx.AsyncClient

    def mocked_client(**kwargs):
        return original_client(transport=httpx.MockTransport(handle), **kwargs)

    monkeypatch.setattr("services.mightpulse_service.httpx.AsyncClient", mocked_client)
    service = MightPulseService("test-key", base_url="https://pulse.example")

    result = await service.get_alliance_roster_by_aid(830, "83900009")

    assert set(result["members_by_uid"]) == {"30669791", "30669792"}
    assert result["members_by_uid"]["30669791"]["governor_id"] == 123456789
    assert len(requests) == 2
    assert requests[0].headers["X-Api-Key"] == "test-key"
    assert dict(requests[0].url.params) == {"board": "alliance_power", "limit": "100"}
    assert dict(requests[1].url.params) == {"include": "info,roster"}


@pytest.mark.asyncio
async def test_rejects_mismatched_alliance_roster(monkeypatch):
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/kingdoms/830/ranks":
            return httpx.Response(200, json={"entries": [{"aid": 83900009, "abbr": "FKA"}]})
        return httpx.Response(200, json={
            "alliance": {"aid": 83900010, "kid": 830},
            "members": [{"uid": 30669791, "governor_id": 123456789}],
        })

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        "services.mightpulse_service.httpx.AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(handle), **kwargs),
    )

    with pytest.raises(MightPulseUnavailableError, match="identity"):
        await MightPulseService("test-key", base_url="https://pulse.example").get_alliance_roster_by_aid(830, 83900009)
