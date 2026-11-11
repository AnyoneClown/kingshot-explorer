"""add_event_calendar_settings

Adds the kingdom a guild plays in (one per guild) and the settings of its daily
event calendar post: the channel and the UTC day of the last post.

Revision ID: 20261112120000
Revises: 20261111120000
Create Date: 2026-11-12 12:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20261112120000"
down_revision: Union[str, None] = "20261111120000"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_COLUMNS = (
    ("calendar_channel_id", sa.BigInteger),
    ("kingdom_id", sa.Integer),
    ("calendar_last_posted_on", sa.Date),
)


def _columns(inspector, table: str) -> set[str]:
    return {column["name"] for column in inspector.get_columns(table)}


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("guild_configurations"):
        return
    existing = _columns(inspector, "guild_configurations")
    for name, column_type in _COLUMNS:
        if name not in existing:
            op.add_column("guild_configurations", sa.Column(name, column_type(), nullable=True))


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("guild_configurations"):
        return
    existing = _columns(inspector, "guild_configurations")
    for name, _column_type in reversed(_COLUMNS):
        if name in existing:
            op.drop_column("guild_configurations", name)
