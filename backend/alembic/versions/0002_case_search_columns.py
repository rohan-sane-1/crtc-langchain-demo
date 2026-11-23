"""cases: applicant, province, intervention deadline, posted year, document links + search indexes

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-20
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    op.add_column("cases", sa.Column("applicant", sa.Text))
    op.add_column("cases", sa.Column("province", sa.Text))
    op.add_column("cases", sa.Column("intervention_deadline", sa.Date))
    op.add_column("cases", sa.Column("posted_year", sa.SmallInteger))
    op.add_column(
        "cases",
        sa.Column(
            "document_links",
            postgresql.JSONB,
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "cases",
        sa.Column(
            "has_documents",
            sa.Boolean,
            sa.Computed("jsonb_array_length(document_links) > 0", persisted=True),
        ),
    )

    # Existing rows get posted_year now; everything else is filled by the next list refresh.
    op.execute(
        "UPDATE cases SET posted_year = EXTRACT(YEAR FROM posted_date AT TIME ZONE 'UTC')::smallint"
    )
    # Mark the list cache as stale so the next request re-fetches and backfills the new columns
    # instead of serving rows with empty deadlines/applicants for up to CRTC_LIST_TTL_MINUTES.
    op.execute("UPDATE cases SET last_listed_at = NULL")

    op.create_index("ix_cases_status_deadline", "cases", ["status", "intervention_deadline"])
    op.create_index("ix_cases_posted_year", "cases", ["posted_year"])
    op.create_index("ix_cases_proceeding_type", "cases", ["proceeding_type"])
    op.create_index("ix_cases_province", "cases", ["province"])
    op.create_index("ix_cases_posted_date", "cases", ["posted_date"])
    op.create_index(
        "ix_cases_has_documents",
        "cases",
        ["has_documents"],
        postgresql_where=sa.text("has_documents"),
    )
    for col in ("title", "applicant", "notice_number"):
        op.create_index(
            f"ix_cases_{'notice' if col == 'notice_number' else col}_trgm",
            "cases",
            [col],
            postgresql_using="gin",
            postgresql_ops={col: "gin_trgm_ops"},
        )


def downgrade() -> None:
    for name in (
        "ix_cases_notice_trgm",
        "ix_cases_applicant_trgm",
        "ix_cases_title_trgm",
        "ix_cases_has_documents",
        "ix_cases_posted_date",
        "ix_cases_province",
        "ix_cases_proceeding_type",
        "ix_cases_posted_year",
        "ix_cases_status_deadline",
    ):
        op.drop_index(name, table_name="cases")
    for col in (
        "has_documents",
        "document_links",
        "posted_year",
        "intervention_deadline",
        "province",
        "applicant",
    ):
        op.drop_column("cases", col)
    # pg_trgm is left installed: other objects may depend on it.
