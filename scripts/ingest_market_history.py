import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.db import SessionLocal
from app.services.market_history import create_provider, ingest_market_history


def main() -> None:
    parser = argparse.ArgumentParser(description="Incrementally ingest normalized daily market history")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--ipo-id", type=int)
    parser.add_argument("--ticker")
    parser.add_argument("--classification-status")
    parser.add_argument("--candidate-type")
    parser.add_argument("--offering-status")
    parser.add_argument(
        "--primary-lockup-only", action="store_true",
        help="Only ingest IPOs with a selected primary lockup and expiration date",
    )
    parser.add_argument("--sleep", type=float, default=12.0, help="Seconds between provider requests")
    parser.add_argument(
        "--refresh", action="store_true",
        help="Refetch and upsert only the recent correction window (default: MARKET_REFRESH_DAYS)",
    )
    parser.add_argument(
        "--refresh-days", type=int,
        help="Number of calendar days re-fetched by --refresh (overrides MARKET_REFRESH_DAYS)",
    )
    args = parser.parse_args()
    provider = create_provider()
    with SessionLocal() as db:
        report = ingest_market_history(db, provider, limit=args.limit, ipo_id=args.ipo_id,
                                       ticker=args.ticker, sleep_seconds=args.sleep, refresh=args.refresh,
                                       refresh_days=args.refresh_days,
                                       classification_status=args.classification_status,
                                       candidate_type=args.candidate_type,
                                       offering_status=args.offering_status,
                                       primary_lockup_only=args.primary_lockup_only)
    print(report.to_dict())


if __name__ == "__main__":
    main()
