"""add_registered_player_uid_cache

Revision ID: 20260615120000
Revises: 20260614233000
Create Date: 2026-06-15 12:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260615120000"
down_revision: Union[str, None] = "20260614233000"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table("registered_players"):
        return

    columns = {column["name"] for column in inspector.get_columns("registered_players")}
    indexes = {index["name"] for index in inspector.get_indexes("registered_players")}

    if "player_uid" not in columns:
        op.add_column("registered_players", sa.Column("player_uid", sa.String(length=255), nullable=True))

    if "ix_registered_players_player_uid" not in indexes:
        op.create_index(
            op.f("ix_registered_players_player_uid"),
            "registered_players",
            ["player_uid"],
            unique=True,
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table("registered_players"):
        return

    columns = {column["name"] for column in inspector.get_columns("registered_players")}
    indexes = {index["name"] for index in inspector.get_indexes("registered_players")}

    if "ix_registered_players_player_uid" in indexes:
        op.drop_index(op.f("ix_registered_players_player_uid"), table_name="registered_players")

    if "player_uid" in columns:
        op.drop_column("registered_players", "player_uid")
