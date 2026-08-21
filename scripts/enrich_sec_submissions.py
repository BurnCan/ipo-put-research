import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.db import Base, SessionLocal, engine
from app.services.company_enrichment import enrich_companies_from_sec


def main():
    parser = argparse.ArgumentParser(description="Enrich discovered companies from SEC submissions JSON")
    parser.add_argument("--limit", type=int, help="maximum number of companies to process")
    parser.add_argument("--sleep", type=float, default=0.12, help="seconds between issuer requests")
    args = parser.parse_args()
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        print(enrich_companies_from_sec(db, limit=args.limit, sleep=max(0, args.sleep)))


if __name__ == "__main__":
    main()
