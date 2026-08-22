from datetime import UTC, date, datetime
from sqlalchemy import Boolean, CheckConstraint, Date, DateTime, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.db import Base


def utc_now() -> datetime:
    """Return an aware UTC timestamp (unlike deprecated ``datetime.utcnow``)."""
    return datetime.now(UTC)


class Company(Base):
    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(primary_key=True)
    cik: Mapped[str] = mapped_column(String(10), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255), index=True)
    ticker: Mapped[str | None] = mapped_column(String(24), nullable=True, index=True)
    exchange: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)

    filings: Mapped[list["Filing"]] = relationship(back_populates="company", cascade="all, delete-orphan")
    ipo: Mapped["IPO | None"] = relationship(back_populates="company", uselist=False, cascade="all, delete-orphan")


class Filing(Base):
    __tablename__ = "filings"
    __table_args__ = (UniqueConstraint("accession_number", name="uq_filings_accession"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    form_type: Mapped[str] = mapped_column(String(24), index=True)
    filed_at: Mapped[date] = mapped_column(Date, index=True)
    accession_number: Mapped[str] = mapped_column(String(32), index=True)
    filing_path: Mapped[str] = mapped_column(Text)
    sec_url: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(32), default="sec_edgar")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)

    company: Mapped[Company] = relationship(back_populates="filings")
    document: Mapped["FilingDocument | None"] = relationship(back_populates="filing", uselist=False, cascade="all, delete-orphan")


class IPO(Base):
    __tablename__ = "ipos"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(32), default="filed", index=True)
    first_filing_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    ipo_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    ipo_price: Mapped[float | None] = mapped_column(Numeric(12, 4), nullable=True)
    shares_offered: Mapped[float | None] = mapped_column(Numeric(18, 2), nullable=True)
    primary_shares: Mapped[float | None] = mapped_column(Numeric(18, 2), nullable=True)
    secondary_shares: Mapped[float | None] = mapped_column(Numeric(18, 2), nullable=True)
    shares_outstanding_post_ipo: Mapped[float | None] = mapped_column(Numeric(18, 2), nullable=True)
    deal_size: Mapped[float | None] = mapped_column(Numeric(18, 2), nullable=True)
    initial_float: Mapped[float | None] = mapped_column(Numeric(18, 2), nullable=True)
    locked_shares: Mapped[float | None] = mapped_column(Numeric(18, 2), nullable=True)
    unlock_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    candidate_type: Mapped[str] = mapped_column(String(32), default="unknown", server_default="unknown", index=True)
    classification_status: Mapped[str] = mapped_column(String(32), default="unclassified", server_default="unclassified", index=True)
    offering_status: Mapped[str] = mapped_column(String(32), default="filed", server_default="filed", index=True)
    classification_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    final_prospectus_filing_id: Mapped[int | None] = mapped_column(
        ForeignKey("filings.id", ondelete="SET NULL"), nullable=True, index=True
    )

    company: Mapped[Company] = relationship(back_populates="ipo")
    final_prospectus: Mapped[Filing | None] = relationship(foreign_keys=[final_prospectus_filing_id])
    facts: Mapped[list["IPOFact"]] = relationship(back_populates="ipo", cascade="all, delete-orphan")


class FilingDocument(Base):
    __tablename__ = "filing_documents"
    id: Mapped[int] = mapped_column(primary_key=True)
    filing_id: Mapped[int] = mapped_column(ForeignKey("filings.id", ondelete="CASCADE"), unique=True, index=True)
    source_url: Mapped[str] = mapped_column(Text)
    fetch_status: Mapped[str] = mapped_column(String(20), index=True)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    content_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    raw_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    text_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    byte_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)
    filing: Mapped[Filing] = relationship(back_populates="document")


class IPOFact(Base):
    __tablename__ = "ipo_facts"
    __table_args__ = (
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_ipo_facts_confidence"),
        UniqueConstraint("ipo_id", "filing_id", "field_name", "parser_name", "parser_version", "value_key", name="uq_ipo_fact_identity"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    ipo_id: Mapped[int] = mapped_column(ForeignKey("ipos.id", ondelete="CASCADE"), index=True)
    filing_id: Mapped[int] = mapped_column(ForeignKey("filings.id", ondelete="CASCADE"), index=True)
    field_name: Mapped[str] = mapped_column(String(64), index=True)
    value_numeric: Mapped[float | None] = mapped_column(Numeric(24, 4), nullable=True)
    value_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    value_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    value_key: Mapped[str] = mapped_column(String(255))
    unit: Mapped[str | None] = mapped_column(String(32), nullable=True)
    confidence: Mapped[float] = mapped_column(Numeric(5, 4))
    parser_name: Mapped[str] = mapped_column(String(64))
    parser_version: Mapped[str] = mapped_column(String(32))
    source_excerpt: Mapped[str | None] = mapped_column(String(500), nullable=True)
    source_locator: Mapped[str | None] = mapped_column(String(128), nullable=True)
    is_derived: Mapped[bool] = mapped_column(Boolean, default=False)
    derivation: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    ipo: Mapped[IPO] = relationship(back_populates="facts")
    filing: Mapped[Filing] = relationship()


Index("ix_ipo_status_first_filing", IPO.status, IPO.first_filing_date)
