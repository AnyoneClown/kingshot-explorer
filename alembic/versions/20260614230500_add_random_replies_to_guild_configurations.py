"""add_random_replies_to_guild_configurations

Revision ID: 20260614230500
Revises: 20260523071500
Create Date: 2026-06-14 23:05:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260614230500"
down_revision: Union[str, None] = "20260523071500"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table("guild_configurations"):
        return

    columns = {column["name"] for column in inspector.get_columns("guild_configurations")}
    if "use_random_replies" not in columns:
        op.add_column(
            "guild_configurations",
            sa.Column("use_random_replies", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table("guild_configurations"):
        return

    columns = {column["name"] for column in inspector.get_columns("guild_configurations")}
    if "use_random_replies" in columns:
        op.drop_column("guild_configurations", "use_random_replies")
