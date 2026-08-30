"""Print the read-only M9A v1 prospective evaluation as JSON."""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db import SessionLocal
from app.services.prospective.m9a_evaluation import evaluate_m9a_prospective


def main():
    parser = argparse.ArgumentParser(description="Evaluate persisted M8 evidence (read only).")
    parser.add_argument("--evaluation-mode", choices=("strict_prospective", "shadow_prospective"),
                        default="strict_prospective")
    args = parser.parse_args()
    with SessionLocal() as db:
        result = evaluate_m9a_prospective(db, evaluation_mode=args.evaluation_mode)
    print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
