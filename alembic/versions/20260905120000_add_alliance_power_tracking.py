"""add_alliance_power_tracking

Revision ID: 20260905120000
Revises: 20260617120000
Create Date: 2026-09-05 12:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260905120000"
down_revision: Union[str, None] = "20260617120000"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "alliance_power_tracking",
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("kingdom_id", sa.Integer(), nullable=False),
        sa.Column("alliance_id", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("guild_id"),
    )
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


def downgrade() -> None:
    op.drop_table("alliance_power_snapshots")
    op.drop_table("alliance_power_tracking")
