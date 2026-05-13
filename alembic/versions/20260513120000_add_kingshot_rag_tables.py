"""add_kingshot_rag_tables

Revision ID: 20260513120000
Revises: 20260511170000
Create Date: 2026-05-13 12:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.types import UserDefinedType

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260513120000"
down_revision: Union[str, None] = "20260511170000"
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
        "kingshot_entities",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("entity_type", sa.String(), nullable=False),
        sa.Column("slug", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("data", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("source", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug"),
        if_not_exists=True,
    )
    op.create_table(
        "kingshot_chunks",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("content", sa.String(), nullable=False),
        sa.Column("embedding", Vector(2048), nullable=False),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("source", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["entity_id"], ["kingshot_entities.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        if_not_exists=True,
    )

    op.create_index(
        "idx_kingshot_entities_type",
        "kingshot_entities",
        ["entity_type"],
        unique=False,
        if_not_exists=True,
    )
    op.create_index(
        "idx_kingshot_entities_data_gin",
        "kingshot_entities",
        ["data"],
        unique=False,
        postgresql_using="gin",
        if_not_exists=True,
    )

    try:
        with op.get_context().autocommit_block():
            op.execute(
                """
                CREATE VECTOR INDEX IF NOT EXISTS idx_kingshot_chunks_embedding
                ON kingshot_chunks (embedding vector_cosine_ops)
                """
            )
    except Exception:
        # CockroachDB versions before vector indexes still support exact VECTOR
        # similarity search. Keep the schema migration usable on those versions.
        pass


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_kingshot_chunks_embedding")
    op.drop_index("idx_kingshot_entities_data_gin", table_name="kingshot_entities", if_exists=True)
    op.drop_index("idx_kingshot_entities_type", table_name="kingshot_entities", if_exists=True)
    op.drop_table("kingshot_chunks", if_exists=True)
    op.drop_table("kingshot_entities", if_exists=True)
