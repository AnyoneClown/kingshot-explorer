"""add_scheduled_reminders

Revision ID: 20260511170000
Revises: 20260511162000
Create Date: 2026-05-11 17:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260511170000"
down_revision: Union[str, None] = "20260511162000"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table("scheduled_reminders"):
        op.create_table(
            "scheduled_reminders",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("channel_id", sa.BigInteger(), nullable=False),
            sa.Column("reminder_time", sa.DateTime(timezone=True), nullable=False),
            sa.Column("role_names_json", sa.Text(), nullable=False),
            sa.Column("message", sa.Text(), nullable=False),
            sa.Column("repeat_every_days", sa.Integer(), nullable=True),
            sa.Column("is_active", sa.Boolean(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )

    op.create_index(
        op.f("ix_scheduled_reminders_channel_id"),
        "scheduled_reminders",
        ["channel_id"],
        unique=False,
        if_not_exists=True,
    )
    op.create_index(
        op.f("ix_scheduled_reminders_reminder_time"),
        "scheduled_reminders",
        ["reminder_time"],
        unique=False,
        if_not_exists=True,
    )
    op.create_index(
        op.f("ix_scheduled_reminders_is_active"),
        "scheduled_reminders",
        ["is_active"],
        unique=False,
        if_not_exists=True,
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table("scheduled_reminders"):
        op.drop_index(op.f("ix_scheduled_reminders_is_active"), table_name="scheduled_reminders")
        op.drop_index(op.f("ix_scheduled_reminders_reminder_time"), table_name="scheduled_reminders")
        op.drop_index(op.f("ix_scheduled_reminders_channel_id"), table_name="scheduled_reminders")
        op.drop_table("scheduled_reminders")
