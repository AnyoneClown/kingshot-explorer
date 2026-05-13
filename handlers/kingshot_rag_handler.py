"""Discord commands for Kingshot RAG knowledge lookup and ingestion."""

from __future__ import annotations

import json
import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from handlers.ui import EmbedColors, build_status_embed
from services.kingshot_rag_service import KingshotChunkInput, KingshotRAGError, KingshotRAGService

logger = logging.getLogger(__name__)


class KingshotRAGHandler:
    """Handles Kingshot knowledge-base Discord commands."""

    def __init__(self, rag_service: KingshotRAGService, bot: commands.Bot):
        self._rag_service = rag_service
        self._bot = bot
        logger.info("KingshotRAGHandler initialized")

    def register_commands(self):
        """Register Kingshot RAG slash commands."""

        @self._bot.tree.command(name="kingshot_ask", description="Ask a question using the Kingshot knowledge base")
        @app_commands.describe(
            question="Question to answer from stored Kingshot knowledge",
            entity_type="Optional entity type filter, for example hero, event, building",
        )
        async def kingshot_ask(
            interaction: discord.Interaction,
            question: str,
            entity_type: Optional[str] = None,
        ):
            await self._handle_ask(interaction, question, entity_type)

        @self._bot.tree.command(name="kingshot_upsert", description="Admin: add or update one Kingshot knowledge item")
        @app_commands.describe(
            entity_type="Knowledge type, for example hero, event, building",
            slug="Stable unique key, for example bear-trap",
            name="Human-readable name",
            content="Knowledge text to embed",
            data_json="Optional JSON object for structured data",
            metadata_json="Optional JSON object for chunk metadata",
            source="Optional source label or URL",
        )
        async def kingshot_upsert(
            interaction: discord.Interaction,
            entity_type: str,
            slug: str,
            name: str,
            content: str,
            data_json: Optional[str] = None,
            metadata_json: Optional[str] = None,
            source: Optional[str] = None,
        ):
            await self._handle_upsert(interaction, entity_type, slug, name, content, data_json, metadata_json, source)

    async def _handle_ask(
        self,
        interaction: discord.Interaction,
        question: str,
        entity_type: Optional[str] = None,
    ) -> None:
        await interaction.response.defer(thinking=True)

        cleaned_question = self._clean_text(question)
        if not cleaned_question:
            await interaction.followup.send(
                embed=build_status_embed(
                    title="Question Required",
                    description="Please provide a Kingshot question.",
                    color=EmbedColors.WARNING,
                )
            )
            return

        try:
            logger.info(
                "Kingshot ask command received: user_id=%s guild_id=%s channel_id=%s entity_type=%s question=%r",
                getattr(interaction.user, "id", None),
                getattr(interaction.guild, "id", None),
                getattr(getattr(interaction, "channel", None), "id", None),
                self._clean_optional_text(entity_type),
                self._truncate(cleaned_question, 180),
            )
            answer = await self._rag_service.answer_question(
                cleaned_question,
                entity_type=self._clean_optional_text(entity_type),
            )
            embed = build_status_embed(
                title="Kingshot Knowledge",
                description=self._truncate(answer, 1900),
                color=EmbedColors.INFO,
                footer="Answered from stored Kingshot knowledge",
            )
            embed.add_field(name="Question", value=self._truncate(cleaned_question, 900), inline=False)
            await interaction.followup.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except Exception as exc:
            logger.error("Kingshot RAG answer failed: %s", exc, exc_info=True)
            await interaction.followup.send(
                embed=build_status_embed(
                    title="Knowledge Lookup Failed",
                    description="The Kingshot knowledge lookup failed. Try again in a moment.",
                    color=EmbedColors.ERROR,
                )
            )

    async def _handle_upsert(
        self,
        interaction: discord.Interaction,
        entity_type: str,
        slug: str,
        name: str,
        content: str,
        data_json: Optional[str],
        metadata_json: Optional[str],
        source: Optional[str],
    ) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)

        if not self._is_admin(interaction):
            logger.warning(
                "Rejected Kingshot upsert from non-admin: user_id=%s guild_id=%s slug=%s",
                getattr(interaction.user, "id", None),
                getattr(interaction.guild, "id", None),
                slug,
            )
            await interaction.followup.send(
                embed=build_status_embed(
                    title="Permission Denied",
                    description="Only server administrators can update Kingshot knowledge.",
                    color=EmbedColors.WARNING,
                ),
                ephemeral=True,
            )
            return

        try:
            data = self._parse_json_object(data_json, default={})
            metadata = self._parse_json_object(metadata_json, default={})
        except ValueError as exc:
            logger.warning(
                "Rejected Kingshot upsert due to invalid JSON: user_id=%s slug=%s error=%s",
                getattr(interaction.user, "id", None),
                slug,
                exc,
            )
            await interaction.followup.send(
                embed=build_status_embed(
                    title="Invalid JSON",
                    description=str(exc),
                    color=EmbedColors.WARNING,
                ),
                ephemeral=True,
            )
            return

        try:
            logger.info(
                "Kingshot upsert command received: user_id=%s guild_id=%s channel_id=%s "
                "entity_type=%s slug=%s name=%s content_chars=%s data_keys=%s metadata_keys=%s source=%s",
                getattr(interaction.user, "id", None),
                getattr(interaction.guild, "id", None),
                getattr(getattr(interaction, "channel", None), "id", None),
                entity_type,
                slug,
                name,
                len(self._clean_text(content)),
                sorted(data.keys()),
                sorted(metadata.keys()),
                self._clean_optional_text(source),
            )
            entity = await self._rag_service.upsert_knowledge(
                entity_type=entity_type,
                slug=slug,
                name=name,
                data=data,
                chunks=[
                    KingshotChunkInput(
                        content=content,
                        metadata=metadata,
                        source=self._clean_optional_text(source),
                    )
                ],
                source=self._clean_optional_text(source),
                replace_chunks=True,
            )
            await interaction.followup.send(
                embed=build_status_embed(
                    title="Kingshot Knowledge Saved",
                    description=f"Updated `{entity.slug}` with one embedded chunk.",
                    color=EmbedColors.SUCCESS,
                ),
                ephemeral=True,
            )
        except KingshotRAGError as exc:
            await interaction.followup.send(
                embed=build_status_embed(
                    title="Could Not Save Knowledge",
                    description=str(exc),
                    color=EmbedColors.WARNING,
                ),
                ephemeral=True,
            )
        except Exception as exc:
            logger.error("Kingshot RAG upsert failed: %s", exc, exc_info=True)
            await interaction.followup.send(
                embed=build_status_embed(
                    title="Knowledge Save Failed",
                    description="The Kingshot knowledge update failed. Check logs and try again.",
                    color=EmbedColors.ERROR,
                ),
                ephemeral=True,
            )

    @staticmethod
    def _is_admin(interaction: discord.Interaction) -> bool:
        return bool(
            interaction.guild
            and getattr(interaction.user, "guild_permissions", None)
            and interaction.user.guild_permissions.administrator
        )

    @staticmethod
    def _parse_json_object(value: Optional[str], *, default: dict) -> dict:
        cleaned = KingshotRAGHandler._clean_optional_text(value)
        if not cleaned:
            return default
        try:
            parsed = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            raise ValueError(f"JSON could not be parsed: {exc.msg}") from exc
        if not isinstance(parsed, dict):
            raise ValueError("JSON value must be an object, for example `{}`.")
        return parsed

    @staticmethod
    def _clean_text(value: str) -> str:
        return " ".join(str(value or "").split())

    @staticmethod
    def _clean_optional_text(value: Optional[str]) -> Optional[str]:
        cleaned = " ".join(str(value or "").split())
        return cleaned or None

    @staticmethod
    def _truncate(value: str, limit: int) -> str:
        if len(value) <= limit:
            return value
        return value[: max(0, limit - 3)] + "..."
