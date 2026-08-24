import argparse, json, sys
from datetime import date, datetime
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))
from app.db import SessionLocal
from app.services.prospective import update_prospective_lockup_signals


def json_default(value):
    """Encode date-like report values at the CLI's JSON boundary."""
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def main():
    p = argparse.ArgumentParser(description="Advance frozen M8 prospective observations from stored M6 rows.")
    p.add_argument("--hypothesis-id", required=True)
    p.add_argument("--evaluation-mode", choices=("strict_prospective", "shadow_prospective"),
                   default="strict_prospective")
    p.add_argument("--classification-status", default="classified")
    p.add_argument("--candidate-type", default="operating_company_ipo")
    p.add_argument("--offering-status", default="priced")
    p.add_argument("--primary-lockup-only", action="store_true", default=True)
    p.add_argument("--ticker"); p.add_argument("--ipo-id", type=int); p.add_argument("--limit", type=int)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--as-of-date", type=date.fromisoformat,
                   help="Inject lifecycle date (YYYY-MM-DD); defaults to today")
    with SessionLocal() as db: report = update_prospective_lockup_signals(db, **vars(p.parse_args()))
    print(json.dumps(report.to_dict(), indent=2, sort_keys=True, default=json_default))
if __name__ == "__main__": main()
