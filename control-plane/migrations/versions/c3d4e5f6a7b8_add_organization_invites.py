"""add organization_invites

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-07-21 02:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c3d4e5f6a7b8'
down_revision: Union[str, Sequence[str], None] = 'b2c3d4e5f6a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'organization_invites',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('organization_id', sa.String(length=36), nullable=False),
        sa.Column('email', sa.String(length=320), nullable=False),
        sa.Column('role', sa.String(length=20), nullable=False),
        sa.Column('token_hash', sa.String(length=64), nullable=False),
        sa.Column('invited_by_account_id', sa.String(length=36), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('accepted_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['invited_by_account_id'], ['accounts.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    # Names match SQLAlchemy's auto-naming for column-level index=True /
    # unique=True so the migration matches models_orm.py exactly (drift test).
    op.create_index(
        'ix_organization_invites_organization_id', 'organization_invites', ['organization_id']
    )
    op.create_index(
        'ix_organization_invites_token_hash', 'organization_invites', ['token_hash'], unique=True
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_organization_invites_token_hash', table_name='organization_invites')
    op.drop_index('ix_organization_invites_organization_id', table_name='organization_invites')
    op.drop_table('organization_invites')
