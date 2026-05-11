"""remove_rag_and_ocr_tables

Revision ID: 20260511162000
Revises: 20260511154000
Create Date: 2026-05-11 16:20:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.types import UserDefinedType

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260511162000"
down_revision: Union[str, None] = "20260511154000"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


class Vector(UserDefinedType):
    """Minimal VECTOR type for CockroachDB migrations."""

    cache_ok = True

    def __init__(self, dimensions: int):
        self.dimensions = dimensions

    def get_col_spec(self, **kw) -> str:
        return f"VECTOR({self.dimensions})"


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS knowledge_chunks_embedding_idx")
    op.drop_index(op.f("ix_knowledge_chunks_knowledge_item_id"), table_name="knowledge_chunks")
    op.drop_index(op.f("ix_knowledge_chunks_entity_type"), table_name="knowledge_chunks")
    op.drop_table("knowledge_chunks")

    op.drop_index(op.f("ix_knowledge_items_created_by_user_id"), table_name="knowledge_items")
    op.drop_index(op.f("ix_knowledge_items_title"), table_name="knowledge_items")
    op.drop_index(op.f("ix_knowledge_items_is_active"), table_name="knowledge_items")
    op.drop_index(op.f("ix_knowledge_items_entity_type"), table_name="knowledge_items")
    op.drop_table("knowledge_items")

    op.drop_index(op.f("ix_ocr_results_ocr_request_id"), table_name="ocr_results")
    op.drop_table("ocr_results")

    op.drop_index(op.f("ix_ocr_requests_created_at"), table_name="ocr_requests")
    op.drop_index(op.f("ix_ocr_requests_ocr_type"), table_name="ocr_requests")
    op.drop_index(op.f("ix_ocr_requests_user_id"), table_name="ocr_requests")
    op.drop_table("ocr_requests")


def downgrade() -> None:
    op.create_table(
        "ocr_requests",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("guild_id", sa.BigInteger(), nullable=True),
        sa.Column("channel_id", sa.BigInteger(), nullable=True),
        sa.Column("ocr_type", sa.String(length=50), nullable=False),
        sa.Column("image_count", sa.Integer(), nullable=False),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_ocr_requests_user_id"), "ocr_requests", ["user_id"], unique=False)
    op.create_index(op.f("ix_ocr_requests_ocr_type"), "ocr_requests", ["ocr_type"], unique=False)
    op.create_index(op.f("ix_ocr_requests_created_at"), "ocr_requests", ["created_at"], unique=False)

    op.create_table(
        "ocr_results",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("ocr_request_id", sa.Integer(), nullable=False),
        sa.Column("image_index", sa.Integer(), nullable=False),
        sa.Column("extracted_data", postgresql.JSON(astext_type=sa.Text()), nullable=True),
        sa.Column("raw_text", sa.Text(), nullable=True),
        sa.Column("confidence_score", sa.Float(), nullable=True),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["ocr_request_id"], ["ocr_requests.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_ocr_results_ocr_request_id"), "ocr_results", ["ocr_request_id"], unique=False)

    op.create_table(
        "knowledge_items",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("entity_type", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_by_user_id", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_knowledge_items_entity_type"), "knowledge_items", ["entity_type"], unique=False)
    op.create_index(op.f("ix_knowledge_items_is_active"), "knowledge_items", ["is_active"], unique=False)
    op.create_index(op.f("ix_knowledge_items_title"), "knowledge_items", ["title"], unique=False)
    op.create_index(
        op.f("ix_knowledge_items_created_by_user_id"),
        "knowledge_items",
        ["created_by_user_id"],
        unique=False,
    )

    op.create_table(
        "knowledge_chunks",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("knowledge_item_id", sa.Integer(), nullable=False),
        sa.Column("entity_type", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(1024), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["knowledge_item_id"], ["knowledge_items.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_knowledge_chunks_entity_type"), "knowledge_chunks", ["entity_type"], unique=False)
    op.create_index(
        op.f("ix_knowledge_chunks_knowledge_item_id"),
        "knowledge_chunks",
        ["knowledge_item_id"],
        unique=False,
    )
    op.execute(
        """
        CREATE VECTOR INDEX knowledge_chunks_embedding_idx
        ON knowledge_chunks (entity_type, embedding vector_cosine_ops)
        """
    )
