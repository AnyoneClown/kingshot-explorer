"""share_gift_code_list_across_servers

The gift-code list is no longer split into per-server rosters: an account is listed
once, redeemed for whichever server added it, and shown in the roster of every server
set to the account's kingdom. ``guild_player_registrations`` (which server lists which
account, with a per-server enabled flag and owner) is therefore dropped, and every row
of ``registered_players`` becomes a listed account.

Before the table goes, what it knew is folded into ``registered_players``:

- Profiles that were only a cache of a ``/player`` lookup are deleted: disabled, on no
  server's roster and never tried with any gift code. They were never on a list, and
  as rows they would now appear in rosters and count against the member who looked.
- The owner becomes the member who listed the account when the profile's own owner
  never did (the profile was first created by somebody's lookup).
- The enabled flag keeps the set of accounts that receive codes unchanged: an account
  that servers listed and all of them disabled stops being redeemed, as it was not
  redeemed before. Accounts no server listed (legacy profiles) keep their own flag.

A downgrade recreates the table empty; which server listed which account is not kept.

Revision ID: 20261114120000
Revises: 20261113120000
Create Date: 2026-11-14 12:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20261114120000"
down_revision: Union[str, None] = "20261113120000"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "guild_player_registrations"


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(_TABLE):
        return
    for statement in (
        """
        DELETE FROM registered_players
        WHERE NOT enabled
          AND NOT EXISTS (
            SELECT 1 FROM guild_player_registrations g
            WHERE g.player_id = registered_players.player_id
          )
          AND NOT EXISTS (
            SELECT 1 FROM gift_code_redemptions r
            WHERE r.player_id = registered_players.player_id
          )
        """,
        """
        UPDATE registered_players SET added_by_user_id = (
            SELECT g.added_by_user_id FROM guild_player_registrations g
            WHERE g.player_id = registered_players.player_id
              AND EXISTS (SELECT 1 FROM users u WHERE u.id = g.added_by_user_id)
            ORDER BY g.created_at, g.guild_id
            LIMIT 1
        )
        WHERE EXISTS (
            SELECT 1 FROM guild_player_registrations g
            WHERE g.player_id = registered_players.player_id
              AND EXISTS (SELECT 1 FROM users u WHERE u.id = g.added_by_user_id)
          )
          AND NOT EXISTS (
            SELECT 1 FROM guild_player_registrations g
            WHERE g.player_id = registered_players.player_id
              AND g.added_by_user_id = registered_players.added_by_user_id
          )
        """,
        """
        UPDATE registered_players SET enabled = false
        WHERE enabled
          AND EXISTS (
            SELECT 1 FROM guild_player_registrations g
            WHERE g.player_id = registered_players.player_id
          )
          AND NOT EXISTS (
            SELECT 1 FROM guild_player_registrations g
            WHERE g.player_id = registered_players.player_id AND g.enabled
          )
        """,
    ):
        op.execute(sa.text(statement))
    op.drop_table(_TABLE)


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table(_TABLE):
        return
    op.create_table(
        _TABLE,
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("player_id", sa.String(length=255), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("added_by_user_id", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["player_id"], ["registered_players.player_id"], ondelete="CASCADE", onupdate="CASCADE"
        ),
        sa.PrimaryKeyConstraint("guild_id", "player_id"),
    )
    op.create_index("ix_guild_player_registrations_player_id", _TABLE, ["player_id"])
