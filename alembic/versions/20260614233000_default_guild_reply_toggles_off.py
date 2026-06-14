"""default_guild_reply_toggles_off

Revision ID: 20260614233000
Revises: 20260614230500
Create Date: 2026-06-14 23:30:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260614233000"
down_revision: Union[str, None] = "20260614230500"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table("guild_configurations"):
        return

    columns = {column["name"] for column in inspector.get_columns("guild_configurations")}

    if "use_voice_replies" in columns:
        op.alter_column(
            "guild_configurations",
            "use_voice_replies",
            server_default=sa.text("false"),
            existing_type=sa.Boolean(),
            existing_nullable=False,
        )
        op.execute("UPDATE guild_configurations SET use_voice_replies = false")

    if "use_random_replies" in columns:
        op.alter_column(
            "guild_configurations",
            "use_random_replies",
            server_default=sa.text("false"),
            existing_type=sa.Boolean(),
            existing_nullable=False,
        )
        op.execute("UPDATE guild_configurations SET use_random_replies = false")


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table("guild_configurations"):
        return

    columns = {column["name"] for column in inspector.get_columns("guild_configurations")}

    if "use_voice_replies" in columns:
        op.alter_column(
            "guild_configurations",
            "use_voice_replies",
            server_default=sa.text("true"),
            existing_type=sa.Boolean(),
            existing_nullable=False,
        )

    if "use_random_replies" in columns:
        op.alter_column(
            "guild_configurations",
            "use_random_replies",
            server_default=sa.text("true"),
            existing_type=sa.Boolean(),
            existing_nullable=False,
        )
