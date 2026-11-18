"""add_guild_translation_languages

Adds the languages a guild translates its chat between, as comma-separated codes
(``en,uk``). An empty string switches translation off; ``NULL`` means the guild
never chose, and the bot decides once from the old ``Translator`` role.

Revision ID: 20261118120000
Revises: 20261115120000
Create Date: 2026-11-18 12:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20261118120000"
down_revision: Union[str, None] = "20261115120000"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns(inspector, table: str) -> set[str]:
    return {column["name"] for column in inspector.get_columns(table)}


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("guild_configurations"):
        return
    if "translation_languages" not in _columns(inspector, "guild_configurations"):
        op.add_column(
            "guild_configurations", sa.Column("translation_languages", sa.String(length=64), nullable=True)
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("guild_configurations"):
        return
    if "translation_languages" in _columns(inspector, "guild_configurations"):
        op.drop_column("guild_configurations", "translation_languages")
