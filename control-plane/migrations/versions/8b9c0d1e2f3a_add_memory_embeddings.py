"""add pgvector-backed memory embeddings

Revision ID: 8b9c0d1e2f3a
Revises: 7a8b9c0d1e2f
Create Date: 2026-08-16 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
from pgvector.sqlalchemy import VECTOR
import sqlalchemy as sa


revision: str = "8b9c0d1e2f3a"
down_revision: Union[str, Sequence[str], None] = "7a8b9c0d1e2f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")
        op.execute(
            """
            DO $$
            DECLARE installed_version text;
            BEGIN
                SELECT extversion INTO installed_version
                FROM pg_extension WHERE extname = 'vector';
                IF (split_part(installed_version, '.', 1)::int,
                    split_part(installed_version, '.', 2)::int) < (0, 8) THEN
                    RAISE EXCEPTION
                        'MemoryBase requires pgvector 0.8 or newer; installed version is %',
                        installed_version;
                END IF;
            END $$
            """
        )
    embedding_type = VECTOR() if dialect == "postgresql" else sa.JSON()
    op.create_table(
        "memory_embeddings",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("account_id", sa.String(length=36), nullable=False),
        sa.Column("memory_id", sa.String(length=36), nullable=False),
        sa.Column("model_id", sa.String(length=191), nullable=False),
        sa.Column("dimensions", sa.Integer(), nullable=False),
        sa.Column("embedding", embedding_type, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["memory_id"], ["memory_records.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("memory_id", "model_id", name="uq_memory_embeddings_memory_model"),
    )
    op.create_index("ix_memory_embeddings_account_id", "memory_embeddings", ["account_id"])
    op.create_index("ix_memory_embeddings_memory_id", "memory_embeddings", ["memory_id"])
    op.create_index(
        "ix_memory_embeddings_account_model", "memory_embeddings", ["account_id", "model_id"]
    )
    if dialect == "postgresql":
        op.execute(
            "CREATE INDEX ix_memory_embeddings_default_hnsw ON memory_embeddings "
            "USING hnsw ((embedding::vector(768)) vector_cosine_ops) "
            "WHERE model_id = 'qwen3-embedding-0.6b-768-v1' AND dimensions = 768"
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP INDEX IF EXISTS ix_memory_embeddings_default_hnsw")
    op.drop_index("ix_memory_embeddings_account_model", table_name="memory_embeddings")
    op.drop_index("ix_memory_embeddings_memory_id", table_name="memory_embeddings")
    op.drop_index("ix_memory_embeddings_account_id", table_name="memory_embeddings")
    op.drop_table("memory_embeddings")
