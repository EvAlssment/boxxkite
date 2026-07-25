"""add idempotency_keys

Revision ID: a1b2c3d4e5f6
Revises: e419cdafd64d
Create Date: 2026-07-21 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, Sequence[str], None] = 'e419cdafd64d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'idempotency_keys',
        sa.Column('scope_hash', sa.String(length=64), nullable=False),
        sa.Column('request_fingerprint', sa.String(length=64), nullable=False),
        sa.Column('response_status', sa.Integer(), nullable=True),
        sa.Column('response_body', sa.LargeBinary(), nullable=True),
        sa.Column('response_media_type', sa.String(length=128), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('scope_hash'),
    )
    op.create_index(
        'ix_idempotency_keys_created_at', 'idempotency_keys', ['created_at'], unique=False
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_idempotency_keys_created_at', table_name='idempotency_keys')
    op.drop_table('idempotency_keys')
