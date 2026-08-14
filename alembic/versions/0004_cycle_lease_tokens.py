"""add ownership tokens for improvement-cycle leases

Revision ID: 0004_cycle_lease_tokens
Revises: 0003_factory_zero
"""

from alembic import op
import sqlalchemy as sa


revision = "0004_cycle_lease_tokens"
down_revision = "0003_factory_zero"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("improvement_cycles", sa.Column("lease_token", sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("improvement_cycles", "lease_token")
