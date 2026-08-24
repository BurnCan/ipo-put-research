from datetime import UTC, date, datetime
from sqlalchemy import BigInteger, Boolean, CheckConstraint, Date, DateTime, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint
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
    securities: Mapped[list["Security"]] = relationship(back_populates="company", cascade="all, delete-orphan")


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
    primary_lockup_id: Mapped[int | None] = mapped_column(
        ForeignKey("ipo_lockups.id", ondelete="SET NULL", use_alter=True), nullable=True, index=True
    )
    primary_lockup_expiration_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)

    company: Mapped[Company] = relationship(back_populates="ipo")
    final_prospectus: Mapped[Filing | None] = relationship(foreign_keys=[final_prospectus_filing_id])
    facts: Mapped[list["IPOFact"]] = relationship(back_populates="ipo", cascade="all, delete-orphan")
    lockups: Mapped[list["IPOLockup"]] = relationship(
        back_populates="ipo", cascade="all, delete-orphan", foreign_keys="IPOLockup.ipo_id"
    )
    primary_lockup: Mapped["IPOLockup | None"] = relationship(foreign_keys=[primary_lockup_id], post_update=True)
    market_summary: Mapped["IPOMarketSummary | None"] = relationship(back_populates="ipo", uselist=False, cascade="all, delete-orphan")


class Security(Base):
    """A durable, provider-addressable identity separate from current issuer metadata."""
    __tablename__ = "securities"
    __table_args__ = (UniqueConstraint("company_id", "ticker", "source", name="uq_security_company_ticker_source"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    ticker: Mapped[str] = mapped_column(String(24), index=True)
    exchange: Mapped[str | None] = mapped_column(String(32), nullable=True)
    security_type: Mapped[str] = mapped_column(String(32), default="common_stock")
    valid_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    valid_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    source: Mapped[str] = mapped_column(String(32), default="sec_company")
    provider_symbol: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)
    company: Mapped[Company] = relationship(back_populates="securities")
    prices: Mapped[list["DailyPrice"]] = relationship(back_populates="security", cascade="all, delete-orphan")


class DailyPrice(Base):
    __tablename__ = "daily_prices"
    __table_args__ = (UniqueConstraint("security_id", "trade_date", "provider", name="uq_daily_price_identity"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    security_id: Mapped[int] = mapped_column(ForeignKey("securities.id", ondelete="CASCADE"), index=True)
    trade_date: Mapped[date] = mapped_column(Date, index=True)
    open: Mapped[float] = mapped_column(Numeric(20, 8))
    high: Mapped[float] = mapped_column(Numeric(20, 8))
    low: Mapped[float] = mapped_column(Numeric(20, 8))
    close: Mapped[float] = mapped_column(Numeric(20, 8))
    volume: Mapped[int] = mapped_column(BigInteger)
    adjusted_close: Mapped[float | None] = mapped_column(Numeric(20, 8), nullable=True)
    provider: Mapped[str] = mapped_column(String(32), index=True)
    provider_symbol: Mapped[str] = mapped_column(String(64))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)
    security: Mapped[Security] = relationship(back_populates="prices")


class IPOMarketSummary(Base):
    __tablename__ = "ipo_market_summary"
    id: Mapped[int] = mapped_column(primary_key=True)
    ipo_id: Mapped[int] = mapped_column(ForeignKey("ipos.id", ondelete="CASCADE"), unique=True, index=True)
    security_id: Mapped[int] = mapped_column(ForeignKey("securities.id", ondelete="CASCADE"), index=True)
    as_of_date: Mapped[date] = mapped_column(Date)
    first_trade_date: Mapped[date] = mapped_column(Date)
    first_day_open: Mapped[float] = mapped_column(Numeric(20, 8))
    first_day_close: Mapped[float] = mapped_column(Numeric(20, 8))
    latest_trade_date: Mapped[date] = mapped_column(Date)
    latest_close: Mapped[float] = mapped_column(Numeric(20, 8))
    post_ipo_high: Mapped[float] = mapped_column(Numeric(20, 8))
    first_day_close_return_vs_ipo_price: Mapped[float | None] = mapped_column(Numeric(20, 10), nullable=True)
    return_from_ipo_price: Mapped[float | None] = mapped_column(Numeric(20, 10), nullable=True)
    drawdown_from_post_ipo_high: Mapped[float] = mapped_column(Numeric(20, 10))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)
    ipo: Mapped[IPO] = relationship(back_populates="market_summary")
    security: Mapped[Security] = relationship()


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


class IPOLockup(Base):
    """One agreement-level lockup observation and its exact parser provenance."""
    __tablename__ = "ipo_lockups"
    __table_args__ = (
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_ipo_lockups_confidence"),
        UniqueConstraint("evidence_key", name="uq_ipo_lockups_evidence_key"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    ipo_id: Mapped[int] = mapped_column(ForeignKey("ipos.id", ondelete="CASCADE"), index=True)
    filing_id: Mapped[int] = mapped_column(ForeignKey("filings.id", ondelete="CASCADE"), index=True)
    holder_group: Mapped[str] = mapped_column(String(32), index=True)
    holder_group_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    lockup_type: Mapped[str] = mapped_column(String(32), index=True)
    duration_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stated_expiration_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    calculated_expiration_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    shares_locked: Mapped[float | None] = mapped_column(Numeric(18, 2), nullable=True)
    percentage_locked: Mapped[float | None] = mapped_column(Numeric(7, 4), nullable=True)
    percentage_is_derived: Mapped[bool] = mapped_column(Boolean, default=False)
    early_release_exists: Mapped[bool] = mapped_column(Boolean, default=False)
    early_release_terms: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[float] = mapped_column(Numeric(5, 4))
    parser_name: Mapped[str] = mapped_column(String(64))
    parser_version: Mapped[str] = mapped_column(String(32))
    source_excerpt: Mapped[str] = mapped_column(Text)
    source_locator: Mapped[str] = mapped_column(String(160))
    evidence_key: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    ipo: Mapped[IPO] = relationship(back_populates="lockups", foreign_keys=[ipo_id])
    filing: Mapped[Filing] = relationship()


class LockupSignalSnapshot(Base):
    """Versioned, point-in-time state observed before a lockup event."""
    __tablename__ = "lockup_signal_snapshots"
    __table_args__ = (UniqueConstraint("lockup_id", "security_id", "observation_offset", "snapshot_version",
                                       name="uq_lockup_snapshot_identity"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    ipo_id: Mapped[int] = mapped_column(ForeignKey("ipos.id", ondelete="CASCADE"), index=True)
    lockup_id: Mapped[int] = mapped_column(ForeignKey("ipo_lockups.id", ondelete="CASCADE"), index=True)
    security_id: Mapped[int] = mapped_column(ForeignKey("securities.id", ondelete="CASCADE"), index=True)
    observation_offset: Mapped[int] = mapped_column(Integer)
    observation_date: Mapped[date] = mapped_column(Date, index=True)
    data_cutoff_date: Mapped[date] = mapped_column(Date)
    event_date: Mapped[date] = mapped_column(Date)
    event_date_source: Mapped[str] = mapped_column(String(16))
    event_trade_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    snapshot_version: Mapped[str] = mapped_column(String(16))
    trading_sessions_to_event: Mapped[int] = mapped_column(Integer)
    lockup_duration_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    lockup_holder_group: Mapped[str | None] = mapped_column(String(32), nullable=True)
    lockup_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    lockup_confidence: Mapped[float | None] = mapped_column(Numeric(10, 6), nullable=True)
    ipo_price: Mapped[float | None] = mapped_column(Numeric(20, 8), nullable=True)
    primary_shares: Mapped[float | None] = mapped_column(Numeric(24, 4), nullable=True)
    secondary_shares: Mapped[float | None] = mapped_column(Numeric(24, 4), nullable=True)
    shares_offered: Mapped[float | None] = mapped_column(Numeric(24, 4), nullable=True)
    deal_size: Mapped[float | None] = mapped_column(Numeric(24, 4), nullable=True)
    secondary_share_fraction: Mapped[float | None] = mapped_column(Numeric(20, 10), nullable=True)
    days_since_ipo: Mapped[int | None] = mapped_column(Integer, nullable=True)
    trading_sessions_since_first_trade: Mapped[int] = mapped_column(Integer)
    available_history_sessions: Mapped[int] = mapped_column(Integer)
    close: Mapped[float] = mapped_column(Numeric(20, 8))
    return_from_ipo_price: Mapped[float | None] = mapped_column(Numeric(20, 10), nullable=True)
    return_5d: Mapped[float | None] = mapped_column(Numeric(20, 10), nullable=True)
    return_10d: Mapped[float | None] = mapped_column(Numeric(20, 10), nullable=True)
    return_20d: Mapped[float | None] = mapped_column(Numeric(20, 10), nullable=True)
    return_40d: Mapped[float | None] = mapped_column(Numeric(20, 10), nullable=True)
    post_ipo_high_to_date: Mapped[float] = mapped_column(Numeric(20, 8))
    drawdown_from_post_ipo_high: Mapped[float | None] = mapped_column(Numeric(20, 10), nullable=True)
    post_ipo_low_to_date: Mapped[float] = mapped_column(Numeric(20, 8))
    position_in_post_ipo_range: Mapped[float | None] = mapped_column(Numeric(20, 10), nullable=True)
    ipo_gain_retention: Mapped[float | None] = mapped_column(Numeric(20, 10), nullable=True)
    avg_volume_5d: Mapped[float | None] = mapped_column(Numeric(24, 4), nullable=True)
    avg_volume_20d: Mapped[float | None] = mapped_column(Numeric(24, 4), nullable=True)
    avg_volume_40d: Mapped[float | None] = mapped_column(Numeric(24, 4), nullable=True)
    volume_ratio_5d_to_20d: Mapped[float | None] = mapped_column(Numeric(20, 10), nullable=True)
    avg_dollar_volume_5d: Mapped[float | None] = mapped_column(Numeric(28, 4), nullable=True)
    avg_dollar_volume_20d: Mapped[float | None] = mapped_column(Numeric(28, 4), nullable=True)
    avg_down_day_volume_20d: Mapped[float | None] = mapped_column(Numeric(24, 4), nullable=True)
    avg_up_day_volume_20d: Mapped[float | None] = mapped_column(Numeric(24, 4), nullable=True)
    down_up_volume_ratio_20d: Mapped[float | None] = mapped_column(Numeric(20, 10), nullable=True)
    realized_vol_5d: Mapped[float | None] = mapped_column(Numeric(20, 10), nullable=True)
    realized_vol_20d: Mapped[float | None] = mapped_column(Numeric(20, 10), nullable=True)
    realized_vol_40d: Mapped[float | None] = mapped_column(Numeric(20, 10), nullable=True)
    avg_daily_range_20d: Mapped[float | None] = mapped_column(Numeric(20, 10), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class LockupEventAnalysis(Base):
    """Versioned event and future outcome state, kept separate from snapshots."""
    __tablename__ = "lockup_event_analysis"
    __table_args__ = (UniqueConstraint("lockup_id", "security_id", "outcome_version",
                                       name="uq_lockup_outcome_identity"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    ipo_id: Mapped[int] = mapped_column(ForeignKey("ipos.id", ondelete="CASCADE"), index=True)
    lockup_id: Mapped[int] = mapped_column(ForeignKey("ipo_lockups.id", ondelete="CASCADE"), index=True)
    security_id: Mapped[int] = mapped_column(ForeignKey("securities.id", ondelete="CASCADE"), index=True)
    event_date: Mapped[date] = mapped_column(Date)
    event_date_source: Mapped[str] = mapped_column(String(16))
    event_trade_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    outcome_version: Mapped[str] = mapped_column(String(16))
    as_of_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    max_post_event_session_available: Mapped[int | None] = mapped_column(Integer, nullable=True)
    event_status: Mapped[str] = mapped_column(String(32), index=True)
    previous_close: Mapped[float | None] = mapped_column(Numeric(20, 8), nullable=True)
    event_open: Mapped[float | None] = mapped_column(Numeric(20, 8), nullable=True)
    event_high: Mapped[float | None] = mapped_column(Numeric(20, 8), nullable=True)
    event_low: Mapped[float | None] = mapped_column(Numeric(20, 8), nullable=True)
    event_close: Mapped[float | None] = mapped_column(Numeric(20, 8), nullable=True)
    event_volume: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    event_gap_return: Mapped[float | None] = mapped_column(Numeric(20, 10), nullable=True)
    event_intraday_return: Mapped[float | None] = mapped_column(Numeric(20, 10), nullable=True)
    event_close_return: Mapped[float | None] = mapped_column(Numeric(20, 10), nullable=True)
    pre_20d_return: Mapped[float | None] = mapped_column(Numeric(20, 10), nullable=True)
    pre_10d_return: Mapped[float | None] = mapped_column(Numeric(20, 10), nullable=True)
    pre_5d_return: Mapped[float | None] = mapped_column(Numeric(20, 10), nullable=True)
    pre_1d_return: Mapped[float | None] = mapped_column(Numeric(20, 10), nullable=True)
    post_1d_return: Mapped[float | None] = mapped_column(Numeric(20, 10), nullable=True)
    post_5d_return: Mapped[float | None] = mapped_column(Numeric(20, 10), nullable=True)
    post_10d_return: Mapped[float | None] = mapped_column(Numeric(20, 10), nullable=True)
    post_20d_return: Mapped[float | None] = mapped_column(Numeric(20, 10), nullable=True)
    post_40d_return: Mapped[float | None] = mapped_column(Numeric(20, 10), nullable=True)
    bearish_mfe_5d: Mapped[float | None] = mapped_column(Numeric(20, 10), nullable=True)
    bearish_mae_5d: Mapped[float | None] = mapped_column(Numeric(20, 10), nullable=True)
    bearish_mfe_10d: Mapped[float | None] = mapped_column(Numeric(20, 10), nullable=True)
    bearish_mae_10d: Mapped[float | None] = mapped_column(Numeric(20, 10), nullable=True)
    bearish_mfe_20d: Mapped[float | None] = mapped_column(Numeric(20, 10), nullable=True)
    bearish_mae_20d: Mapped[float | None] = mapped_column(Numeric(20, 10), nullable=True)
    bearish_mfe_40d: Mapped[float | None] = mapped_column(Numeric(20, 10), nullable=True)
    bearish_mae_40d: Mapped[float | None] = mapped_column(Numeric(20, 10), nullable=True)
    baseline_avg_volume: Mapped[float | None] = mapped_column(Numeric(24, 4), nullable=True)
    event_volume_ratio: Mapped[float | None] = mapped_column(Numeric(20, 10), nullable=True)
    post_5d_avg_volume_ratio: Mapped[float | None] = mapped_column(Numeric(20, 10), nullable=True)
    post_10d_avg_volume_ratio: Mapped[float | None] = mapped_column(Numeric(20, 10), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class LockupProspectiveSignal(Base):
    """Immutable point-in-time M8 classification plus a later, separate outcome."""
    __tablename__ = "lockup_prospective_signals"
    __table_args__ = (
        UniqueConstraint("hypothesis_id", "hypothesis_version", "lockup_id", "evaluation_mode",
                         name="uq_prospective_hypothesis_lockup_mode"),
        Index("ix_prospective_hypothesis_status", "hypothesis_id", "signal_status"),
        Index("ix_prospective_event_date", "event_date"),
        Index("ix_prospective_group", "interaction_group"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    hypothesis_id: Mapped[str] = mapped_column(String(80), index=True)
    hypothesis_version: Mapped[str] = mapped_column(String(32))
    ipo_id: Mapped[int] = mapped_column(ForeignKey("ipos.id", ondelete="CASCADE"), index=True)
    lockup_id: Mapped[int] = mapped_column(ForeignKey("ipo_lockups.id", ondelete="CASCADE"), index=True)
    security_id: Mapped[int | None] = mapped_column(ForeignKey("securities.id", ondelete="CASCADE"), nullable=True)
    observation_offset: Mapped[int] = mapped_column(Integer)
    observation_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    required_observation_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    calendar_id: Mapped[str | None] = mapped_column(String(16), nullable=True)
    calendar_provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    calendar_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    event_date: Mapped[date] = mapped_column(Date)
    event_trade_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    feature1_name: Mapped[str] = mapped_column(String(40))
    feature1_value: Mapped[float | None] = mapped_column(Numeric(20, 10), nullable=True)
    feature1_threshold: Mapped[float] = mapped_column(Numeric(20, 10))
    feature2_name: Mapped[str] = mapped_column(String(40))
    feature2_value: Mapped[float | None] = mapped_column(Numeric(20, 10), nullable=True)
    feature2_threshold: Mapped[float] = mapped_column(Numeric(20, 10))
    feature1_side: Mapped[str | None] = mapped_column(String(8), nullable=True)
    feature2_side: Mapped[str | None] = mapped_column(String(8), nullable=True)
    interaction_group: Mapped[str | None] = mapped_column(String(20), nullable=True)
    is_high_high: Mapped[bool] = mapped_column(Boolean, default=False)
    signal_status: Mapped[str] = mapped_column(String(32), index=True)
    unavailable_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    evaluation_mode: Mapped[str] = mapped_column(
        String(24), default="strict_prospective", server_default="strict_prospective", index=True)
    realized_outcome_name: Mapped[str | None] = mapped_column(String(40), nullable=True)
    realized_outcome_value: Mapped[float | None] = mapped_column(Numeric(20, 10), nullable=True)
    outcome_observation_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    outcome_attached_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    bearish_mfe_20d: Mapped[float | None] = mapped_column(Numeric(20, 10), nullable=True)
    bearish_mae_20d: Mapped[float | None] = mapped_column(Numeric(20, 10), nullable=True)
    # Immutable signal-lock provenance.  Unlike ``updated_at``, this value is
    # assigned once at admission and is never changed by lifecycle reruns.
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


Index("ix_ipo_status_first_filing", IPO.status, IPO.first_filing_date)
