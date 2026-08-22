"""Canonical M7 dataset, assembled exclusively from stored M6 derived rows.

Rows are repeated measures keyed by ``(lockup_id, observation_offset)``.  In
particular, the number of rows is not the number of independent IPO events.
"""
from __future__ import annotations

import csv
from datetime import date
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select

from app.models import Company, IPO, IPOLockup, LockupEventAnalysis, LockupSignalSnapshot
from app.services.event_analysis.constants import OUTCOME_VERSION, SNAPSHOT_VERSION

IDENTITY_COLUMNS = ("ipo_id", "lockup_id", "security_id", "company_name", "ticker")
PROVENANCE_COLUMNS = ("snapshot_version", "outcome_version", "event_date_source", "data_cutoff_date")
FEATURE_COLUMNS = (
    "observation_offset", "observation_date", "close", "return_from_ipo_price",
    "return_5d", "return_10d", "return_20d", "return_40d",
    "drawdown_from_post_ipo_high", "position_in_post_ipo_range", "ipo_gain_retention",
    "avg_volume_5d", "avg_volume_20d", "avg_volume_40d", "volume_ratio_5d_to_20d",
    "avg_dollar_volume_5d", "avg_dollar_volume_20d", "down_up_volume_ratio_20d",
    "realized_vol_5d", "realized_vol_20d", "realized_vol_40d", "avg_daily_range_20d",
    "available_history_sessions", "trading_sessions_to_event", "days_since_ipo",
    "lockup_duration_days", "lockup_holder_group", "lockup_type", "lockup_confidence",
    "ipo_price", "primary_shares", "secondary_shares", "shares_offered", "deal_size",
    "secondary_share_fraction", "shares_outstanding_post_ipo",
    "ipo_date", "lockup_expiration_date",
)
OUTCOME_COLUMNS = (
    "event_date", "event_trade_date", "event_status", "max_post_event_session_available",
    "event_gap_return", "event_intraday_return", "event_close_return",
    "post_1d_return", "post_5d_return", "post_10d_return", "post_20d_return", "post_40d_return",
    "bearish_mfe_5d", "bearish_mae_5d", "bearish_mfe_10d", "bearish_mae_10d",
    "bearish_mfe_20d", "bearish_mae_20d", "bearish_mfe_40d", "bearish_mae_40d",
    "event_volume_ratio", "post_5d_avg_volume_ratio", "post_10d_avg_volume_ratio",
)
AVAILABILITY_COLUMNS = ("has_ipo_price", "has_20d_history", "has_40d_history",
                        "has_post_5d", "has_post_10d", "has_post_20d", "has_post_40d")
CSV_COLUMNS = IDENTITY_COLUMNS + FEATURE_COLUMNS + OUTCOME_COLUMNS + PROVENANCE_COLUMNS + AVAILABILITY_COLUMNS


def _scalar(value):
    if isinstance(value, Decimal):
        return float(value)
    return value


def build_backtest_dataset(db, *, classification_status="classified",
                           candidate_type="operating_company_ipo", offering_status="priced",
                           primary_lockup_only=True, ticker=None, ipo_id=None, limit=None):
    """Return deterministic normalized dictionaries; filters are applied before limit."""
    stmt = (select(LockupSignalSnapshot, LockupEventAnalysis, IPO, Company)
            .join(IPO, IPO.id == LockupSignalSnapshot.ipo_id)
            .join(Company, Company.id == IPO.company_id)
            .join(IPOLockup, IPOLockup.id == LockupSignalSnapshot.lockup_id)
            .outerjoin(LockupEventAnalysis,
                       (LockupEventAnalysis.lockup_id == LockupSignalSnapshot.lockup_id) &
                       (LockupEventAnalysis.security_id == LockupSignalSnapshot.security_id) &
                       (LockupEventAnalysis.outcome_version == OUTCOME_VERSION))
            .where(LockupSignalSnapshot.snapshot_version == SNAPSHOT_VERSION))
    if primary_lockup_only:
        stmt = stmt.where(LockupSignalSnapshot.lockup_id == IPO.primary_lockup_id,
                          IPO.primary_lockup_id.is_not(None),
                          IPO.primary_lockup_expiration_date.is_not(None))
    if classification_status is not None: stmt = stmt.where(IPO.classification_status == classification_status)
    if candidate_type is not None: stmt = stmt.where(IPO.candidate_type == candidate_type)
    if offering_status is not None: stmt = stmt.where(IPO.offering_status == offering_status)
    if ticker: stmt = stmt.where(Company.ticker.ilike(ticker.strip()))
    if ipo_id is not None: stmt = stmt.where(IPO.id == ipo_id)
    stmt = stmt.order_by(LockupSignalSnapshot.event_date, IPO.id,
                         LockupSignalSnapshot.observation_offset, LockupSignalSnapshot.security_id)
    if limit is not None: stmt = stmt.limit(limit)

    result, seen = [], set()
    for snapshot, outcome, ipo, company in db.execute(stmt):
        key = (snapshot.lockup_id, snapshot.observation_offset)
        if key in seen:
            continue
        seen.add(key)
        row = {"ipo_id": snapshot.ipo_id, "lockup_id": snapshot.lockup_id,
               "security_id": snapshot.security_id, "company_name": company.name, "ticker": company.ticker}
        for name in FEATURE_COLUMNS:
            if name == "shares_outstanding_post_ipo": value = ipo.shares_outstanding_post_ipo
            elif name == "ipo_date": value = ipo.ipo_date
            elif name == "lockup_expiration_date": value = ipo.primary_lockup_expiration_date
            else: value = getattr(snapshot, name, None)
            row[name] = _scalar(value)
        for name in OUTCOME_COLUMNS:
            row[name] = _scalar(getattr(outcome, name, None)) if outcome is not None else None
        # Event identity is snapshot provenance too; retain it even when no
        # retrospective outcome row has been produced yet.
        row["event_date"] = row["event_date"] or snapshot.event_date
        row["event_trade_date"] = row["event_trade_date"] or snapshot.event_trade_date
        row.update(snapshot_version=snapshot.snapshot_version,
                   outcome_version=outcome.outcome_version if outcome else None,
                   event_date_source=snapshot.event_date_source, data_cutoff_date=snapshot.data_cutoff_date,
                   has_ipo_price=snapshot.ipo_price is not None,
                   has_20d_history=snapshot.available_history_sessions >= 21,
                   has_40d_history=snapshot.available_history_sessions >= 41,
                   **{f"has_post_{h}d": outcome is not None and getattr(outcome, f"post_{h}d_return") is not None
                      for h in (5, 10, 20, 40)})
        result.append(row)
    return result


def export_backtest_csv(rows, output):
    """Write scalar columns in canonical order and return the output path."""
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: v.isoformat() if isinstance(v, date) else v for k, v in row.items()})
    return path
