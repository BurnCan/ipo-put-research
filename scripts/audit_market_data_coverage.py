#!/usr/bin/env python3
"""Read-only canonical XNYS/DailyPrice coverage audit."""
import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))

from sqlalchemy import func, select
from app.db import SessionLocal
from app.models import Company, IPO, IPOLockup, Security
from app.services.market_data.coverage import coverage, plan_lockup_coverage


def json_default(value):
    if isinstance(value, date): return value.isoformat()
    raise TypeError(type(value).__name__)


def selected_rows(db, args):
    stmt = (select(IPO, Company, IPOLockup, Security).join(Company, Company.id == IPO.company_id)
            .join(IPOLockup, IPOLockup.ipo_id == IPO.id)
            .join(Security, (Security.company_id == Company.id) & Security.is_primary))
    if args.ticker: stmt = stmt.where(func.upper(Security.ticker) == args.ticker.upper())
    if args.ipo_id: stmt = stmt.where(IPO.id == args.ipo_id)
    if args.lockup_id: stmt = stmt.where(IPOLockup.id == args.lockup_id)
    if args.classification_status: stmt = stmt.where(IPO.classification_status == args.classification_status)
    if args.candidate_type: stmt = stmt.where(IPO.candidate_type == args.candidate_type)
    if args.offering_status: stmt = stmt.where(IPO.offering_status == args.offering_status)
    if args.primary_lockup_only: stmt = stmt.where(IPOLockup.id == IPO.primary_lockup_id)
    return db.execute(stmt.order_by(Security.ticker, IPOLockup.id)).all()


def add_filters(parser):
    parser.add_argument('--ticker'); parser.add_argument('--ipo-id', type=int); parser.add_argument('--lockup-id', type=int)
    parser.add_argument('--classification-status'); parser.add_argument('--candidate-type'); parser.add_argument('--offering-status')
    parser.add_argument('--primary-lockup-only', action='store_true')
    parser.add_argument('--start-date', type=date.fromisoformat); parser.add_argument('--end-date', type=date.fromisoformat)
    parser.add_argument('--as-of-date', type=date.fromisoformat,
                        help='classify missing sessions relative to YYYY-MM-DD')
    parser.add_argument('--lockup-required-range', action='store_true'); parser.add_argument('--details', action='store_true')


def coverage_tasks(rows, args, planner=None):
    """Return executable coverage tasks and lockups that cannot be planned.

    An explicit range is security-scoped, so identical work selected through
    multiple lockups is collapsed.  Lockup-derived ranges remain lockup-scoped.
    """
    planner = planner or plan_lockup_coverage
    tasks, skipped, seen = [], [], set()
    for ipo, company, lockup, security in rows:
        if args.start_date:
            start, end = args.start_date, args.end_date
            key = (security.id, start, end)
            if key in seen:
                continue
            seen.add(key)
        else:
            try:
                plan = planner(lockup)
            except ValueError as exc:
                if str(exc) != 'lockup has no known event date':
                    raise
                skipped.append({'ipo_id': ipo.id, 'lockup_id': lockup.id,
                                'ticker': security.ticker,
                                'status': 'no_known_event_date'})
                continue
            start, end = plan.coverage_start, plan.coverage_end
        tasks.append((ipo, lockup, security, start, end))
    return tasks, skipped


def main():
    parser = argparse.ArgumentParser(description=__doc__); add_filters(parser); args = parser.parse_args()
    if bool(args.start_date) != bool(args.end_date): parser.error('--start-date and --end-date must be supplied together')
    if not args.start_date and not args.lockup_required_range: parser.error('supply a date range or --lockup-required-range')
    results, skipped = [], []
    with SessionLocal() as db:
        rows = selected_rows(db, args)
        tasks, skipped = coverage_tasks(rows, args)
        for ipo, lockup, security, start, end in tasks:
            item = coverage(db, security, start, end, as_of_date=args.as_of_date).to_dict()
            item.update(ipo_id=ipo.id, lockup_id=lockup.id)
            results.append(item)
    summary = {'lockups_selected': len(rows), 'lockups_plannable': len(rows) - len(skipped),
               'lockups_skipped_no_event_date': len(skipped),
               'securities_seen': len(results), 'securities_complete': sum(x['coverage_complete'] for x in results),
               'securities_with_gaps': sum(bool(x['fetchable_missing_sessions']) for x in results),
               'securities_future_sessions_only': sum(
                   bool(x['future_missing_sessions']) and not x['fetchable_missing_sessions'] for x in results),
               'expected_sessions': sum(x['expected_session_count'] for x in results),
               'stored_expected_sessions': sum(x['stored_expected_session_count'] for x in results),
               'missing_sessions_total': sum(x['missing_sessions_total'] for x in results),
               'fetchable_missing_sessions': sum(len(x['fetchable_missing_sessions']) for x in results),
               'future_missing_sessions': sum(len(x['future_missing_sessions']) for x in results)}
    if args.details or len(results) + len(skipped) <= 10: summary['details'] = results + skipped
    print(json.dumps(summary, indent=2, sort_keys=True, default=json_default))

if __name__ == '__main__': main()
