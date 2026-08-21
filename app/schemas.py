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
    locked_shares: float | None = None
    unlock_date: date | None = None
    filing_count: int = 0
