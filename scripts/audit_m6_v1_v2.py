#!/usr/bin/env python3
"""Emit the read-only persisted M6 v1/v2 parity report as JSON."""
import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db import SessionLocal
from app.services.event_analysis.m6_parity_audit import (DEFAULT_ATOL, DEFAULT_RTOL,
                                                         audit_m6_v1_v2)


def _json(value):
    if isinstance(value, date):
        return value.isoformat()
    raise TypeError(f"cannot serialize {type(value).__name__}")


def main():
    parser = argparse.ArgumentParser(description="Read-only audit of persisted M6 v1 and canonical v2 snapshots.")
    parser.add_argument("--classification-status", default="classified")
    parser.add_argument("--candidate-type", default="operating_company_ipo")
    parser.add_argument("--offering-status", default="priced")
    parser.add_argument("--primary-lockup-only", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--ticker")
    parser.add_argument("--ipo-id", type=int)
    parser.add_argument("--lockup-id", type=int)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--max-examples", type=int, default=25)
    parser.add_argument("--atol", type=float, default=DEFAULT_ATOL)
    parser.add_argument("--rtol", type=float, default=DEFAULT_RTOL)
    args = parser.parse_args()
    if args.max_examples < 0 or args.atol < 0 or args.rtol < 0:
        parser.error("max-examples and numeric tolerances must be non-negative")
    with SessionLocal() as db:
        result = audit_m6_v1_v2(db, **vars(args))
    print(json.dumps(result, indent=2, sort_keys=True, default=_json))


if __name__ == "__main__":
    main()
