#!/usr/bin/env python3
"""Explicit targeted canonical-session market-data backfill."""
import argparse
import json
import sys
from datetime import date
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))
from app.db import SessionLocal
from app.services.market_history import create_provider
from app.services.market_data.coverage import backfill_missing_sessions, plan_lockup_coverage
from scripts.audit_market_data_coverage import add_filters, coverage_tasks, json_default, selected_rows


def main():
    parser = argparse.ArgumentParser(description=__doc__); add_filters(parser)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--dry-run', action='store_true'); action.add_argument('--execute', action='store_true')
    parser.add_argument('--retry-known-no-data', action='store_true',
                        help='request provider ranges again even when no-data provenance exists')
    parser.set_defaults(details=True); args = parser.parse_args()
    if bool(args.start_date) != bool(args.end_date): parser.error('--start-date and --end-date must be supplied together')
    if not args.start_date and not args.lockup_required_range: parser.error('supply a date range or --lockup-required-range')
    provider = create_provider(); details, skipped = [], []
    with SessionLocal() as db:
        rows = selected_rows(db, args)
        tasks, skipped = coverage_tasks(rows, args, planner=plan_lockup_coverage)
        for ipo, lockup, security, start, end in tasks:
            kwargs = dict(as_of_date=args.as_of_date, dry_run=args.dry_run)
            # Omit the default keyword for compatibility with service wrappers.
            if args.retry_known_no_data:
                kwargs['retry_known_no_data'] = True
            result = backfill_missing_sessions(db, provider, security, start, end, **kwargs)
            item = result.to_dict()
            item.update(ipo_id=ipo.id, lockup_id=lockup.id)
            details.append(item)
    summary = {'lockups_selected': len(rows), 'lockups_plannable': len(rows) - len(skipped),
               'lockups_skipped_no_event_date': len(skipped),
               'securities_seen': len(details), 'securities_complete': sum(not x['coverage_before']['missing_sessions'] for x in details),
               'securities_with_gaps': sum(bool(x['coverage_before']['fetchable_missing_sessions']) for x in details),
               'securities_future_sessions_only': sum(
                   bool(x['coverage_before']['future_missing_sessions'])
                   and not x['coverage_before']['fetchable_missing_sessions'] for x in details),
               'expected_sessions': sum(len(x['coverage_before']['expected_sessions']) for x in details),
               'stored_expected_sessions': sum(x['coverage_before']['stored_expected_session_count'] for x in details),
               'missing_sessions_total': sum(x['coverage_before']['missing_sessions_total'] for x in details),
               'fetchable_missing_sessions': sum(len(x['coverage_before']['fetchable_missing_sessions']) for x in details),
               'future_missing_sessions': sum(len(x['coverage_before']['future_missing_sessions']) for x in details),
               'known_no_data_sessions': sum(x.get('known_no_data_sessions', 0) for x in details),
               'known_no_data_ranges': sum(x.get('known_no_data_ranges', 0) for x in details),
               'provider_requests_skipped_known_no_data': sum(x.get('provider_requests_skipped_known_no_data', 0) for x in details),
               'attempt_records_created': sum(x.get('attempt_records_created', 0) for x in details),
               'provider_requests': sum(x['provider_requests'] for x in details), 'bars_fetched': sum(x['bars_fetched'] for x in details),
               'bars_created': sum(x['bars_created'] for x in details), 'bars_updated': sum(x['bars_updated'] for x in details),
               'provider_no_data': sum(x['provider_no_data'] for x in details), 'provider_errors': sum(x['provider_errors'] for x in details),
               'missing_sessions_after': sum(len(x['coverage_after']['missing_sessions']) for x in details),
               'coverage_completed': sum(not x['coverage_after']['missing_sessions'] for x in details),
               'coverage_still_incomplete': sum(bool(x['coverage_after']['missing_sessions']) for x in details),
               'details': details + skipped}
    print(json.dumps(summary, indent=2, sort_keys=True, default=json_default))
if __name__ == '__main__': main()
