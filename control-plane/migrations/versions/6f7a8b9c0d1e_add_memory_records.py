"""add account-scoped durable memory records

Revision ID: 6f7a8b9c0d1e
Revises: 5e47e3df9dac
Create Date: 2026-08-16 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "6f7a8b9c0d1e"
down_revision: Union[str, Sequence[str], None] = "5e47e3df9dac"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "memory_records",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("account_id", sa.String(length=36), nullable=False),
        sa.Column("scope", sa.String(length=128), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column("source_session_id", sa.String(length=36), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "account_id",
            "scope",
            "kind",
            "content_hash",
            name="uq_memory_records_account_scope_kind_hash",
        ),
    )
    op.create_index("ix_memory_records_account_id", "memory_records", ["account_id"], unique=False)
    op.create_index(
        "ix_memory_records_account_scope_updated",
        "memory_records",
        ["account_id", "scope", "updated_at"],
        unique=False,
    )
    op.create_index(
        "ix_memory_records_account_expires", "memory_records", ["account_id", "expires_at"], unique=False
    )
    op.create_index(
        "ix_memory_records_source_session_id", "memory_records", ["source_session_id"], unique=False
    )
    op.create_index("ix_memory_records_content_hash", "memory_records", ["content_hash"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_memory_records_content_hash", table_name="memory_records")
    op.drop_index("ix_memory_records_source_session_id", table_name="memory_records")
    op.drop_index("ix_memory_records_account_expires", table_name="memory_records")
    op.drop_index("ix_memory_records_account_scope_updated", table_name="memory_records")
    op.drop_index("ix_memory_records_account_id", table_name="memory_records")
    op.drop_table("memory_records")
