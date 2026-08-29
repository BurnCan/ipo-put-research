#!/usr/bin/env python3
"""Explicitly record an observed historical provider backfill attempt."""
import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Security
from app.services.market_data.coverage import record_backfill_attempt


def parse_date(value: str) -> date:
    return date.fromisoformat(value)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    identity = parser.add_mutually_exclusive_group(required=True)
    identity.add_argument("--security-id", type=int)
    identity.add_argument("--ticker")
    parser.add_argument("--provider", required=True)
    parser.add_argument("--start-date", required=True, type=parse_date)
    parser.add_argument("--end-date", required=True, type=parse_date)
    parser.add_argument("--status", required=True,
                        choices=("success", "no_data", "partial", "error"))
    parser.add_argument("--error-message")
    args = parser.parse_args()
    with SessionLocal() as db:
        if args.security_id is not None:
            security = db.get(Security, args.security_id)
        else:
            matches = db.scalars(select(Security).where(Security.ticker == args.ticker)).all()
            if len(matches) > 1:
                parser.error("ticker is ambiguous; use --security-id")
            security = matches[0] if matches else None
        if security is None:
            parser.error("security not found")
        row = record_backfill_attempt(
            db, security, args.provider, args.start_date, args.end_date, args.status,
            error_message=args.error_message)
        print(json.dumps({"id": row.id, "security_id": row.security_id,
                          "ticker": security.ticker, "provider": row.provider,
                          "requested_start_date": row.requested_start_date.isoformat(),
                          "requested_end_date": row.requested_end_date.isoformat(),
                          "status": row.status, "bars_returned": row.bars_returned,
                          "bars_created": row.bars_created,
                          "bars_updated": row.bars_updated}, sort_keys=True))


if __name__ == "__main__":
    main()
