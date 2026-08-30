#!/usr/bin/env python3
"""Audit exact T-5 21-session feature readiness (not M6 v2 completeness)."""
import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))

from app.config import settings
from app.db import SessionLocal
from app.services.market_data.t5_readiness import audit_t5_signal_readiness


def json_default(value):
    if isinstance(value, date): return value.isoformat()
    raise TypeError(f"cannot serialize {type(value).__name__}")


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--classification-status", default="classified")
    parser.add_argument("--candidate-type", default="operating_company_ipo")
    parser.add_argument("--offering-status", default="priced")
    parser.add_argument("--primary-lockup-only", action=argparse.BooleanOptionalAction,
                        default=True)
    parser.add_argument("--ticker"); parser.add_argument("--ipo-id", type=int)
    parser.add_argument("--lockup-id", type=int); parser.add_argument("--limit", type=int)
    parser.add_argument("--as-of-date", type=date.fromisoformat, default=date.today())
    return parser


def main():
    args = build_parser().parse_args()
    query = vars(args)
    with SessionLocal() as db:
        report = audit_t5_signal_readiness(
            db, provider=settings.market_data_provider, **query)
    print(json.dumps(report, indent=2, sort_keys=True, default=json_default))


if __name__ == "__main__": main()
