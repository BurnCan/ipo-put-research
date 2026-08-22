import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))

from app.db import SessionLocal
from app.services.backtest import build_backtest_dataset, export_backtest_csv


def main():
    parser = argparse.ArgumentParser(description="Export stored M6 snapshots and outcomes as the M7 dataset.")
    parser.add_argument("--output", default="data/backtests/lockup_signal_outcomes.csv")
    parser.add_argument("--classification-status", default="classified")
    parser.add_argument("--candidate-type", default="operating_company_ipo")
    parser.add_argument("--offering-status", default="priced")
    parser.add_argument("--primary-lockup-only", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--ticker")
    parser.add_argument("--ipo-id", type=int)
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    output = args.output
    delattr(args, "output")
    with SessionLocal() as db: rows = build_backtest_dataset(db, **vars(args))
    path = export_backtest_csv(rows, output)
    print(json.dumps({"output": str(path), "n_observations": len(rows),
                      "n_events": len({r["lockup_id"] for r in rows}),
                      "rows_are_repeated_measures_by_lockup": True}, sort_keys=True))


if __name__ == "__main__": main()
