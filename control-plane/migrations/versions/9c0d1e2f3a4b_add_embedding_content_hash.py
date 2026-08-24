"""track the memory content represented by each embedding

Revision ID: 9c0d1e2f3a4b
Revises: 8b9c0d1e2f3a
Create Date: 2026-08-17 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "9c0d1e2f3a4b"
down_revision: Union[str, Sequence[str], None] = "8b9c0d1e2f3a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "memory_embeddings",
        sa.Column("content_hash", sa.String(length=64), nullable=True),
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            """
            UPDATE memory_embeddings
            SET content_hash = memory_records.content_hash
            FROM memory_records
            WHERE memory_records.id = memory_embeddings.memory_id
            """
        )
    else:
        op.execute(
            """
            UPDATE memory_embeddings
            SET content_hash = (
                SELECT content_hash FROM memory_records
                WHERE memory_records.id = memory_embeddings.memory_id
            )
            """
        )
    with op.batch_alter_table("memory_embeddings") as batch_op:
        batch_op.alter_column("content_hash", existing_type=sa.String(length=64), nullable=False)
        batch_op.create_index("ix_memory_embeddings_content_hash", ["content_hash"])


def downgrade() -> None:
    with op.batch_alter_table("memory_embeddings") as batch_op:
        batch_op.drop_index("ix_memory_embeddings_content_hash")
        batch_op.drop_column("content_hash")
