import asyncio
from types import SimpleNamespace

from handlers.gift_code_handler import GiftCodeHandler


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

    async def add_registered_player(self, **kwargs):
        self.added_players.append(kwargs)
        return SimpleNamespace(**kwargs)

    async def add_registered_players(self, players):
        self.added_player_batches.append(players)
        self.added_players.extend(players)
        return [SimpleNamespace(**player) for player in players]


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


def build_handler(registry, kingshot, mightpulse=None):
    return GiftCodeHandler(
        gift_code_service=SimpleNamespace(),
        player_info_service=SimpleNamespace(),
        bot=SimpleNamespace(),
        config=SimpleNamespace(),
        interaction_tracking_service=FakeTrackingService(),
        player_registry_service=registry,
        kingshot_data_service=kingshot,
        mightpulse_service=mightpulse,
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
    assert registry.added_player_batches == []
    assert registry.added_players == []
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
            "added_by_user_id": 111,
            "player_uid": "30669791",
            "player_name": "Resolved Name",
            "kingdom": "830",
            "castle_level": "30",
            "enabled": True,
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
    assert registry.added_players == [
        {
            "player_id": "987654321",
            "added_by_user_id": 111,
            "player_uid": "30669792",
            "player_name": "Missing Cache Name",
            "kingdom": "830",
            "castle_level": "29",
            "enabled": True,
        },
    ]


def test_addalliance_reenables_cached_disabled_player():
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
            "added_by_user_id": 111,
            "player_uid": "30669791",
            "player_name": "Cached Name",
            "kingdom": "830",
            "castle_level": "30",
            "enabled": True,
        }
    ]


def test_addalliance_uses_external_governor_ids_only_for_current_roster_uids():
    class FakeMightPulse:
        def __init__(self):
            self.calls = []

        async def get_alliance_roster_by_aid(self, kid, aid):
            self.calls.append((kid, aid))
            return {
                "name": "Known Alliance",
                "members_by_uid": {
                    "30669791": {
                        "uid": 30669791,
                        "governor_id": 123456789,
                        "nick_name": "Known Governor",
                        "town_center_level": 30,
                        "kid": 830,
                    },
                    "99999999": {"uid": 99999999, "governor_id": 999999999, "kid": 830},
                },
            }

    provider = FakeMightPulse()
    registry = FakeRegistryService()
    kingshot = FakeKingshotDataService(
        alliance_payload={
            "aid": None,
            "name": None,
            "members": [{"uid": 30669791, "rank": 4}, {"uid": 30669792, "rank": 3}],
        }
    )
    handler = build_handler(registry, kingshot, provider)
    interaction = FakeInteraction()

    asyncio.run(handler._handle_add_alliance_slash(interaction, aid="83900009", kid=830))

    assert provider.calls == [(830, "83900009")]
    assert kingshot.player_calls == ["30669792"]
    assert registry.added_players == [{
        "player_id": "123456789",
        "added_by_user_id": 111,
        "player_uid": "30669791",
        "player_name": "Known Governor",
        "kingdom": "830",
        "castle_level": "30",
        "enabled": True,
    }]
    assert "Known Alliance" in interaction.followup.sent_embed.description
    assert "1 member(s) could not be mapped" in interaction.followup.sent_embed.fields[-1].value


def test_addalliance_reports_missing_governor_ids_without_claiming_success():
    registry = FakeRegistryService()
    kingshot = FakeKingshotDataService(
        alliance_payload={"members": [{"uid": 30669791, "rank": 4}]}
    )
    handler = build_handler(registry, kingshot)
    interaction = FakeInteraction()

    asyncio.run(handler._handle_add_alliance_slash(interaction, aid="83900009", kid=830))

    assert registry.added_players == []
    assert interaction.followup.sent_embed.title == "⚠️ No Governor IDs Available"
    assert "`/addplayer`" in interaction.followup.sent_embed.description


def test_addalliance_rejects_mismatched_alliance_payload():
    registry = FakeRegistryService()
    kingshot = FakeKingshotDataService(
        alliance_payload={"aid": 83900010, "members": [{"uid": 30669791, "fid": 123456789}]}
    )
    interaction = FakeInteraction()

    asyncio.run(build_handler(registry, kingshot)._handle_add_alliance_slash(
        interaction, aid="83900009", kid=830,
    ))

    assert registry.added_players == []
    assert "does not match" in interaction.followup.sent_embed.description
