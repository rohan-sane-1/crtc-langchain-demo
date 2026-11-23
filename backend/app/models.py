import enum
import uuid
from datetime import date, datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    Computed,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.config import get_settings
from app.db import Base

EMBEDDING_DIM = get_settings().embedding_dim


class DocumentStatus(str, enum.Enum):
    pending = "pending"
    embedded = "embedded"
    failed = "failed"
    skipped = "skipped"


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    google_sub: Mapped[str] = mapped_column(Text, unique=True)
    email: Mapped[str] = mapped_column(Text)
    name: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Case(Base):
    __tablename__ = "cases"
    __table_args__ = (
        Index("ix_cases_status_deadline", "status", "intervention_deadline"),
        Index("ix_cases_posted_year", "posted_year"),
        Index("ix_cases_proceeding_type", "proceeding_type"),
        Index("ix_cases_province", "province"),
        Index("ix_cases_posted_date", "posted_date"),
        Index(
            "ix_cases_has_documents",
            "has_documents",
            postgresql_where=text("has_documents"),
        ),
        # Trigram GIN indexes make the ILIKE '%word%' search filters index-assisted.
        Index(
            "ix_cases_title_trgm",
            "title",
            postgresql_using="gin",
            postgresql_ops={"title": "gin_trgm_ops"},
        ),
        Index(
            "ix_cases_applicant_trgm",
            "applicant",
            postgresql_using="gin",
            postgresql_ops={"applicant": "gin_trgm_ops"},
        ),
        Index(
            "ix_cases_notice_trgm",
            "notice_number",
            postgresql_using="gin",
            postgresql_ops={"notice_number": "gin_trgm_ops"},
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    notice_number: Mapped[str] = mapped_column(Text, unique=True)
    title: Mapped[str | None] = mapped_column(Text)
    sector: Mapped[str | None] = mapped_column(Text)
    proceeding_type: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str | None] = mapped_column(Text)
    posted_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_url: Mapped[str | None] = mapped_column(Text)
    last_listed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    record_fingerprint: Mapped[str | None] = mapped_column(Text)
    # Added in migration 0002 (Phase 2 follow-up)
    applicant: Mapped[str | None] = mapped_column(Text)
    province: Mapped[str | None] = mapped_column(Text)
    intervention_deadline: Mapped[date | None] = mapped_column(Date)
    posted_year: Mapped[int | None] = mapped_column(SmallInteger)
    document_links: Mapped[list] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb"), default=list
    )
    has_documents: Mapped[bool] = mapped_column(
        Boolean, Computed("jsonb_array_length(document_links) > 0", persisted=True)
    )


class Chat(Base):
    """`chats.id` IS the LangGraph thread_id."""

    __tablename__ = "chats"
    __table_args__ = (Index("ix_chats_user_id_updated_at", "user_id", "updated_at"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    case_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("cases.id"))
    title: Mapped[str] = mapped_column(Text, default="New chat")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    case_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"))
    source_url: Mapped[str] = mapped_column(Text, unique=True)
    title: Mapped[str | None] = mapped_column(Text)
    filer: Mapped[str | None] = mapped_column(Text)
    filed_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    mime: Mapped[str | None] = mapped_column(Text)
    sha256: Mapped[str | None] = mapped_column(Text)
    status: Mapped[DocumentStatus] = mapped_column(
        Enum(DocumentStatus, name="document_status"), default=DocumentStatus.pending
    )
    error: Mapped[str | None] = mapped_column(Text)
    n_paragraphs: Mapped[int | None] = mapped_column(Integer)
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    chunks: Mapped[list["Chunk"]] = relationship(cascade="all, delete-orphan")


class Chunk(Base):
    __tablename__ = "chunks"
    __table_args__ = (
        Index(
            "ix_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        Index("ix_chunks_tsv", "tsv", postgresql_using="gin"),
        Index("ix_chunks_case_id", "case_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    case_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"))
    para_start: Mapped[int] = mapped_column(Integer)
    para_end: Mapped[int] = mapped_column(Integer)
    page: Mapped[int | None] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM))
    tsv: Mapped[str] = mapped_column(
        TSVECTOR, Computed("to_tsvector('english', text)", persisted=True)
    )
