"""add_guild_configurations

Revision ID: 20260523071500
Revises: 20260513120000
Create Date: 2026-05-23 07:15:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260523071500"
down_revision: Union[str, None] = "20260513120000"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table("guild_configurations"):
        op.create_table(
            "guild_configurations",
            sa.Column("guild_id", sa.BigInteger(), nullable=False),
            sa.Column("use_voice_replies", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.PrimaryKeyConstraint("guild_id"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table("guild_configurations"):
        op.drop_table("guild_configurations")
