import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db import SessionLocal
from app.services.event_analysis import recompute_lockup_analyses


def main():
    parser = argparse.ArgumentParser(description="Compute lockup analysis from stored database bars (offline).")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--ipo-id", type=int)
    parser.add_argument("--lockup-id", type=int)
    parser.add_argument("--ticker")
    parser.add_argument("--classification-status")
    parser.add_argument("--candidate-type")
    parser.add_argument("--offering-status")
    parser.add_argument(
        "--primary-lockup-only",
        action="store_true",
        help="Require a selected primary lockup and stored primary expiration date",
    )
    parser.add_argument("--recompute", action="store_true")
    args = parser.parse_args()
    with SessionLocal() as db:
        report = recompute_lockup_analyses(db, **vars(args))
    print(json.dumps(report.to_dict(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
