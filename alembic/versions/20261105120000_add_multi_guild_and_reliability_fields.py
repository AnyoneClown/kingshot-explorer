"""add_multi_guild_and_reliability_fields

Adds per-guild gift-code rosters, per-guild channels and manager role,
resumable auto-redemption state for gift codes, and delivery tracking for
scheduled reminders.

Revision ID: 20261105120000
Revises: 20260905120000
Create Date: 2026-11-05 12:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20261105120000"
down_revision: Union[str, None] = "20260905120000"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns(inspector, table: str) -> set[str]:
    return {column["name"] for column in inspector.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table("gift_codes"):
        columns = _columns(inspector, "gift_codes")
        if "auto_redeem_status" not in columns:
            # Existing codes are backfilled as "completed" by the server default, so
            # they are never re-redeemed. The application inserts new codes as "pending".
            op.add_column(
                "gift_codes",
                sa.Column(
                    "auto_redeem_status",
                    sa.String(length=20),
                    nullable=False,
                    server_default="completed",
                ),
            )
        if "auto_redeem_completed_at" not in columns:
            op.add_column(
                "gift_codes",
                sa.Column("auto_redeem_completed_at", sa.DateTime(timezone=True), nullable=True),
            )

    if inspector.has_table("scheduled_reminders"):
        columns = _columns(inspector, "scheduled_reminders")
        if "guild_id" not in columns:
            op.add_column("scheduled_reminders", sa.Column("guild_id", sa.BigInteger(), nullable=True))
            op.create_index(
                op.f("ix_scheduled_reminders_guild_id"),
                "scheduled_reminders",
                ["guild_id"],
                unique=False,
            )
        if "created_by_user_id" not in columns:
            op.add_column("scheduled_reminders", sa.Column("created_by_user_id", sa.BigInteger(), nullable=True))
        if "last_sent_at" not in columns:
            op.add_column(
                "scheduled_reminders",
                sa.Column("last_sent_at", sa.DateTime(timezone=True), nullable=True),
            )
        if "last_error" not in columns:
            op.add_column("scheduled_reminders", sa.Column("last_error", sa.Text(), nullable=True))

    if inspector.has_table("guild_configurations"):
        columns = _columns(inspector, "guild_configurations")
        for name in ("manager_role_id", "gift_code_channel_id", "alliance_report_channel_id"):
            if name not in columns:
                op.add_column("guild_configurations", sa.Column(name, sa.BigInteger(), nullable=True))

    if not inspector.has_table("guild_player_registrations"):
        op.create_table(
            "guild_player_registrations",
            sa.Column("guild_id", sa.BigInteger(), nullable=False),
            sa.Column("player_id", sa.String(length=255), nullable=False),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
            sa.Column("added_by_user_id", sa.BigInteger(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.ForeignKeyConstraint(
                ["player_id"],
                ["registered_players.player_id"],
                ondelete="CASCADE",
                onupdate="CASCADE",
            ),
            sa.PrimaryKeyConstraint("guild_id", "player_id"),
        )
        op.create_index(
            "ix_guild_player_registrations_player_id",
            "guild_player_registrations",
            ["player_id"],
            unique=False,
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table("guild_player_registrations"):
        op.drop_index("ix_guild_player_registrations_player_id", table_name="guild_player_registrations")
        op.drop_table("guild_player_registrations")

    if inspector.has_table("guild_configurations"):
        columns = _columns(inspector, "guild_configurations")
        for name in ("alliance_report_channel_id", "gift_code_channel_id", "manager_role_id"):
            if name in columns:
                op.drop_column("guild_configurations", name)

    if inspector.has_table("scheduled_reminders"):
        columns = _columns(inspector, "scheduled_reminders")
        indexes = {index["name"] for index in inspector.get_indexes("scheduled_reminders")}
        if "ix_scheduled_reminders_guild_id" in indexes:
            op.drop_index(op.f("ix_scheduled_reminders_guild_id"), table_name="scheduled_reminders")
        for name in ("last_error", "last_sent_at", "created_by_user_id", "guild_id"):
            if name in columns:
                op.drop_column("scheduled_reminders", name)

    if inspector.has_table("gift_codes"):
        columns = _columns(inspector, "gift_codes")
        for name in ("auto_redeem_completed_at", "auto_redeem_status"):
            if name in columns:
                op.drop_column("gift_codes", name)
