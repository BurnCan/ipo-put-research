import time
from datetime import UTC, date, datetime
from decimal import Decimal
from urllib.parse import quote

import httpx

from app.services.market_data.base import DailyBar, MarketDataError, ProviderConfigurationError


class MassiveMarketDataProvider:
    """Massive adapter; no response-specific representation escapes this class."""

    name = "massive"
    base_url = "https://api.massive.com"
    transient_statuses = {408, 429, 500, 502, 503, 504}

    def __init__(self, api_key: str | None, *, client: httpx.Client | None = None,
                 max_retries: int = 3, backoff_seconds: float = 1.0, sleep_fn=time.sleep):
        if not api_key or not api_key.strip():
            raise ProviderConfigurationError("MASSIVE_API_KEY is required for market-data ingestion")
        self.api_key = api_key.strip()
        self.client = client or httpx.Client(timeout=30)
        self.max_retries = max_retries
        self.backoff_seconds = backoff_seconds
        self.sleep_fn = sleep_fn

    def get_daily_history(self, symbol: str, start_date: date, end_date: date) -> list[DailyBar]:
        url = f"{self.base_url}/v2/aggs/ticker/{quote(symbol, safe='')}/range/1/day/{start_date.isoformat()}/{end_date.isoformat()}"
        params = {"adjusted": "false", "sort": "asc", "limit": 50000, "apiKey": self.api_key}
        response = None
        for attempt in range(self.max_retries + 1):
            try:
                response = self.client.get(url, params=params)
                if response.status_code not in self.transient_statuses:
                    response.raise_for_status()
                    break
            except httpx.RequestError as exc:
                if attempt >= self.max_retries:
                    raise MarketDataError(f"Massive request failed for {symbol}: {exc}") from exc
            if attempt >= self.max_retries:
                status = response.status_code if response is not None else "network error"
                raise MarketDataError(f"Massive request failed for {symbol} after retries (HTTP {status})")
            retry_after = response.headers.get("Retry-After") if response is not None else None
            delay = float(retry_after) if retry_after and retry_after.isdigit() else self.backoff_seconds * (2 ** attempt)
            self.sleep_fn(delay)
        payload = response.json()
        bars = []
        for item in payload.get("results") or []:
            trade_date = datetime.fromtimestamp(item["t"] / 1000, tz=UTC).date()
            bars.append(DailyBar(
                trade_date=trade_date, open=Decimal(str(item["o"])), high=Decimal(str(item["h"])),
                low=Decimal(str(item["l"])), close=Decimal(str(item["c"])), volume=int(item["v"]),
                adjusted_close=None,
            ))
        return bars
