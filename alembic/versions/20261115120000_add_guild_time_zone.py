"""add_guild_time_zone

Adds the timezone of a guild (an IANA name such as ``Europe/Kyiv``). Reminder
times are entered in it; a guild without one uses UTC.

Revision ID: 20261115120000
Revises: 20261114120000
Create Date: 2026-11-15 12:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20261115120000"
down_revision: Union[str, None] = "20261114120000"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns(inspector, table: str) -> set[str]:
    return {column["name"] for column in inspector.get_columns(table)}


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("guild_configurations"):
        return
    if "time_zone" not in _columns(inspector, "guild_configurations"):
        op.add_column("guild_configurations", sa.Column("time_zone", sa.String(length=64), nullable=True))


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("guild_configurations"):
        return
    if "time_zone" in _columns(inspector, "guild_configurations"):
        op.drop_column("guild_configurations", "time_zone")
