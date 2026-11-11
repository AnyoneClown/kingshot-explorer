"""drop_voice_and_alliance_power_leftovers

Removes what the deleted features left behind: the voice-reply and alliance-report
settings of a guild, and the alliance power tracking tables with their snapshot
history. The data is not kept: a downgrade recreates the structures empty.

Revision ID: 20261113120000
Revises: 20261112120000
Create Date: 2026-11-13 12:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20261113120000"
down_revision: Union[str, None] = "20261112120000"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLES = ("alliance_power_snapshots", "alliance_power_tracking")
_COLUMNS = ("use_voice_replies", "alliance_report_channel_id")


def _columns(inspector, table: str) -> set[str]:
    return {column["name"] for column in inspector.get_columns(table)}


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    for table in _TABLES:
        if inspector.has_table(table):
            op.drop_table(table)
    if inspector.has_table("guild_configurations"):
        existing = _columns(inspector, "guild_configurations")
        for name in _COLUMNS:
            if name in existing:
                op.drop_column("guild_configurations", name)


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("guild_configurations"):
        existing = _columns(inspector, "guild_configurations")
        if "use_voice_replies" not in existing:
            op.add_column(
                "guild_configurations",
                sa.Column("use_voice_replies", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            )
        if "alliance_report_channel_id" not in existing:
            op.add_column("guild_configurations", sa.Column("alliance_report_channel_id", sa.BigInteger(), nullable=True))
    if not inspector.has_table("alliance_power_tracking"):
        op.create_table(
            "alliance_power_tracking",
            sa.Column("guild_id", sa.BigInteger(), nullable=False),
            sa.Column("kingdom_id", sa.Integer(), nullable=False),
            sa.Column("alliance_id", sa.BigInteger(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.PrimaryKeyConstraint("guild_id"),
        )
    if not inspector.has_table("alliance_power_snapshots"):
        op.create_table(
            "alliance_power_snapshots",
            sa.Column("kingdom_id", sa.Integer(), nullable=False),
            sa.Column("alliance_id", sa.BigInteger(), nullable=False),
            sa.Column("snapshot_date", sa.Date(), nullable=False),
            sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("alliance_name", sa.String(255), nullable=False),
            sa.Column("alliance_tag", sa.String(32), nullable=False),
            sa.Column("total_power", sa.BigInteger(), nullable=False),
            sa.Column("members", sa.JSON(), nullable=False),
            sa.PrimaryKeyConstraint("kingdom_id", "alliance_id", "snapshot_date"),
        )
