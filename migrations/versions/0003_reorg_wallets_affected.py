"""record the wallets a reorg removed events from

Revision ID: 0003
Revises: 0002
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "reorgs",
        sa.Column(
            "wallets_affected",
            postgresql.ARRAY(sa.String(length=42)),
            server_default="{}",
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("reorgs", "wallets_affected")
