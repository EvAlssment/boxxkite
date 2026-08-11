"""add session config to sandbox_sessions and snapshots

Revision ID: 5e47e3df9dac
Revises: c3d4e5f6a7b8
Create Date: 2026-08-11 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '5e47e3df9dac'
down_revision: Union[str, Sequence[str], None] = 'c3d4e5f6a7b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    for table in ("sandbox_sessions", "snapshots"):
        op.add_column(table, sa.Column('size', sa.String(length=32), nullable=True))
        op.add_column(table, sa.Column('storage_gb', sa.Float(), nullable=True))
        op.add_column(table, sa.Column('image_id', sa.String(length=36), nullable=True))
        op.add_column(table, sa.Column('secret_names', sa.JSON(), nullable=True))
        op.add_column(table, sa.Column('volume_mounts', sa.JSON(), nullable=True))
        op.add_column(table, sa.Column('mcp_connection_names', sa.JSON(), nullable=True))
        op.add_column(table, sa.Column('gpu_count', sa.Integer(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    for table in ("sandbox_sessions", "snapshots"):
        op.drop_column(table, 'gpu_count')
        op.drop_column(table, 'mcp_connection_names')
        op.drop_column(table, 'volume_mounts')
        op.drop_column(table, 'secret_names')
        op.drop_column(table, 'image_id')
        op.drop_column(table, 'storage_gb')
        op.drop_column(table, 'size')
