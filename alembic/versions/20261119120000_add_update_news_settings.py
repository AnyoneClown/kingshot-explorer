"""add_update_news_settings

Adds what a guild needs for the bot's update news: whether it is posted there
(on unless a manager turns it off), the channel a manager chose (``NULL`` means the
guild's general channel) and the id of the newest note the guild was given.

Revision ID: 20261119120000
Revises: 20261118120000
Create Date: 2026-11-19 12:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20261119120000"
down_revision: Union[str, None] = "20261118120000"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_COLUMNS = (
    lambda: sa.Column("update_news_enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
    lambda: sa.Column("update_news_channel_id", sa.BigInteger(), nullable=True),
    lambda: sa.Column("update_news_last_id", sa.String(length=64), nullable=True),
)


def _columns(inspector, table: str) -> set[str]:
    return {column["name"] for column in inspector.get_columns(table)}


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("guild_configurations"):
        return
    existing = _columns(inspector, "guild_configurations")
    for build in _COLUMNS:
        column = build()
        if column.name not in existing:
            op.add_column("guild_configurations", column)


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("guild_configurations"):
        return
    existing = _columns(inspector, "guild_configurations")
    for build in reversed(_COLUMNS):
        name = build().name
        if name in existing:
            op.drop_column("guild_configurations", name)
