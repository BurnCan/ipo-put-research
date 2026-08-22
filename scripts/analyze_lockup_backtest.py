import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))

from app.db import SessionLocal
from app.services.backtest import analyze_feature, build_backtest_dataset, classify_signal_persistence


def main():
    parser = argparse.ArgumentParser(description="Per-offset exploratory M7 signal analysis.")
    parser.add_argument("--feature", default="return_20d")
    parser.add_argument("--outcome", default="post_20d_return")
    parser.add_argument("--offset", type=int)
    parser.add_argument("--persistence", action="store_true")
    parser.add_argument("--ticker")
    parser.add_argument("--ipo-id", type=int)
    args = parser.parse_args()
    with SessionLocal() as db: rows = build_backtest_dataset(db, ticker=args.ticker, ipo_id=args.ipo_id)
    report = analyze_feature(rows, args.feature, args.outcome, args.offset)
    if args.persistence: report["persistence"] = classify_signal_persistence(rows, args.feature)
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__": main()
