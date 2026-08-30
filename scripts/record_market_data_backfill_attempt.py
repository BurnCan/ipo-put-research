#!/usr/bin/env python3
"""Reconcile historical market-data attempt provenance without fetching data.

This command only validates and records an already-observed provider request.
It never constructs a provider or calls an external provider API.
"""
import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import func, select

from app.config import settings
from app.db import SessionLocal
from app.models import Security
from app.services.market_data.coverage import (
    equivalent_backfill_attempt, record_backfill_attempt_if_missing,
)


def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected YYYY-MM-DD") from exc


def nonnegative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return parsed


def reconcile_attempt(db, *, ticker: str, provider: str, start_date: date,
                      end_date: date, status: str, bars_returned: int | None = None,
                      bars_created: int | None = None, bars_updated: int | None = None,
                      error_message: str | None = None, dry_run: bool = False) -> dict:
    """Validate and reconcile one attempt; this function performs no data fetch."""
    ticker = ticker.strip().upper()
    provider = provider.strip()
    if not ticker:
        raise ValueError("ticker is required")
    if not provider:
        raise ValueError("provider identity is unavailable")
    if start_date > end_date:
        raise ValueError("start date must not be after end date")
    if status not in {"success", "no_data", "partial", "error"}:
        raise ValueError("status must be success, no_data, partial, or error")
    matches = db.scalars(select(Security).where(
        func.upper(Security.ticker) == ticker)).all()
    if not matches:
        raise ValueError(f"unknown ticker: {ticker}")
    if len(matches) != 1:
        raise ValueError(f"ticker is ambiguous: {ticker}")
    security = matches[0]
    existing = equivalent_backfill_attempt(
        db, security, provider, start_date, end_date, status)
    created = False
    row = existing
    if existing is None and not dry_run:
        row, created = record_backfill_attempt_if_missing(
            db, security, provider, start_date, end_date, status,
            bars_returned=bars_returned, bars_created=bars_created,
            bars_updated=bars_updated, error_message=error_message)
    result = {
        "status": ("created" if created else
                   "already_present" if row is not None else "would_create"),
        "ticker": security.ticker, "security_id": security.id,
        "provider": provider, "requested_start_date": start_date.isoformat(),
        "requested_end_date": end_date.isoformat(), "attempt_status": status,
        "dry_run": dry_run,
    }
    if row is not None:
        result["attempt_id"] = row.id
    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog="PROVENANCE ONLY: no market-data provider request is made.")
    parser.add_argument("--ticker", required=True,
                        help="unique ticker of the connected Security row")
    parser.add_argument("--provider", default=settings.market_data_provider,
                        help="provider identity (default: configured MARKET_DATA_PROVIDER)")
    parser.add_argument("--start-date", required=True, type=parse_date)
    parser.add_argument("--end-date", required=True, type=parse_date)
    parser.add_argument("--status", required=True,
                        choices=("success", "no_data", "partial", "error"))
    parser.add_argument("--bars-returned", type=nonnegative_int)
    parser.add_argument("--bars-created", type=nonnegative_int)
    parser.add_argument("--bars-updated", type=nonnegative_int)
    parser.add_argument("--error-message")
    parser.add_argument("--dry-run", action="store_true",
                        help="validate and report without changing the database")
    args = parser.parse_args()
    try:
        with SessionLocal() as db:
            result = reconcile_attempt(db, **vars(args))
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
