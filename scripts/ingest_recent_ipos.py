import argparse
import sys
from pathlib import Path

# Allow the script to be run directly from a fresh clone without setting
# PYTHONPATH manually.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.db import Base, SessionLocal, engine
from app.services.ipo_ingest import ingest_registration_filings


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=365)
    args = parser.parse_args()

    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        result = ingest_registration_filings(db, days=args.days)
    print(result)


if __name__ == "__main__":
    main()
