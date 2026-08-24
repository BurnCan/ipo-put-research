from datetime import date
from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import Company, DailyPrice, IPO, IPOLockup, Security
from app.services.market_calendar import session_offset, sessions_in_range
from app.services.market_data.base import DailyBar
from app.services.market_data.coverage import (
    backfill_missing_sessions, coverage, feature_window_coverage,
    plan_lockup_coverage,
)


def fixture():
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    Base.metadata.create_all(engine); db = Session(engine)
    company = Company(cik='0000000001', name='Sparse Co', ticker='SPRS')
    ipo = IPO(company=company, first_filing_date=date(2025, 1, 1))
    security = Security(company=company, ticker='SPRS', provider_symbol='SPRS', is_primary=True)
    lockup = IPOLockup(ipo=ipo, filing_id=1, holder_group='all', lockup_type='standard',
                      stated_expiration_date=date(2025, 1, 11), confidence=Decimal('1'),
                      parser_name='test', parser_version='1', source_excerpt='x',
                      source_locator='x', evidence_key='coverage-test')
    db.add_all([ipo, security, lockup]); db.commit()
    return db, security, lockup


def add_price(db, security, day, provider='fake'):
    db.add(DailyPrice(security_id=security.id, trade_date=day, provider=provider,
                      provider_symbol=security.ticker, open=10, high=11, low=9,
                      close=10, volume=100)); db.commit()


class Provider:
    name = 'fake'
    def __init__(self, bars=(), error=False): self.bars, self.error, self.calls = bars, error, []
    def get_daily_history(self, symbol, start, end):
        self.calls.append((symbol, start, end))
        if self.error: raise RuntimeError('offline fixture failure')
        return [bar for bar in self.bars if start <= bar.trade_date <= end]


def bar(day):
    return DailyBar(day, Decimal('10'), Decimal('11'), Decimal('9'), Decimal('10'), 100)


def test_range_alignment_missing_and_non_session_reporting():
    db, security, _ = fixture()
    expected = sessions_in_range(date(2025, 1, 4), date(2025, 1, 12))
    assert expected == tuple(date(2025, 1, day) for day in (6, 7, 8, 10))
    for day in expected[1:]: add_price(db, security, day)
    add_price(db, security, date(2025, 1, 11))
    result = coverage(db, security, date(2025, 1, 4), date(2025, 1, 12))
    assert result.canonical_start_session == date(2025, 1, 6)
    assert result.canonical_end_session == date(2025, 1, 10)
    assert result.missing_sessions == (date(2025, 1, 6),)
    assert result.non_session_stored_dates == (date(2025, 1, 11),)


def test_lockup_plan_and_exact_feature_window():
    db, security, lockup = fixture(); plan = plan_lockup_coverage(lockup)
    assert plan.event_session == date(2025, 1, 13)
    assert plan.earliest_required_snapshot_session == session_offset(plan.event_session, -60)
    assert plan.earliest_feature_session == session_offset(plan.earliest_required_snapshot_session, -20)
    observation = date(2025, 1, 10)
    required = tuple(session_offset(observation, n) for n in range(-20, 1))
    for day in required: add_price(db, security, day)
    assert feature_window_coverage(db, security, observation).complete
    db.delete(db.scalar(select(DailyPrice).where(DailyPrice.trade_date == required[5]))); db.commit()
    assert feature_window_coverage(db, security, observation).missing_sessions == (required[5],)


def test_dry_run_future_cap_success_idempotency_and_no_data():
    db, security, _ = fixture(); start, end = date(2025, 1, 6), date(2025, 1, 10)
    wanted = sessions_in_range(start, end); provider = Provider([bar(day) for day in wanted])
    dry = backfill_missing_sessions(db, provider, security, start, end, dry_run=True)
    assert not provider.calls and not db.scalars(select(DailyPrice)).all() and dry.request_ranges
    result = backfill_missing_sessions(db, provider, security, start, end, as_of_date=date(2025, 1, 8))
    assert result.bars_created == 3 and all(call[2] <= date(2025, 1, 8) for call in provider.calls)
    result = backfill_missing_sessions(db, provider, security, start, end)
    assert result.status == 'complete' and result.bars_created == 1
    again = backfill_missing_sessions(db, provider, security, start, end)
    assert again.provider_requests == 0 and again.bars_created == 0
    missing_day = wanted[0]; db.delete(db.scalar(select(DailyPrice).where(DailyPrice.trade_date == missing_day))); db.commit()
    empty = backfill_missing_sessions(db, Provider(), security, start, end)
    assert empty.status == 'provider_no_data' and empty.coverage_after.missing_sessions == (missing_day,)


def test_provider_error_preserves_canonical_gap():
    db, security, _ = fixture()
    result = backfill_missing_sessions(db, Provider(error=True), security,
                                       date(2025, 1, 6), date(2025, 1, 6))
    assert result.status == 'provider_error'
    assert result.coverage_after.missing_sessions == (date(2025, 1, 6),)


def test_canonical_coverage_accepts_another_provider_and_preserves_provenance_view():
    db, security, _ = fixture()
    day = date(2025, 1, 6)
    add_price(db, security, day, provider='old-provider')

    assert coverage(db, security, day, day).coverage_complete
    provider_view = coverage(db, security, day, day, provider='new-provider')
    assert provider_view.missing_sessions == (day,)

    new_provider = Provider([bar(day)])
    result = backfill_missing_sessions(db, new_provider, security, day, day,
                                       as_of_date=day)
    assert result.status == 'complete'
    assert result.provider_requests == 0
    assert db.query(DailyPrice).count() == 1


def test_future_only_and_mixed_missing_sessions_are_classified_and_not_fetched():
    db, security, _ = fixture()
    start, end, cutoff = date(2025, 1, 6), date(2025, 1, 10), date(2025, 1, 8)
    wanted = sessions_in_range(start, end)
    provider = Provider([bar(day) for day in wanted])

    mixed = backfill_missing_sessions(db, provider, security, start, end,
                                      as_of_date=cutoff, dry_run=True)
    assert mixed.coverage_before.missing_sessions_total == 4
    assert mixed.coverage_before.fetchable_missing_sessions == wanted[:3]
    assert mixed.coverage_before.future_missing_sessions == wanted[3:]
    assert mixed.request_ranges == ((start, cutoff),)
    assert not provider.calls

    executed = backfill_missing_sessions(db, provider, security, start, end,
                                         as_of_date=cutoff)
    assert provider.calls == [('SPRS', start, cutoff)]
    assert executed.bars_created == 3
    assert executed.status == 'future_sessions_only'

    provider.calls.clear()
    future_only = backfill_missing_sessions(db, provider, security, date(2025, 1, 10), end,
                                            as_of_date=cutoff)
    assert future_only.status == 'future_sessions_only'
    assert future_only.request_ranges == ()
    assert provider.calls == []


def test_backfill_cli_honors_as_of_date(monkeypatch, capsys):
    import scripts.backfill_market_data_gaps as cli

    captured = {}
    class DbContext:
        def __enter__(self): return object()
        def __exit__(self, *args): return None
    db_context = DbContext()
    monkeypatch.setattr(cli, 'SessionLocal', lambda: db_context)
    monkeypatch.setattr(cli, 'create_provider', lambda: Provider())
    monkeypatch.setattr(cli, 'selected_rows', lambda db, args: [
        (SimpleNamespace(id=1), SimpleNamespace(), SimpleNamespace(id=2),
         SimpleNamespace(id=3))])
    monkeypatch.setattr(cli, 'plan_lockup_coverage', lambda lockup: SimpleNamespace(
        coverage_start=date(2025, 1, 6), coverage_end=date(2025, 1, 10)))

    before = SimpleNamespace(to_dict=lambda: {
        'missing_sessions': (), 'missing_sessions_total': 0,
        'fetchable_missing_sessions': (), 'future_missing_sessions': (),
        'expected_sessions': (), 'stored_expected_session_count': 0})
    fake_result = SimpleNamespace(to_dict=lambda: {
        'status': 'complete', 'coverage_before': before.to_dict(),
        'coverage_after': before.to_dict(), 'request_ranges': (),
        'provider_requests': 0, 'bars_fetched': 0, 'bars_created': 0,
        'bars_updated': 0, 'provider_no_data': 0, 'provider_errors': 0})

    def fake_backfill(db, provider, security, start, end, **kwargs):
        captured.update(kwargs)
        return fake_result

    monkeypatch.setattr(cli, 'backfill_missing_sessions', fake_backfill)
    monkeypatch.setattr('sys.argv', ['backfill_market_data_gaps.py', '--ticker', 'SPRS',
                                    '--lockup-required-range', '--as-of-date', '2025-01-08',
                                    '--dry-run'])
    cli.main()
    capsys.readouterr()
    assert captured == {'as_of_date': date(2025, 1, 8), 'dry_run': True}


def test_coverage_tasks_skip_unknown_lockup_and_keep_valid_work():
    from scripts.audit_market_data_coverage import coverage_tasks

    args = SimpleNamespace(start_date=None, end_date=None)
    security = SimpleNamespace(id=10, ticker='SPRS')
    rows = [
        (SimpleNamespace(id=1), SimpleNamespace(), SimpleNamespace(id=2), security),
        (SimpleNamespace(id=1), SimpleNamespace(), SimpleNamespace(id=3), security),
    ]

    def planner(lockup):
        if lockup.id == 3:
            raise ValueError('lockup has no known event date')
        return SimpleNamespace(coverage_start=date(2025, 1, 6),
                               coverage_end=date(2025, 1, 10))

    tasks, skipped = coverage_tasks(rows, args, planner)
    assert [(task[1].id, task[3], task[4]) for task in tasks] == [
        (2, date(2025, 1, 6), date(2025, 1, 10))]
    assert skipped == [{'ipo_id': 1, 'lockup_id': 3, 'ticker': 'SPRS',
                        'status': 'no_known_event_date'}]


def test_explicit_coverage_tasks_need_no_event_date_and_deduplicate_security():
    from scripts.audit_market_data_coverage import coverage_tasks

    args = SimpleNamespace(start_date=date(2025, 2, 3), end_date=date(2025, 2, 7))
    security = SimpleNamespace(id=10, ticker='SPRS')
    rows = [
        (SimpleNamespace(id=1), SimpleNamespace(), SimpleNamespace(id=2), security),
        (SimpleNamespace(id=1), SimpleNamespace(), SimpleNamespace(id=3), security),
    ]

    def unexpected_planner(lockup):
        raise AssertionError('explicit ranges must not plan lockup coverage')

    tasks, skipped = coverage_tasks(rows, args, unexpected_planner)
    assert len(tasks) == 1
    assert tasks[0][3:] == (date(2025, 2, 3), date(2025, 2, 7))
    assert skipped == []


def test_audit_cli_reports_unknown_lockup_without_aborting(monkeypatch, capsys):
    import scripts.audit_market_data_coverage as cli

    class DbContext:
        def __enter__(self): return object()
        def __exit__(self, *args): return None

    security = SimpleNamespace(id=10, ticker='SPRS')
    rows = [
        (SimpleNamespace(id=1), SimpleNamespace(), SimpleNamespace(id=2), security),
        (SimpleNamespace(id=1), SimpleNamespace(), SimpleNamespace(id=3), security),
    ]
    monkeypatch.setattr(cli, 'SessionLocal', DbContext)
    monkeypatch.setattr(cli, 'selected_rows', lambda db, args: rows)
    monkeypatch.setattr(cli, 'plan_lockup_coverage', lambda lockup: None)

    def planner(lockup):
        if lockup.id == 3:
            raise ValueError('lockup has no known event date')
        return SimpleNamespace(coverage_start=date(2025, 1, 6),
                               coverage_end=date(2025, 1, 6))

    original_coverage_tasks = cli.coverage_tasks
    monkeypatch.setattr(cli, 'coverage_tasks',
                        lambda selected, args: original_coverage_tasks(selected, args, planner))
    monkeypatch.setattr(cli, 'coverage', lambda *args, **kwargs: SimpleNamespace(
        to_dict=lambda: {'coverage_complete': True, 'fetchable_missing_sessions': (),
                         'future_missing_sessions': (), 'expected_session_count': 1,
                         'stored_expected_session_count': 1, 'missing_sessions_total': 0}))
    monkeypatch.setattr('sys.argv', ['audit_market_data_coverage.py', '--ticker', 'SPRS',
                                    '--lockup-required-range', '--details'])
    cli.main()
    output = __import__('json').loads(capsys.readouterr().out)
    assert output['lockups_selected'] == 2
    assert output['lockups_plannable'] == 1
    assert output['lockups_skipped_no_event_date'] == 1
    assert output['securities_seen'] == 1
    assert output['details'][-1]['status'] == 'no_known_event_date'


def test_backfill_cli_dry_run_skips_unknown_lockup(monkeypatch, capsys):
    import scripts.backfill_market_data_gaps as cli

    class DbContext:
        def __enter__(self): return object()
        def __exit__(self, *args): return None

    row = (SimpleNamespace(id=1), SimpleNamespace(), SimpleNamespace(id=2),
           SimpleNamespace(id=10, ticker='SPRS'))
    monkeypatch.setattr(cli, 'SessionLocal', DbContext)
    monkeypatch.setattr(cli, 'create_provider', lambda: Provider())
    monkeypatch.setattr(cli, 'selected_rows', lambda db, args: [row])
    monkeypatch.setattr(cli, 'plan_lockup_coverage',
                        lambda lockup: (_ for _ in ()).throw(
                            ValueError('lockup has no known event date')))
    monkeypatch.setattr(cli, 'backfill_missing_sessions',
                        lambda *args, **kwargs: (_ for _ in ()).throw(
                            AssertionError('skipped lockup must not be backfilled')))
    monkeypatch.setattr('sys.argv', ['backfill_market_data_gaps.py', '--ticker', 'SPRS',
                                    '--lockup-required-range', '--dry-run'])
    cli.main()
    output = __import__('json').loads(capsys.readouterr().out)
    assert output['lockups_skipped_no_event_date'] == 1
    assert output['details'] == [{'ipo_id': 1, 'lockup_id': 2,
                                  'status': 'no_known_event_date', 'ticker': 'SPRS'}]


def test_selected_rows_primary_lockup_only():
    from scripts.audit_market_data_coverage import selected_rows

    db, security, primary = fixture()
    ancillary = IPOLockup(
        ipo=primary.ipo, filing_id=2, holder_group='directors', lockup_type='ancillary',
        confidence=Decimal('1'), parser_name='test', parser_version='1',
        source_excerpt='x', source_locator='x', evidence_key='coverage-secondary')
    db.add(ancillary); db.flush()
    primary.ipo.primary_lockup_id = primary.id
    db.commit()
    base = dict(ticker='SPRS', ipo_id=None, lockup_id=None,
                classification_status=None, candidate_type=None, offering_status=None)
    all_rows = selected_rows(db, SimpleNamespace(**base, primary_lockup_only=False))
    primary_rows = selected_rows(db, SimpleNamespace(**base, primary_lockup_only=True))
    assert [row[2].id for row in all_rows] == [primary.id, ancillary.id]
    assert [row[2].id for row in primary_rows] == [primary.id]
