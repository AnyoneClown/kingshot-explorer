"""change_knowledge_vectors_to_1024

Revision ID: 20260511154000
Revises: 20260508120000
Create Date: 2026-05-11 15:40:00.000000

"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260511154000"
down_revision: Union[str, None] = "20260508120000"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS knowledge_chunks_embedding_idx")
    op.execute("ALTER TABLE knowledge_chunks ALTER COLUMN embedding TYPE VECTOR(1024)")
    op.execute(
        """
        CREATE VECTOR INDEX knowledge_chunks_embedding_idx
        ON knowledge_chunks (entity_type, embedding vector_cosine_ops)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS knowledge_chunks_embedding_idx")
    op.execute("ALTER TABLE knowledge_chunks ALTER COLUMN embedding TYPE VECTOR(1536)")
    op.execute(
        """
        CREATE VECTOR INDEX knowledge_chunks_embedding_idx
        ON knowledge_chunks (entity_type, embedding vector_cosine_ops)
        """
    )
