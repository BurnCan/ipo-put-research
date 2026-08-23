"""Read-only projections used by the lockup research dashboard."""
from datetime import date

from sqlalchemy import func, select

from app.models import (Company, DailyPrice, IPO, IPOLockup,
                        LockupProspectiveSignal, LockupSignalSnapshot)
from app.services.backtest.analysis import (FROZEN_HYPOTHESES,
                                             analyze_two_feature_interaction)
from app.services.backtest.dataset import build_backtest_dataset
from app.services.event_analysis.constants import SNAPSHOT_VERSION
from app.services.prospective.evaluation import evaluate_prospective_signals

HYPOTHESIS_ID = "m7_return20_vol20_minus5_post20"
GROUPS = ("low_low", "low_high", "high_low", "high_high")


def hypothesis_metadata(hypothesis_id=HYPOTHESIS_ID):
    """Serialize the registry entry without deriving or copying its parameters."""
    spec = FROZEN_HYPOTHESES[hypothesis_id]
    return {"hypothesis_id": hypothesis_id,
            "feature1": {"name": spec.feature1, "threshold": spec.feature1_threshold,
                         "comparison": ">"},
            "feature2": {"name": spec.feature2, "threshold": spec.feature2_threshold,
                         "comparison": ">"},
            "observation_offset": spec.observation_offset, "outcome": spec.outcome,
            "grouping_rule": spec.grouping_rule, "analysis_version": spec.analysis_version,
            "prospective_start_date": spec.prospective_start_date}


def _cohort(db):
    return list(db.execute(
        select(IPOLockup, IPO, Company).join(IPO, IPO.id == IPOLockup.ipo_id)
        .join(Company, Company.id == IPO.company_id)
        .where(IPOLockup.id == IPO.primary_lockup_id,
               IPO.primary_lockup_expiration_date.is_not(None),
               IPO.classification_status == "classified",
               IPO.candidate_type == "operating_company_ipo",
               IPO.offering_status == "priced")
        .order_by(IPO.primary_lockup_expiration_date, IPO.id, IPOLockup.id)))


def _signal_dict(signal, company):
    numeric = ("feature1_value", "feature1_threshold", "feature2_value",
               "feature2_threshold", "realized_outcome_value", "bearish_mfe_20d",
               "bearish_mae_20d")
    fields = ("id", "hypothesis_id", "hypothesis_version", "ipo_id", "lockup_id",
              "security_id", "observation_date", "event_date", "event_trade_date",
              "feature1_name", "feature1_value", "feature1_threshold", "feature1_side",
              "feature2_name", "feature2_value", "feature2_threshold", "feature2_side",
              "interaction_group", "is_high_high", "signal_status",
              "realized_outcome_name", "realized_outcome_value",
              "outcome_observation_date", "bearish_mfe_20d", "bearish_mae_20d", "created_at")
    result = {name: getattr(signal, name) for name in fields}
    result.update(company_name=company.name, ticker=company.ticker)
    for name in numeric:
        result[name] = float(result[name]) if result[name] is not None else None
    return result


def get_prospective_signal_rows(db, *, status=None, interaction_group=None, ticker=None,
                                evaluation_mode="prospective"):
    stmt = (select(LockupProspectiveSignal, Company)
            .join(IPO, IPO.id == LockupProspectiveSignal.ipo_id)
            .join(Company, Company.id == IPO.company_id)
            .where(LockupProspectiveSignal.evaluation_mode == evaluation_mode))
    if status: stmt = stmt.where(LockupProspectiveSignal.signal_status == status)
    if interaction_group: stmt = stmt.where(LockupProspectiveSignal.interaction_group == interaction_group)
    if ticker: stmt = stmt.where(Company.ticker.ilike(ticker.strip()))
    stmt = stmt.order_by(LockupProspectiveSignal.observation_date.desc().nullslast(),
                         LockupProspectiveSignal.event_date, Company.ticker,
                         LockupProspectiveSignal.lockup_id)
    return [_signal_dict(signal, company) for signal, company in db.execute(stmt)]


def get_upcoming_lockups(db, *, today=None):
    today = today or date.today()
    spec = FROZEN_HYPOTHESES[HYPOTHESIS_ID]
    latest = db.scalar(select(func.max(DailyPrice.trade_date)))
    result = []
    for lockup, ipo, company in _cohort(db):
        event_date = lockup.stated_expiration_date or lockup.calculated_expiration_date
        snapshot = db.scalar(select(LockupSignalSnapshot).where(
            LockupSignalSnapshot.lockup_id == lockup.id,
            LockupSignalSnapshot.observation_offset == spec.observation_offset,
            LockupSignalSnapshot.snapshot_version == SNAPSHOT_VERSION).order_by(LockupSignalSnapshot.id))
        signal = db.scalar(select(LockupProspectiveSignal).where(
            LockupProspectiveSignal.lockup_id == lockup.id,
            LockupProspectiveSignal.hypothesis_id == HYPOTHESIS_ID,
            LockupProspectiveSignal.hypothesis_version == spec.analysis_version))
        # Stored M8 state is authoritative.  Only snapshot-only rows belong to
        # historical discovery and must not leak into prospective monitoring.
        if signal is None and snapshot is not None and \
                snapshot.observation_date <= spec.prospective_start_date:
            continue
        status = signal.signal_status if signal else "pending_observation"
        # Stored market-session observations are the only valid T-5 dates.
        # M8 is authoritative when a genuine prospective signal exists.
        t5_observation_date = (signal.observation_date if signal else
                               snapshot.observation_date if snapshot else None)
        t5_timing_status = ("observation_before_prospective_start"
                            if signal and signal.signal_status == "unavailable" else
                            "signal_frozen" if signal else
                            "t5_snapshot_available" if snapshot else
                            "waiting_for_t5")
        result.append({"ipo_id": ipo.id, "lockup_id": lockup.id,
            "company_name": company.name, "ticker": company.ticker, "ipo_date": ipo.ipo_date,
            "lockup_event_date": event_date,
            "primary_lockup_expiration_date": ipo.primary_lockup_expiration_date,
            "event_trade_date": (signal.event_trade_date if signal else
                                 snapshot.event_trade_date if snapshot else None),
            "latest_market_date": latest, "m8_status": status,
            "unavailable_reason": signal.unavailable_reason if signal else None,
            "has_minus5_snapshot": snapshot is not None,
            "minus5_observation_date": snapshot.observation_date if snapshot else None,
            "t5_observation_date": t5_observation_date,
            "t5_snapshot_available": snapshot is not None,
            "t5_timing_status": t5_timing_status,
            "return_20d_at_minus5": float(snapshot.return_20d) if snapshot and snapshot.return_20d is not None else None,
            "realized_vol_20d_at_minus5": float(snapshot.realized_vol_20d) if snapshot and snapshot.realized_vol_20d is not None else None,
            "interaction_group": signal.interaction_group if signal else None,
            "is_high_high": bool(signal.is_high_high) if signal else False,
            "signal_created_at": signal.created_at if signal else None,
            "calendar_days_to_event": (event_date - today).days if event_date else None})
    return result


def get_research_summary(db):
    upcoming = get_upcoming_lockups(db)
    signals = get_prospective_signal_rows(db)
    eligible_lockups = len(_cohort(db))
    missed = sum(r["m8_status"] == "unavailable" for r in upcoming)
    return {"eligible_lockups": eligible_lockups,
            "historical_unavailable": eligible_lockups - len(upcoming),
            "missed_t5_window": missed,
            "pending_observation": sum(r["m8_status"] == "pending_observation" for r in upcoming),
            "prospective_signals": len(signals),
            "awaiting_event": sum(r["signal_status"] == "awaiting_event" for r in signals),
            "awaiting_outcome": sum(r["signal_status"] == "awaiting_outcome" for r in signals),
            "matured_signals": sum(r["signal_status"] == "matured" for r in signals),
            "high_high_signals": sum(r["is_high_high"] for r in signals),
            "high_high_matured": sum(r["is_high_high"] and r["signal_status"] == "matured" for r in signals),
            "latest_market_date": db.scalar(select(func.max(DailyPrice.trade_date)))}


def get_prospective_evaluation(db):
    return evaluate_prospective_signals(db, hypothesis_id=HYPOTHESIS_ID)


def get_historical_reference(db):
    spec = FROZEN_HYPOTHESES[HYPOTHESIS_ID]
    rows = [r for r in build_backtest_dataset(db) if
            r.get("observation_date") is not None and r["observation_date"] <= spec.prospective_start_date]
    report = analyze_two_feature_interaction(rows, spec.feature1, spec.feature2,
                                             spec.outcome, spec.observation_offset,
                                             robustness=True)
    robustness = report["robustness"]
    return {"analysis_type": "historical_discovery", "sample_designation": "not_out_of_sample",
            "is_out_of_sample": False, "hypothesis_id": HYPOTHESIS_ID,
            "discovery_sample_n": report["n_events"], "ols": report["ols"],
            "groups": report["groups"],
            "robustness": {key: robustness[key] for key in
                ("feature1_sign_stable", "feature2_sign_stable",
                 "high_high_median_outcome_always_negative", "high_high_bearish_hit_rate_min")}
            | {"coefficient_ranges": {name: {"min": values["min"], "max": values["max"]}
                 for name, values in robustness["coefficient_summary"].items()
                 if name in ("feature1_coefficient", "feature2_coefficient")}}}
