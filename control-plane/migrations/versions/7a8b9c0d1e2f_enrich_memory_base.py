"""enrich MemoryBase with provenance, temporal data, and relations

Revision ID: 7a8b9c0d1e2f
Revises: 6f7a8b9c0d1e
Create Date: 2026-08-16 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "7a8b9c0d1e2f"
down_revision: Union[str, Sequence[str], None] = "6f7a8b9c0d1e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("memory_records", sa.Column("source_content", sa.Text(), nullable=True))
    op.add_column("memory_records", sa.Column("document_date", sa.DateTime(timezone=True), nullable=True))
    op.add_column("memory_records", sa.Column("event_dates", sa.JSON(), nullable=True))
    op.add_column("memory_records", sa.Column("importance", sa.Float(), nullable=False, server_default="0.5"))
    op.add_column("memory_records", sa.Column("access_count", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("memory_records", sa.Column("last_accessed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("memory_records", sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("memory_records", sa.Column("superseded_by_id", sa.String(length=36), nullable=True))
    with op.batch_alter_table("memory_records") as batch_op:
        batch_op.create_foreign_key(
            "fk_memory_records_superseded_by_id",
            "memory_records",
            ["superseded_by_id"],
            ["id"],
            ondelete="SET NULL",
        )
    op.create_index(
        "ix_memory_records_account_scope_kind", "memory_records", ["account_id", "scope", "kind"], unique=False
    )
    op.create_index(
        "ix_memory_records_account_superseded", "memory_records", ["account_id", "superseded_at"], unique=False
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "CREATE INDEX ix_memory_records_content_fts ON memory_records "
            "USING GIN (to_tsvector('simple', content))"
        )
    op.create_table(
        "memory_relations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("account_id", sa.String(length=36), nullable=False),
        sa.Column("source_memory_id", sa.String(length=36), nullable=False),
        sa.Column("target_memory_id", sa.String(length=36), nullable=False),
        sa.Column("relation_type", sa.String(length=24), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0.5"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_memory_id"], ["memory_records.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["target_memory_id"], ["memory_records.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "account_id", "source_memory_id", "target_memory_id", "relation_type",
            name="uq_memory_relations_edge",
        ),
    )
    op.create_index("ix_memory_relations_account_id", "memory_relations", ["account_id"], unique=False)
    op.create_index(
        "ix_memory_relations_account_source", "memory_relations", ["account_id", "source_memory_id"], unique=False
    )
    op.create_index(
        "ix_memory_relations_account_target", "memory_relations", ["account_id", "target_memory_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_memory_relations_account_target", table_name="memory_relations")
    op.drop_index("ix_memory_relations_account_source", table_name="memory_relations")
    op.drop_index("ix_memory_relations_account_id", table_name="memory_relations")
    op.drop_table("memory_relations")
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP INDEX IF EXISTS ix_memory_records_content_fts")
    op.drop_index("ix_memory_records_account_superseded", table_name="memory_records")
    op.drop_index("ix_memory_records_account_scope_kind", table_name="memory_records")
    with op.batch_alter_table("memory_records") as batch_op:
        batch_op.drop_constraint("fk_memory_records_superseded_by_id", type_="foreignkey")
    op.drop_column("memory_records", "superseded_by_id")
    op.drop_column("memory_records", "superseded_at")
    op.drop_column("memory_records", "last_accessed_at")
    op.drop_column("memory_records", "access_count")
    op.drop_column("memory_records", "importance")
    op.drop_column("memory_records", "event_dates")
    op.drop_column("memory_records", "document_date")
    op.drop_column("memory_records", "source_content")
