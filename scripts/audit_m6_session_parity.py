#!/usr/bin/env python3
"""Print the read-only M6/canonical-session parity audit."""
import argparse
import csv
import json
import sys
from dataclasses import fields
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))

from app.db import SessionLocal
from app.services.event_analysis.session_parity import (audit_m6_session_parity,
                                                         mismatching_session_parity_rows,
                                                         summarize_session_parity)


def _json(value):
    if isinstance(value, date): return value.isoformat()
    raise TypeError(f"cannot serialize {type(value).__name__}")


def main():
    parser = argparse.ArgumentParser(description="Audit historical M6 sessions against canonical XNYS sessions.")
    parser.add_argument("--classification-status", default="classified")
    parser.add_argument("--candidate-type", default="operating_company_ipo")
    parser.add_argument("--offering-status", default="priced")
    parser.add_argument("--primary-lockup-only", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--ticker")
    parser.add_argument("--ipo-id", type=int)
    parser.add_argument("--lockup-id", type=int)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--details", action="store_true")
    parser.add_argument("--mismatches-only", action="store_true")
    parser.add_argument("--output", help="Optional detailed CSV path")
    args = parser.parse_args()
    query = {name: getattr(args, name) for name in (
        "classification_status", "candidate_type", "offering_status",
        "primary_lockup_only", "ticker", "ipo_id", "lockup_id", "limit")}
    with SessionLocal() as db:
        rows = audit_m6_session_parity(db, **query)
        result = summarize_session_parity(db, rows)
        selected = mismatching_session_parity_rows(rows) if args.mismatches_only else rows
        if args.details or args.mismatches_only:
            result["details"] = [r.to_dict() for r in selected]
        if args.output:
            path = Path(args.output); path.parent.mkdir(parents=True, exist_ok=True)
            payload = [r.to_dict() for r in selected]
            names = list(payload[0]) if payload else [f.name for f in fields(rows[0])] if rows else []
            with path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=names, extrasaction="ignore", lineterminator="\n")
                writer.writeheader()
                for item in payload:
                    writer.writerow({k: json.dumps(v, default=_json) if isinstance(v, (tuple, list)) else
                                     v.isoformat() if isinstance(v, date) else v for k, v in item.items()})
            result["csv_output"] = str(path)
    print(json.dumps(result, indent=2, sort_keys=True, default=_json))


if __name__ == "__main__": main()
