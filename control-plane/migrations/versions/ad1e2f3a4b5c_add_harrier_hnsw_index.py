"""add the Harrier 1024-dimensional memory HNSW index

Revision ID: ad1e2f3a4b5c
Revises: 9c0d1e2f3a4b
Create Date: 2026-08-19 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "ad1e2f3a4b5c"
down_revision: Union[str, Sequence[str], None] = "9c0d1e2f3a4b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "CREATE INDEX ix_memory_embeddings_harrier_hnsw "
            "ON memory_embeddings USING hnsw ((embedding::vector(1024)) vector_cosine_ops) "
            "WHERE model_id = 'harrier-oss-v1-0.6b-1024-v1' AND dimensions = 1024"
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP INDEX IF EXISTS ix_memory_embeddings_harrier_hnsw")
