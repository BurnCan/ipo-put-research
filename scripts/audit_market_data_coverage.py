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
    parser.add_argument('--lockup-required-range', action='store_true'); parser.add_argument('--details', action='store_true')


def main():
    parser = argparse.ArgumentParser(description=__doc__); add_filters(parser); args = parser.parse_args()
    if bool(args.start_date) != bool(args.end_date): parser.error('--start-date and --end-date must be supplied together')
    if not args.start_date and not args.lockup_required_range: parser.error('supply a date range or --lockup-required-range')
    details = []
    with SessionLocal() as db:
        for ipo, company, lockup, security in selected_rows(db, args):
            plan = plan_lockup_coverage(lockup)
            start, end = (args.start_date, args.end_date) if args.start_date else (plan.coverage_start, plan.coverage_end)
            item = coverage(db, security, start, end).to_dict(); item.update(ipo_id=ipo.id, lockup_id=lockup.id)
            details.append(item)
    summary = {'securities_seen': len(details), 'securities_complete': sum(x['coverage_complete'] for x in details),
               'securities_with_gaps': sum(not x['coverage_complete'] for x in details),
               'expected_sessions': sum(x['expected_session_count'] for x in details),
               'stored_expected_sessions': sum(x['stored_expected_session_count'] for x in details),
               'missing_sessions': sum(x['missing_session_count'] for x in details)}
    if args.details or len(details) <= 10: summary['details'] = details
    print(json.dumps(summary, indent=2, sort_keys=True, default=json_default))

if __name__ == '__main__': main()
