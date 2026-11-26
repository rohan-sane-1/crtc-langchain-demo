"""knowledge-base sync: documents.blocked status + provenance columns

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-23

* ``document_status`` gains ``blocked`` (the server or robots.txt refused us).
* ``documents`` gains kind, detected_type, bytes, numbering, source_signature, note,
  attempted_at (``mime``, ``sha256``, ``n_paragraphs`` etc. already exist from 0001).
* ``cases.record_fingerprint`` / ``last_synced_at`` already exist from 0001.

The downgrade maps any ``blocked`` rows to ``skipped`` and rebuilds the enum without it.
"""

import sqlalchemy as sa

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A new enum value can't be used in the transaction that adds it, so commit it first.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE document_status ADD VALUE IF NOT EXISTS 'blocked'")

    op.add_column("documents", sa.Column("kind", sa.Text))
    op.add_column("documents", sa.Column("detected_type", sa.Text))
    op.add_column("documents", sa.Column("bytes", sa.BigInteger))
    op.add_column("documents", sa.Column("numbering", sa.Text))
    op.add_column("documents", sa.Column("source_signature", sa.Text))
    op.add_column("documents", sa.Column("note", sa.Text))
    op.add_column("documents", sa.Column("attempted_at", sa.DateTime(timezone=True)))
    op.create_check_constraint(
        "ck_documents_numbering",
        "documents",
        "numbering IS NULL OR numbering IN ('source', 'synthetic')",
    )
    op.create_index("ix_documents_case_status", "documents", ["case_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_documents_case_status", table_name="documents")
    op.drop_constraint("ck_documents_numbering", "documents", type_="check")
    for col in (
        "attempted_at",
        "note",
        "source_signature",
        "numbering",
        "bytes",
        "detected_type",
        "kind",
    ):
        op.drop_column("documents", col)

    op.execute("UPDATE documents SET status = 'skipped' WHERE status = 'blocked'")
    op.execute("ALTER TABLE documents ALTER COLUMN status DROP DEFAULT")
    op.execute("ALTER TYPE document_status RENAME TO document_status_old")
    op.execute("CREATE TYPE document_status AS ENUM ('pending', 'embedded', 'failed', 'skipped')")
    op.execute(
        "ALTER TABLE documents ALTER COLUMN status TYPE document_status "
        "USING status::text::document_status"
    )
    op.execute("ALTER TABLE documents ALTER COLUMN status SET DEFAULT 'pending'")
    op.execute("DROP TYPE document_status_old")
