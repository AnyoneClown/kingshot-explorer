"""add_announcements_and_command_usage

Adds persisted gift-code announcement messages (edited across passes and restarts)
and per-day command usage counters.

Revision ID: 20261111120000
Revises: 20261105120000
Create Date: 2026-11-11 12:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20261111120000"
down_revision: Union[str, None] = "20261105120000"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())

    if not inspector.has_table("gift_code_announcements"):
        op.create_table(
            "gift_code_announcements",
            sa.Column("gift_code", sa.String(length=255), nullable=False),
            sa.Column("channel_id", sa.BigInteger(), nullable=False),
            sa.Column("message_id", sa.BigInteger(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.PrimaryKeyConstraint("gift_code", "channel_id"),
        )

    if not inspector.has_table("command_usage_daily"):
        op.create_table(
            "command_usage_daily",
            sa.Column("usage_date", sa.Date(), nullable=False),
            sa.Column("guild_id", sa.BigInteger(), nullable=False),
            sa.Column("command_name", sa.String(length=100), nullable=False),
            sa.Column("uses", sa.Integer(), nullable=False, server_default="0"),
            sa.PrimaryKeyConstraint("usage_date", "guild_id", "command_name"),
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("command_usage_daily"):
        op.drop_table("command_usage_daily")
    if inspector.has_table("gift_code_announcements"):
        op.drop_table("gift_code_announcements")
