"""add_gift_redemption_bulk_lookup_index

Revision ID: 20260617120000
Revises: 20260615120000
Create Date: 2026-06-17 12:00:00.000000

"""

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "20260617120000"
down_revision: Union[str, None] = "20260615120000"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "ix_gift_code_redemptions_code_status_player",
        "gift_code_redemptions",
        ["gift_code", "success", "error_code", "player_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_gift_code_redemptions_code_status_player",
        table_name="gift_code_redemptions",
    )
