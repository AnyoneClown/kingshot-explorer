"""add_knowledge_base_tables

Revision ID: 20260508120000
Revises: 20260401143000
Create Date: 2026-05-08 12:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.types import UserDefinedType

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260508120000"
down_revision: Union[str, None] = "20260401143000"
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


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS knowledge_chunks_embedding_idx")
    op.drop_index(op.f("ix_knowledge_chunks_knowledge_item_id"), table_name="knowledge_chunks")
    op.drop_index(op.f("ix_knowledge_chunks_entity_type"), table_name="knowledge_chunks")
    op.drop_table("knowledge_chunks")

    op.drop_index(op.f("ix_knowledge_items_created_by_user_id"), table_name="knowledge_items")
    op.drop_index(op.f("ix_knowledge_items_title"), table_name="knowledge_items")
    op.drop_index(op.f("ix_knowledge_items_is_active"), table_name="knowledge_items")
    op.drop_index(op.f("ix_knowledge_items_entity_type"), table_name="knowledge_items")
    op.drop_table("knowledge_items")
