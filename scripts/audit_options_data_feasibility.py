#!/usr/bin/env python3
"""Audit Massive historical options feasibility without writing database state."""
import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import settings
from app.db import SessionLocal
from app.services.market_data.options_feasibility import (MassiveOptionsProbe,
                                                          audit_options_feasibility)


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tickers", nargs="*", help="space- or comma-separated cohort tickers")
    parser.add_argument("--limit", type=int, default=12)
    parser.add_argument("--as-of-date", type=date.fromisoformat, default=date.today())
    parser.add_argument("--output", type=Path, help="optional path for the JSON report")
    return parser


def _tickers(values):
    return tuple(dict.fromkeys(part.strip().upper() for value in (values or ())
                              for part in value.split(",") if part.strip()))


def main():
    args = build_parser().parse_args()
    if args.limit < 1 or args.limit > 50:
        raise SystemExit("--limit must be between 1 and 50")
    if settings.market_data_provider != "massive":
        raise SystemExit("M9B currently audits the configured Massive provider only")
    probe = MassiveOptionsProbe(settings.massive_api_key or "")
    with SessionLocal() as db:
        report = audit_options_feasibility(db, probe, as_of_date=args.as_of_date,
                                           limit=args.limit, tickers=_tickers(args.tickers))
    rendered = json.dumps(report, indent=2, sort_keys=True, allow_nan=False)
    print(f"M9B classification: {report['classification']}")
    print(f"Provider: {report['provider']} | sampled names: {report['sample_size']}")
    print("Historical contract discovery: " + str(report["historical_contract_discovery_supported"]))
    print("Historical T-5 bid/ask: " + str(report["historical_bid_ask_supported"]))
    print("Historical later valuation: " + str(report["historical_followup_valuation_supported"]))
    print("T-5 optionability: "
          f"optionable={report['optionable_at_t5_count']}, "
          f"not_optionable={report['not_optionable_at_t5_count']}, "
          f"unknown={report['optionability_unknown_count']}")
    for item in report["names"]:
        print(f"- {item['ticker']} T-5 {item['t5_date']}: "
              f"optionable={item['optionable_at_t5']}, "
              f"contracts={item['contract_discovery_status']}, "
              f"quotes={item['historical_quote_status']}, "
              f"follow-up={item['later_valuation_status']}, "
              f"error={item['error_category'] or 'none'}")
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
        print(f"JSON report written to {args.output}")


if __name__ == "__main__":
    main()
