from datetime import date, datetime
from sqlalchemy import Date, DateTime, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.db import Base


class Company(Base):
    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(primary_key=True)
    cik: Mapped[str] = mapped_column(String(10), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255), index=True)
    ticker: Mapped[str | None] = mapped_column(String(24), nullable=True, index=True)
    exchange: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    company: Mapped[Company] = relationship(back_populates="filings")


class IPO(Base):
    __tablename__ = "ipos"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(32), default="filed", index=True)
    first_filing_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    ipo_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    ipo_price: Mapped[float | None] = mapped_column(Numeric(12, 4), nullable=True)
    shares_offered: Mapped[float | None] = mapped_column(Numeric(18, 2), nullable=True)
    deal_size: Mapped[float | None] = mapped_column(Numeric(18, 2), nullable=True)
    initial_float: Mapped[float | None] = mapped_column(Numeric(18, 2), nullable=True)
    locked_shares: Mapped[float | None] = mapped_column(Numeric(18, 2), nullable=True)
    unlock_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    company: Mapped[Company] = relationship(back_populates="ipo")


Index("ix_ipo_status_first_filing", IPO.status, IPO.first_filing_date)
