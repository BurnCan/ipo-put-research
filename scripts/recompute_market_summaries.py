import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import settings
from app.db import SessionLocal
from app.services.market_summary import recompute_market_summaries


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Recompute market summaries offline from stored daily prices"
    )
    parser.add_argument("--limit", type=int)
    parser.add_argument("--ipo-id", type=int)
    parser.add_argument("--ticker")
    args = parser.parse_args()

    with SessionLocal() as db:
        report = recompute_market_summaries(
            db, settings.market_data_provider, limit=args.limit,
            ipo_id=args.ipo_id, ticker=args.ticker,
        )
    print(report.to_dict())


if __name__ == "__main__":
    main()
