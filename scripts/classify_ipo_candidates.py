import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.db import SessionLocal
from app.services.ipo_classification import classify_ipo_candidates


def main():
    parser = argparse.ArgumentParser(description="Classify IPO candidates from stored SEC filing metadata")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--company-id", type=int)
    parser.add_argument("--ipo-id", type=int)
    args = parser.parse_args()
    with SessionLocal() as db:
        print(classify_ipo_candidates(db, limit=args.limit, company_id=args.company_id, ipo_id=args.ipo_id))


if __name__ == "__main__":
    main()
