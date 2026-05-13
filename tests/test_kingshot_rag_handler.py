import asyncio
from types import SimpleNamespace

from handlers.kingshot_rag_handler import KingshotRAGHandler


class FakeResponse:
    def __init__(self):
        self.deferred = []

    async def defer(self, **kwargs):
        self.deferred.append(kwargs)


class FakeFollowup:
    def __init__(self):
        self.sent = []

    async def send(self, **kwargs):
        self.sent.append(kwargs)


class FakeInteraction:
    def __init__(self, *, is_admin=False):
        self.guild = SimpleNamespace(id=123)
        self.user = SimpleNamespace(guild_permissions=SimpleNamespace(administrator=is_admin))
        self.response = FakeResponse()
        self.followup = FakeFollowup()


class FakeRAGService:
    def __init__(self):
        self.answer_calls = []
        self.upsert_calls = []

    async def answer_question(self, question, **kwargs):
        self.answer_calls.append((question, kwargs))
        return "Use rally leaders and coordinate timing."

    async def upsert_knowledge(self, **kwargs):
        self.upsert_calls.append(kwargs)
        return SimpleNamespace(slug=kwargs["slug"])


def test_handle_ask_delegates_to_service_and_sends_embed():
    service = FakeRAGService()
    handler = KingshotRAGHandler(service, bot=SimpleNamespace())
    interaction = FakeInteraction()

    asyncio.run(handler._handle_ask(interaction, " How do rallies work? ", "event"))

    assert service.answer_calls == [("How do rallies work?", {"entity_type": "event"})]
    assert interaction.followup.sent[0]["embed"].title == "Kingshot Knowledge"


def test_handle_upsert_rejects_non_admin_user():
    service = FakeRAGService()
    handler = KingshotRAGHandler(service, bot=SimpleNamespace())
    interaction = FakeInteraction(is_admin=False)

    asyncio.run(
        handler._handle_upsert(
            interaction,
            "event",
            "bear-trap",
            "Bear Trap",
            "Use rallies.",
            None,
            None,
            None,
        )
    )

    assert service.upsert_calls == []
    assert interaction.followup.sent[0]["embed"].title == "Permission Denied"


def test_handle_upsert_rejects_invalid_json():
    service = FakeRAGService()
    handler = KingshotRAGHandler(service, bot=SimpleNamespace())
    interaction = FakeInteraction(is_admin=True)

    asyncio.run(
        handler._handle_upsert(
            interaction,
            "event",
            "bear-trap",
            "Bear Trap",
            "Use rallies.",
            "{bad",
            None,
            None,
        )
    )

    assert service.upsert_calls == []
    assert interaction.followup.sent[0]["embed"].title == "Invalid JSON"


def test_handle_upsert_delegates_valid_admin_payload():
    service = FakeRAGService()
    handler = KingshotRAGHandler(service, bot=SimpleNamespace())
    interaction = FakeInteraction(is_admin=True)

    asyncio.run(
        handler._handle_upsert(
            interaction,
            "event",
            "bear-trap",
            "Bear Trap",
            "Use rallies.",
            '{"difficulty":"daily"}',
            '{"phase":"prep"}',
            "manual",
        )
    )

    call = service.upsert_calls[0]
    assert call["entity_type"] == "event"
    assert call["slug"] == "bear-trap"
    assert call["data"] == {"difficulty": "daily"}
    assert call["chunks"][0].metadata == {"phase": "prep"}
    assert interaction.followup.sent[0]["embed"].title == "Kingshot Knowledge Saved"
