from datetime import date
from pydantic import BaseModel, ConfigDict


class IPORead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    cik: str
    company_name: str
    ticker: str | None = None
    exchange: str | None = None
    status: str
    first_filing_date: date | None = None
    ipo_date: date | None = None
    ipo_price: float | None = None
    shares_offered: float | None = None
    primary_shares: float | None = None
    secondary_shares: float | None = None
    shares_outstanding_post_ipo: float | None = None
    deal_size: float | None = None
    document_cached: bool = False
    document_sha256: str | None = None
    fact_count: int = 0
    locked_shares: float | None = None
    unlock_date: date | None = None
    primary_lockup_expiration_date: date | None = None
    filing_count: int = 0
    candidate_type: str = "unknown"
    classification_status: str = "unclassified"
    offering_status: str = "filed"
    classification_reason: str | None = None
    final_prospectus: dict | None = None
