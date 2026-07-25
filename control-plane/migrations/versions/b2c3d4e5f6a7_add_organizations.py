"""add organizations and organization_members

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-07-21 01:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b2c3d4e5f6a7'
down_revision: Union[str, Sequence[str], None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'organizations',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column('created_by_account_id', sa.String(length=36), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['created_by_account_id'], ['accounts.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_organizations_created_by_account_id', 'organizations', ['created_by_account_id']
    )
    op.create_table(
        'organization_members',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('organization_id', sa.String(length=36), nullable=False),
        sa.Column('account_id', sa.String(length=36), nullable=False),
        sa.Column('role', sa.String(length=20), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('organization_id', 'account_id', name='uq_org_member'),
    )
    op.create_index(
        'ix_organization_members_organization_id', 'organization_members', ['organization_id']
    )
    op.create_index(
        'ix_organization_members_account', 'organization_members', ['account_id']
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_organization_members_account', table_name='organization_members')
    op.drop_index('ix_organization_members_organization_id', table_name='organization_members')
    op.drop_table('organization_members')
    op.drop_index('ix_organizations_created_by_account_id', table_name='organizations')
    op.drop_table('organizations')
