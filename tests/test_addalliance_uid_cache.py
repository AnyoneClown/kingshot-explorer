import asyncio
from types import SimpleNamespace

from handlers.gift_code_handler import GiftCodeHandler
from services.player_registry_service import RegistrationOutcome


class FakeResponse:
    def __init__(self):
        self.deferred = None

    async def defer(self, *, thinking=False, ephemeral=False):
        self.deferred = (thinking, ephemeral)


class FakeFollowup:
    def __init__(self):
        self.sent_embed = None

    async def send(self, *, embed=None):
        self.sent_embed = embed


class FakeInteraction:
    def __init__(self):
        self.guild_id = 999
        self.user = SimpleNamespace(
            id=111,
            name="tester",
            discriminator="0",
            display_name="Tester",
        )
        self.response = FakeResponse()
        self.followup = FakeFollowup()


class FakeTrackingService:
    def __init__(self):
        self.tracked_users = []

    async def track_user(self, **kwargs):
        self.tracked_users.append(kwargs)


class FakeRegistryService:
    def __init__(self, cached_by_uid=None):
        self.cached_by_uid = cached_by_uid or {}
        self.added_players = []
        self.added_player_batches = []

    async def get_registered_player_by_uid(self, player_uid):
        return self.cached_by_uid.get(str(player_uid))

    async def get_registered_players_by_uids(self, player_uids):
        return {
            str(player_uid): self.cached_by_uid[str(player_uid)]
            for player_uid in player_uids
            if str(player_uid) in self.cached_by_uid
        }

    async def register_players_for_guild(self, guild_id, profiles, **kwargs):
        assert guild_id == 999
        assert kwargs["added_by_user_id"] == 111
        assert kwargs["enable_existing"] is True
        self.registration_kwargs = kwargs
        self.added_player_batches.append(profiles)
        self.added_players.extend(profiles)
        return RegistrationOutcome(added=[profile["player_id"] for profile in profiles], roster_size=len(profiles))


class FakeKingshotDataService:
    def __init__(self, alliance_payload, profiles_by_uid=None):
        self.alliance_payload = alliance_payload
        self.profiles_by_uid = profiles_by_uid or {}
        self.player_calls = []
        self.full_alliance_calls = []

    async def get_alliance(self, aid, kid):
        return {"success": True, "data": self.alliance_payload}

    async def get_alliance_full(self, aid, kid):
        self.full_alliance_calls.append((aid, kid))
        full_members = []
        for member in self.alliance_payload.get("members", []):
            uid = str(member.get("uid"))
            full_member = dict(member)
            if uid in self.profiles_by_uid:
                full_member["player"] = self.profiles_by_uid[uid]
            full_members.append(full_member)
        return {"success": True, "data": {**self.alliance_payload, "members": full_members}}

    async def get_player(self, uid):
        self.player_calls.append(str(uid))
        profile = self.profiles_by_uid.get(str(uid))
        if profile is None:
            return {"success": True, "data": {"uid": uid, "fid": None}}
        return {"success": True, "data": profile}


def build_handler(registry, kingshot):
    return GiftCodeHandler(
        gift_code_service=SimpleNamespace(),
        player_info_service=SimpleNamespace(),
        bot=SimpleNamespace(),
        config=SimpleNamespace(max_players_per_guild=300),
        interaction_tracking_service=FakeTrackingService(),
        player_registry_service=registry,
        kingshot_data_service=kingshot,
    )


def test_addalliance_uses_cached_uid_without_resolving_profile():
    cached_player = SimpleNamespace(
        player_id="123456789",
        player_name="Cached Name",
        kingdom="830",
        castle_level="30",
        enabled=True,
    )
    registry = FakeRegistryService(cached_by_uid={"30669791": cached_player})
    kingshot = FakeKingshotDataService(
        alliance_payload={"name": "Test Alliance", "members": [{"uid": 30669791, "rank": 4}]}
    )
    handler = build_handler(registry, kingshot)
    interaction = FakeInteraction()

    asyncio.run(handler._handle_add_alliance_slash(interaction, aid="83900009", kid=830))

    assert kingshot.player_calls == []
    assert kingshot.full_alliance_calls == []
    # The cached fid is registered into this server's roster without an API lookup.
    assert registry.added_players == [
        {
            "player_id": "123456789",
            "player_uid": "30669791",
            "player_name": "Cached Name",
            "kingdom": "830",
            "castle_level": "30",
        }
    ]
    assert registry.registration_kwargs["max_total"] == 300
    assert "Added 1 Alliance Member" in interaction.followup.sent_embed.title


def test_addalliance_resolves_uncached_uid_and_stores_uid_mapping():
    registry = FakeRegistryService()
    kingshot = FakeKingshotDataService(
        alliance_payload={"name": "Test Alliance", "members": [{"uid": 30669791, "rank": 4}]},
        profiles_by_uid={
            "30669791": {
                "uid": 30669791,
                "fid": 123456789,
                "name": "Resolved Name",
                "kid": 830,
                "stove_lv": 30,
            }
        },
    )
    handler = build_handler(registry, kingshot)
    interaction = FakeInteraction()

    asyncio.run(handler._handle_add_alliance_slash(interaction, aid="83900009", kid=830))

    assert kingshot.player_calls == ["30669791"]
    assert kingshot.full_alliance_calls == []
    assert len(registry.added_player_batches) == 1
    assert registry.added_players == [
        {
            "player_id": "123456789",
            "player_uid": "30669791",
            "player_name": "Resolved Name",
            "kingdom": "830",
            "castle_level": "30",
        }
    ]
    assert "Added 1 Alliance Member" in interaction.followup.sent_embed.title


def test_addalliance_only_resolves_cache_misses():
    cached_player = SimpleNamespace(
        player_id="123456789",
        player_name="Cached Name",
        kingdom="830",
        castle_level="30",
        enabled=True,
    )
    registry = FakeRegistryService(cached_by_uid={"30669791": cached_player})
    kingshot = FakeKingshotDataService(
        alliance_payload={
            "name": "Test Alliance",
            "members": [
                {"uid": 30669791, "rank": 4},
                {"uid": 30669792, "rank": 3},
            ],
        },
        profiles_by_uid={
            "30669792": {
                "uid": 30669792,
                "fid": 987654321,
                "name": "Missing Cache Name",
                "kid": 830,
                "stove_lv": 29,
            }
        },
    )
    handler = build_handler(registry, kingshot)
    interaction = FakeInteraction()

    asyncio.run(handler._handle_add_alliance_slash(interaction, aid="83900009", kid=830))

    assert kingshot.player_calls == ["30669792"]
    assert kingshot.full_alliance_calls == []
    assert len(registry.added_player_batches) == 1
    assert [player["player_id"] for player in registry.added_players] == ["123456789", "987654321"]
    assert registry.added_players[1] == {
        "player_id": "987654321",
        "player_uid": "30669792",
        "player_name": "Missing Cache Name",
        "kingdom": "830",
        "castle_level": "29",
    }


def test_addalliance_registers_cached_disabled_player_for_this_server():
    cached_player = SimpleNamespace(
        player_id="123456789",
        player_name="Cached Name",
        kingdom="830",
        castle_level="30",
        enabled=False,
    )
    registry = FakeRegistryService(cached_by_uid={"30669791": cached_player})
    kingshot = FakeKingshotDataService(
        alliance_payload={"name": "Test Alliance", "members": [{"uid": 30669791, "rank": 4}]}
    )
    handler = build_handler(registry, kingshot)
    interaction = FakeInteraction()

    asyncio.run(handler._handle_add_alliance_slash(interaction, aid="83900009", kid=830))

    assert kingshot.player_calls == []
    assert kingshot.full_alliance_calls == []
    assert len(registry.added_player_batches) == 1
    assert registry.added_players == [
        {
            "player_id": "123456789",
            "player_uid": "30669791",
            "player_name": "Cached Name",
            "kingdom": "830",
            "castle_level": "30",
        }
    ]
