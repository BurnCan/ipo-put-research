import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db import SessionLocal
from app.services.lockup_processing import process_cached_lockups

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Extract lockups from cached final prospectus text")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--ipo-id", type=int)
    parser.add_argument("--reparse", action="store_true")
    parser.add_argument("--classification-status")
    parser.add_argument("--candidate-type")
    parser.add_argument("--offering-status")
    args = parser.parse_args()
    with SessionLocal() as db:
        print(process_cached_lockups(
            db, limit=args.limit, ipo_id=args.ipo_id, reparse=args.reparse,
            classification_status=args.classification_status,
            candidate_type=args.candidate_type, offering_status=args.offering_status,
        ))
