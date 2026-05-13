import asyncio
from types import SimpleNamespace

import pytest

from services.kingshot_rag_service import KingshotRAGError, KingshotRAGService


def embedding():
    return [0.1] * 2048


class FakeEmbeddings:
    def __init__(self):
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(data=[SimpleNamespace(embedding=embedding())])


class FakeCompletions:
    def __init__(self, content="Use infantry heroes for this rally."):
        self.content = content
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=self.content))])


class FakeClient:
    def __init__(self):
        self.embeddings = FakeEmbeddings()
        self.chat = SimpleNamespace(completions=FakeCompletions())


class FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows


class FakeSession:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    async def execute(self, statement, params=None):
        self.calls.append((str(statement), params or {}))
        return FakeResult(self.rows)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class FakeDB:
    def __init__(self, rows):
        self.session_obj = FakeSession(rows)

    def session(self):
        return self.session_obj


def test_format_vector_literal_validates_dimension():
    literal = KingshotRAGService.format_vector_literal(embedding())

    assert literal.startswith("[0.1,0.1")
    assert literal.endswith("]")


def test_format_vector_literal_rejects_wrong_dimension():
    with pytest.raises(KingshotRAGError, match="2048 dimensions"):
        KingshotRAGService.format_vector_literal([0.1, 0.2])


def test_embed_text_uses_bge_m3_and_validates_response():
    client = FakeClient()
    service = KingshotRAGService(FakeDB([]), client)

    result = asyncio.run(service.embed_text(" bear trap "))

    assert len(result) == 2048
    assert client.embeddings.calls[0]["model"] == "nvidia/llama-nemotron-embed-1b-v2"
    assert client.embeddings.calls[0]["input"] == "bear trap"
    assert client.embeddings.calls[0]["dimensions"] == 2048
    assert client.embeddings.calls[0]["extra_body"] == {"input_type": "passage"}


def test_search_uses_cosine_operator_and_entity_type_filter():
    row = {
        "chunk_id": "chunk-1",
        "entity_id": "entity-1",
        "entity_type": "event",
        "slug": "bear-trap",
        "name": "Bear Trap",
        "entity_data": {"category": "alliance_event"},
        "content": "Use rallies and coordinate march timing.",
        "metadata": {"phase": "prep"},
        "source": "manual",
        "distance": 0.12,
    }
    db = FakeDB([row])
    service = KingshotRAGService(db, FakeClient())

    results = asyncio.run(service.search("how do bear trap rallies work?", entity_type="event", limit=3))

    sql, params = db.session_obj.calls[0]
    assert " <=> " in sql
    assert "WHERE e.entity_type = :entity_type" in sql
    assert "CAST(:query_vector AS VECTOR(2048))" in sql
    assert params["entity_type"] == "event"
    assert params["limit"] == 3
    assert service._client.embeddings.calls[0]["extra_body"] == {"input_type": "query"}
    assert results[0].slug == "bear-trap"
    assert results[0].metadata == {"phase": "prep"}


def test_answer_question_includes_retrieved_context_in_chat_prompt():
    row = {
        "chunk_id": "chunk-1",
        "entity_id": "entity-1",
        "entity_type": "event",
        "slug": "bear-trap",
        "name": "Bear Trap",
        "entity_data": {"best_practice": "rally together"},
        "content": "Bear Trap rewards improve when alliance members rally together.",
        "metadata": {},
        "source": "manual",
        "distance": 0.1,
    }
    client = FakeClient()
    service = KingshotRAGService(FakeDB([row]), client)

    answer = asyncio.run(service.answer_question("How should we do Bear Trap?"))

    prompt = client.chat.completions.calls[0]["messages"][1]["content"]
    assert answer == "Use infantry heroes for this rally."
    assert "Bear Trap rewards improve" in prompt
    assert '"best_practice": "rally together"' in prompt
    assert "How should we do Bear Trap?" in prompt
