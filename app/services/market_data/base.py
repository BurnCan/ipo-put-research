from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Protocol


@dataclass(frozen=True)
class DailyBar:
    trade_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    adjusted_close: Decimal | None = None


class MarketDataError(RuntimeError):
    """A provider request failed after its bounded retries."""


class ProviderConfigurationError(MarketDataError):
    """Market ingestion was requested without usable provider configuration."""


class MarketDataProvider(Protocol):
    name: str

    def get_daily_history(self, symbol: str, start_date: date, end_date: date) -> list[DailyBar]: ...
