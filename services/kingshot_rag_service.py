"""Reusable Kingshot RAG service backed by CockroachDB VECTOR search."""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from typing import Any, Optional, Sequence

from openai import AsyncOpenAI
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import KingshotChunk, KingshotEntity
from db.session import DatabaseManager

logger = logging.getLogger(__name__)


class KingshotRAGError(Exception):
    """Raised when Kingshot RAG operations cannot be completed."""


@dataclass(frozen=True)
class KingshotChunkInput:
    """Input shape for a Kingshot knowledge chunk."""

    content: str
    metadata: Optional[dict[str, Any]] = None
    source: Optional[str] = None


@dataclass(frozen=True)
class KingshotRetrievedChunk:
    """Search result returned by the Kingshot RAG service."""

    chunk_id: str
    entity_id: str
    entity_type: str
    slug: str
    name: str
    entity_data: dict[str, Any]
    content: str
    metadata: dict[str, Any]
    source: Optional[str]
    distance: float


class KingshotRAGService:
    """Embed, store, retrieve, and answer Kingshot game knowledge."""

    EMBEDDING_DIMENSIONS = 2048

    def __init__(
        self,
        db_manager: DatabaseManager,
        client: AsyncOpenAI,
        *,
        embedding_model: str = "nvidia/llama-nemotron-embed-1b-v2",
        chat_model: str = "openai/gpt-oss-120b",
        max_answer_chars: int = 1800,
    ):
        self._db_manager = db_manager
        self._client = client
        self._embedding_model = embedding_model
        self._chat_model = chat_model
        self._max_answer_chars = max_answer_chars
        logger.info(
            "KingshotRAGService initialized with embedding model %s and chat model %s",
            embedding_model,
            chat_model,
        )

    async def embed_text(self, text_value: str, *, input_type: str = "passage") -> list[float]:
        """Generate a dense BGE-M3 embedding for text."""
        cleaned_text = self._clean_text(text_value)
        if not cleaned_text:
            raise KingshotRAGError("Cannot embed empty text")
        if input_type not in {"query", "passage"}:
            raise KingshotRAGError("Embedding input_type must be 'query' or 'passage'")

        logger.info(
            "Embedding Kingshot text: input_type=%s model=%s dimensions=%s text_chars=%s",
            input_type,
            self._embedding_model,
            self.EMBEDDING_DIMENSIONS,
            len(cleaned_text),
        )

        response = await self._client.embeddings.create(
            model=self._embedding_model,
            input=cleaned_text,
            dimensions=self.EMBEDDING_DIMENSIONS,
            extra_body={"input_type": input_type},
        )
        data = getattr(response, "data", None) or []
        if not data:
            raise KingshotRAGError("Embedding response did not include data")

        embedding = getattr(data[0], "embedding", None)
        if embedding is None and isinstance(data[0], dict):
            embedding = data[0].get("embedding")

        vector = self._validate_embedding(embedding)
        logger.info("Embedded Kingshot text: input_type=%s returned_dimensions=%s", input_type, len(vector))
        return vector

    async def upsert_entity(
        self,
        entity_type: str,
        slug: str,
        name: str,
        data: dict[str, Any],
        source: Optional[str] = None,
    ) -> KingshotEntity:
        """Create or update a structured Kingshot entity."""
        async with self._db_manager.session() as session:
            return await self._upsert_entity_in_session(session, entity_type, slug, name, data, source)

    async def upsert_knowledge(
        self,
        *,
        entity_type: str,
        slug: str,
        name: str,
        data: dict[str, Any],
        chunks: Sequence[KingshotChunkInput | dict[str, Any]],
        source: Optional[str] = None,
        replace_chunks: bool = True,
    ) -> KingshotEntity:
        """Create or update an entity and its embedded chunks."""
        normalized_chunks = self._normalize_chunks(chunks)
        if not normalized_chunks:
            raise KingshotRAGError("At least one knowledge chunk is required")

        logger.info(
            "Upserting Kingshot knowledge: entity_type=%s slug=%s name=%s chunks=%s replace_chunks=%s source=%s",
            entity_type,
            slug,
            name,
            len(normalized_chunks),
            replace_chunks,
            source,
        )

        async with self._db_manager.session() as session:
            entity = await self._upsert_entity_in_session(session, entity_type, slug, name, data, source)
            logger.info("Upserted Kingshot entity: id=%s slug=%s", entity.id, entity.slug)

            if replace_chunks:
                delete_result = await session.execute(delete(KingshotChunk).where(KingshotChunk.entity_id == entity.id))
                logger.info(
                    "Deleted existing Kingshot chunks for entity: entity_id=%s deleted=%s",
                    entity.id,
                    getattr(delete_result, "rowcount", "unknown"),
                )

            inserted_chunks = 0
            for index, chunk in enumerate(normalized_chunks, 1):
                logger.info(
                    "Embedding Kingshot chunk: entity_slug=%s chunk_index=%s content_chars=%s metadata_keys=%s",
                    slug,
                    index,
                    len(chunk.content),
                    sorted((chunk.metadata or {}).keys()),
                )
                embedding = await self.embed_text(chunk.content, input_type="passage")
                await session.execute(
                    text(
                        """
                        INSERT INTO kingshot_chunks (entity_id, content, embedding, metadata, source)
                        VALUES (
                            :entity_id,
                            :content,
                            CAST(:embedding AS VECTOR(2048)),
                            CAST(:metadata AS JSONB),
                            :source
                        )
                        """
                    ),
                    {
                        "entity_id": entity.id,
                        "content": chunk.content,
                        "embedding": self.format_vector_literal(embedding),
                        "metadata": json.dumps(chunk.metadata or {}),
                        "source": chunk.source if chunk.source is not None else source,
                    },
                )
                inserted_chunks += 1

            await session.flush()
            logger.info(
                "Finished Kingshot knowledge upsert: entity_id=%s slug=%s inserted_chunks=%s",
                entity.id,
                entity.slug,
                inserted_chunks,
            )
            return entity

    async def search(
        self,
        query: str,
        *,
        limit: int = 5,
        entity_type: Optional[str] = None,
    ) -> list[KingshotRetrievedChunk]:
        """Retrieve similar Kingshot chunks using exact cosine vector search."""
        cleaned_query = self._clean_text(query)
        if not cleaned_query:
            logger.info("Skipping empty Kingshot search query")
            return []

        safe_limit = max(1, min(int(limit), 20))
        logger.info(
            "Searching Kingshot knowledge: query=%r entity_type=%s limit=%s",
            self._truncate_for_log(cleaned_query),
            entity_type,
            safe_limit,
        )
        query_embedding = await self.embed_text(cleaned_query, input_type="query")
        params: dict[str, Any] = {
            "query_vector": self.format_vector_literal(query_embedding),
            "limit": safe_limit,
        }
        where_clause = ""
        if entity_type:
            where_clause = "WHERE e.entity_type = :entity_type"
            params["entity_type"] = entity_type.strip()

        sql = text(
            f"""
            SELECT
                c.id::STRING AS chunk_id,
                e.id::STRING AS entity_id,
                e.entity_type AS entity_type,
                e.slug AS slug,
                e.name AS name,
                e.data AS entity_data,
                c.content AS content,
                c.metadata AS metadata,
                c.source AS source,
                c.embedding <=> CAST(:query_vector AS VECTOR(2048)) AS distance
            FROM kingshot_chunks c
            JOIN kingshot_entities e ON e.id = c.entity_id
            {where_clause}
            ORDER BY c.embedding <=> CAST(:query_vector AS VECTOR(2048))
            LIMIT :limit
            """
        )

        async with self._db_manager.session() as session:
            result = await session.execute(sql, params)
            chunks = [self._retrieved_chunk_from_row(row) for row in result.mappings().all()]

        if not chunks:
            logger.warning(
                "Kingshot search returned no chunks: query=%r entity_type=%s",
                self._truncate_for_log(cleaned_query),
                entity_type,
            )
            return []

        logger.info(
            "Kingshot search returned %s chunks: %s",
            len(chunks),
            [
                {
                    "slug": chunk.slug,
                    "entity_type": chunk.entity_type,
                    "distance": round(chunk.distance, 6),
                    "content_chars": len(chunk.content),
                    "content_preview": self._truncate_for_log(chunk.content, 80),
                }
                for chunk in chunks
            ],
        )
        return chunks

    async def answer_question(
        self,
        question: str,
        *,
        limit: int = 5,
        entity_type: Optional[str] = None,
    ) -> str:
        """Answer a question using retrieved Kingshot context."""
        chunks = await self.search(question, limit=limit, entity_type=entity_type)
        if not chunks:
            logger.info("Kingshot answer skipped because no chunks were retrieved")
            return "I do not know based on the Kingshot knowledge base."

        context = self._build_context(chunks)
        logger.info(
            "Generating Kingshot answer: question=%r retrieved_chunks=%s context_chars=%s top_slug=%s top_distance=%.6f",
            self._truncate_for_log(self._clean_text(question)),
            len(chunks),
            len(context),
            chunks[0].slug,
            chunks[0].distance,
        )
        response = await self._client.chat.completions.create(
            model=self._chat_model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You answer Kingshot game questions using only the provided knowledge base context. "
                        "If the context contains a direct fact, answer from that fact even if the wording is brief. "
                        "If the context is genuinely unrelated or insufficient, say you do not know. "
                        "Keep answers concise and practical."
                    ),
                },
                {
                    "role": "user",
                    "content": f"Knowledge base context:\n{context}\n\nQuestion: {self._clean_text(question)}",
                },
            ],
            temperature=0.2,
            top_p=1,
            max_tokens=700,
        )

        answer = self._extract_completion_text(response).strip()
        if not answer:
            logger.warning("Kingshot chat model returned an empty answer")
            return "I do not know based on the Kingshot knowledge base."
        logger.info("Generated Kingshot answer: answer_chars=%s answer_preview=%r", len(answer), self._truncate_for_log(answer))
        return answer[: self._max_answer_chars]

    async def _upsert_entity_in_session(
        self,
        session: AsyncSession,
        entity_type: str,
        slug: str,
        name: str,
        data: dict[str, Any],
        source: Optional[str],
    ) -> KingshotEntity:
        entity_type = self._require_text(entity_type, "entity_type")
        slug = self._require_text(slug, "slug")
        name = self._require_text(name, "name")
        if not isinstance(data, dict):
            raise KingshotRAGError("Entity data must be a JSON object")

        result = await session.execute(select(KingshotEntity).where(KingshotEntity.slug == slug))
        entity = result.scalar_one_or_none()
        if entity:
            logger.info("Updating existing Kingshot entity: id=%s slug=%s", entity.id, slug)
            entity.entity_type = entity_type
            entity.name = name
            entity.data = data
            entity.source = source
        else:
            logger.info("Creating new Kingshot entity: entity_type=%s slug=%s name=%s", entity_type, slug, name)
            entity = KingshotEntity(entity_type=entity_type, slug=slug, name=name, data=data, source=source)
            session.add(entity)

        await session.flush()
        return entity

    def _normalize_chunks(self, chunks: Sequence[KingshotChunkInput | dict[str, Any]]) -> list[KingshotChunkInput]:
        normalized = []
        for chunk in chunks:
            if isinstance(chunk, KingshotChunkInput):
                item = chunk
            elif isinstance(chunk, dict):
                item = KingshotChunkInput(
                    content=str(chunk.get("content", "")),
                    metadata=chunk.get("metadata"),
                    source=chunk.get("source"),
                )
            else:
                raise KingshotRAGError("Chunks must be KingshotChunkInput objects or dictionaries")

            content = self._clean_text(item.content)
            if not content:
                raise KingshotRAGError("Chunk content cannot be empty")
            if item.metadata is not None and not isinstance(item.metadata, dict):
                raise KingshotRAGError("Chunk metadata must be a JSON object")
            normalized.append(KingshotChunkInput(content=content, metadata=item.metadata or {}, source=item.source))
        return normalized

    def _retrieved_chunk_from_row(self, row: Any) -> KingshotRetrievedChunk:
        entity_data = row["entity_data"] or {}
        if isinstance(entity_data, str):
            try:
                entity_data = json.loads(entity_data)
            except json.JSONDecodeError:
                entity_data = {}
        if not isinstance(entity_data, dict):
            entity_data = {}

        metadata = row["metadata"] or {}
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except json.JSONDecodeError:
                metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}

        return KingshotRetrievedChunk(
            chunk_id=str(row["chunk_id"]),
            entity_id=str(row["entity_id"]),
            entity_type=str(row["entity_type"]),
            slug=str(row["slug"]),
            name=str(row["name"]),
            entity_data=entity_data,
            content=str(row["content"]),
            metadata=metadata,
            source=row["source"],
            distance=float(row["distance"]),
        )

    def _build_context(self, chunks: Sequence[KingshotRetrievedChunk]) -> str:
        blocks = []
        for index, chunk in enumerate(chunks, 1):
            source = f" source={chunk.source}" if chunk.source else ""
            data = f"\nStructured data: {json.dumps(chunk.entity_data, sort_keys=True)}" if chunk.entity_data else ""
            metadata = f"\nChunk metadata: {json.dumps(chunk.metadata, sort_keys=True)}" if chunk.metadata else ""
            blocks.append(
                f"[{index}] {chunk.name} ({chunk.entity_type}/{chunk.slug}{source})\n"
                f"Content: {chunk.content}"
                f"{data}"
                f"{metadata}"
            )
        return "\n\n".join(blocks)

    def _extract_completion_text(self, response: Any) -> str:
        choices = getattr(response, "choices", None) or []
        if not choices:
            return ""
        content = choices[0].message.content
        if isinstance(content, list):
            return "".join(part.get("text", "") for part in content if isinstance(part, dict))
        return content or ""

    @classmethod
    def format_vector_literal(cls, embedding: Sequence[float]) -> str:
        """Format a validated embedding as a CockroachDB VECTOR literal."""
        vector = cls._validate_embedding(embedding)
        return "[" + ",".join(f"{value:.12g}" for value in vector) + "]"

    @classmethod
    def _validate_embedding(cls, embedding: Any) -> list[float]:
        if not isinstance(embedding, Sequence) or isinstance(embedding, (str, bytes)):
            raise KingshotRAGError("Embedding must be a sequence of floats")

        vector = []
        for value in embedding:
            if not isinstance(value, (int, float)):
                raise KingshotRAGError("Embedding values must be numeric")
            float_value = float(value)
            if not math.isfinite(float_value):
                raise KingshotRAGError("Embedding values must be finite")
            vector.append(float_value)

        if len(vector) != cls.EMBEDDING_DIMENSIONS:
            raise KingshotRAGError(
                f"Embedding must have {cls.EMBEDDING_DIMENSIONS} dimensions, got {len(vector)}"
            )
        return vector

    @staticmethod
    def _clean_text(text_value: str) -> str:
        return " ".join(str(text_value or "").split())

    @staticmethod
    def _truncate_for_log(text_value: str, limit: int = 180) -> str:
        if len(text_value) <= limit:
            return text_value
        return text_value[: limit - 3] + "..."

    @classmethod
    def _require_text(cls, text_value: str, field_name: str) -> str:
        cleaned = cls._clean_text(text_value)
        if not cleaned:
            raise KingshotRAGError(f"{field_name} is required")
        return cleaned
