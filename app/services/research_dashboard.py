"""Read-only projections used by the lockup research dashboard."""
from datetime import date

from sqlalchemy import case, func, select

from app.models import (Company, DailyPrice, IPO, IPOLockup,
                        LockupProspectiveSignal, LockupSignalSnapshot)
from app.services.backtest.analysis import (FROZEN_HYPOTHESES,
                                             analyze_two_feature_interaction)
from app.services.backtest.dataset import build_backtest_dataset
from app.services.event_analysis.constants import SNAPSHOT_VERSION
from app.services.market_calendar import resolve_observation_session
from app.services.prospective.evaluation import evaluate_prospective_signals

HYPOTHESIS_ID = "m7_return20_vol20_minus5_post20"
GROUPS = ("low_low", "low_high", "high_low", "high_high")
STRICT_EVALUATION_MODES = ("strict_prospective", "prospective")


def _signals_for_lockup(db, lockup_id, spec):
    """Load and separate stored signal roles in an explicit, stable order."""
    mode_order = case(
        (LockupProspectiveSignal.evaluation_mode == "strict_prospective", 0),
        (LockupProspectiveSignal.evaluation_mode == "prospective", 1),
        (LockupProspectiveSignal.evaluation_mode == "shadow_prospective", 2),
        (LockupProspectiveSignal.evaluation_mode == "lifecycle_tracking", 3),
        else_=4,
    )
    rows = list(db.scalars(select(LockupProspectiveSignal).where(
        LockupProspectiveSignal.lockup_id == lockup_id,
        LockupProspectiveSignal.hypothesis_id == HYPOTHESIS_ID,
        LockupProspectiveSignal.hypothesis_version == spec.analysis_version,
    ).order_by(mode_order, LockupProspectiveSignal.id)))
    return {
        "strict_signal": next((row for row in rows
                               if row.evaluation_mode in STRICT_EVALUATION_MODES), None),
        "shadow_signal": next((row for row in rows
                               if row.evaluation_mode == "shadow_prospective"), None),
        "lifecycle_row": next((row for row in rows
                               if row.evaluation_mode == "lifecycle_tracking"), None),
    }


def _classify_non_signaled(required_t5_date, today):
    """Return the projected M8 lifecycle state before a signal is persisted."""
    return ("pending_observation" if required_t5_date > today
            else "waiting_for_market_data")


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
              "required_observation_date", "calendar_id", "calendar_provider",
              "calendar_version",
              "feature1_name", "feature1_value", "feature1_threshold", "feature1_side",
              "feature2_name", "feature2_value", "feature2_threshold", "feature2_side",
              "interaction_group", "is_high_high", "signal_status", "evaluation_mode",
              "realized_outcome_name", "realized_outcome_value",
              "outcome_observation_date", "bearish_mfe_20d", "bearish_mae_20d", "created_at")
    result = {name: getattr(signal, name) for name in fields}
    # Public provenance name; ``created_at`` is the immutable lock timestamp.
    result["signal_locked_at"] = signal.created_at
    result.update(company_name=company.name, ticker=company.ticker)
    for name in numeric:
        result[name] = float(result[name]) if result[name] is not None else None
    return result


def get_prospective_signal_rows(db, *, status=None, interaction_group=None, ticker=None,
                                evaluation_mode="strict_prospective"):
    stmt = (select(LockupProspectiveSignal, Company)
            .join(IPO, IPO.id == LockupProspectiveSignal.ipo_id)
            .join(Company, Company.id == IPO.company_id)
            .where(LockupProspectiveSignal.evaluation_mode.in_(
                ("strict_prospective", "prospective") if evaluation_mode == "strict_prospective"
                else (evaluation_mode,))))
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
        signals = _signals_for_lockup(db, lockup.id, spec)
        signal = (signals["strict_signal"] or signals["shadow_signal"] or
                  signals["lifecycle_row"])
        # Stored M8 state is authoritative.  Only snapshot-only rows belong to
        # historical discovery and must not leak into prospective monitoring.
        if signal is None and snapshot is not None and \
                snapshot.observation_date <= spec.prospective_start_date:
            continue
        resolution = resolve_observation_session(event_date, spec.observation_offset)
        # A genuine prospective M8 signal is frozen evidence.  Rows created
        # before calendar provenance was stored have no required date, so
        # preserve their observation date rather than recomputing identity.
        # Lifecycle rows, by contrast, use the stored calendar-derived field
        # when present and otherwise retain the canonical fallback.
        if signal and signal.evaluation_mode in (*STRICT_EVALUATION_MODES,
                                                 "shadow_prospective"):
            required_t5_date = (signal.required_observation_date or
                                signal.observation_date or
                                resolution.observation_session)
        else:
            required_t5_date = (signal.required_observation_date if signal and
                                signal.required_observation_date else
                                resolution.observation_session)
        if signal:
            status = signal.signal_status
        else:
            status = _classify_non_signaled(required_t5_date, today)
        stored_t5_snapshot_date = snapshot.observation_date if snapshot else None
        t5_timing_status = ("shadow_signal_frozen"
                            if signal and signal.evaluation_mode == "shadow_prospective" else
                            "observation_before_prospective_start"
                            if signal and signal.signal_status == "unavailable" else
                            "signal_frozen" if signal else
                            "t5_snapshot_available" if snapshot else
                            "waiting_for_t5" if required_t5_date > today else
                            "waiting_for_market_data")
        result.append({"ipo_id": ipo.id, "lockup_id": lockup.id,
            "company_name": company.name, "ticker": company.ticker, "ipo_date": ipo.ipo_date,
            "lockup_event_date": event_date,
            "primary_lockup_expiration_date": ipo.primary_lockup_expiration_date,
            "event_trade_date": (signal.event_trade_date if signal else
                                 snapshot.event_trade_date if snapshot else None),
            "latest_market_date": latest, "m8_status": status,
            "evaluation_mode": signal.evaluation_mode if signal else None,
            "prospective_track": ("strict" if signal and
                                  signal.evaluation_mode in STRICT_EVALUATION_MODES else
                                  "shadow" if signal and
                                  signal.evaluation_mode == "shadow_prospective" else None),
            "unavailable_reason": signal.unavailable_reason if signal else None,
            "has_minus5_snapshot": snapshot is not None,
            "minus5_observation_date": snapshot.observation_date if snapshot else None,
            "required_observation_date": required_t5_date,
            "required_t5_date": required_t5_date,
            "stored_t5_snapshot_date": stored_t5_snapshot_date,
            "signal_observation_date": signal.observation_date if signal else None,
            # Deprecated backward-compatible alias.  For genuine prospective
            # rows it preserves the historical stored observation identity
            # when required_observation_date is absent; otherwise it is the
            # required session identity represented by required_t5_date.
            "t5_observation_date": required_t5_date,
            "calendar_id": signal.calendar_id if signal and signal.calendar_id else resolution.calendar_id,
            "calendar_provider": (signal.calendar_provider if signal and signal.calendar_provider
                                  else resolution.calendar_provider),
            "calendar_version": (signal.calendar_version if signal and signal.calendar_version
                                 else resolution.calendar_version),
            "t5_snapshot_available": snapshot is not None,
            "t5_timing_status": t5_timing_status,
            "return_20d_at_minus5": (float(signal.feature1_value)
                if signal and signal.evaluation_mode in (*STRICT_EVALUATION_MODES,
                    "shadow_prospective") and signal.feature1_value is not None
                else float(snapshot.return_20d) if snapshot and snapshot.return_20d is not None
                else None),
            "realized_vol_20d_at_minus5": (float(signal.feature2_value)
                if signal and signal.evaluation_mode in (*STRICT_EVALUATION_MODES,
                    "shadow_prospective") and signal.feature2_value is not None
                else float(snapshot.realized_vol_20d) if snapshot and snapshot.realized_vol_20d is not None
                else None),
            "interaction_group": signal.interaction_group if signal else None,
            "is_high_high": bool(signal.is_high_high) if signal else False,
            "signal_created_at": signal.created_at if signal else None,
            "signal_locked_at": signal.created_at if signal else None,
            "calendar_days_to_event": (event_date - today).days if event_date else None})
    return result


def get_research_summary(db, *, today=None):
    today = today or date.today()
    spec = FROZEN_HYPOTHESES[HYPOTHESIS_ID]
    categories = {"historical_unavailable": 0, "missed_t5_window": 0,
                  "pending_observation": 0, "waiting_for_market_data": 0,
                  "prospective_signals": 0, "shadow_signals": 0}
    prospective_rows = []
    cohort = _cohort(db)
    for lockup, _ipo, _company in cohort:
        signals = _signals_for_lockup(db, lockup.id, spec)
        strict_signal = signals["strict_signal"]
        shadow_signal = signals["shadow_signal"]
        lifecycle_row = signals["lifecycle_row"]
        categories["shadow_signals"] += int(shadow_signal is not None)
        snapshot = db.scalar(select(LockupSignalSnapshot).where(
            LockupSignalSnapshot.lockup_id == lockup.id,
            LockupSignalSnapshot.observation_offset == spec.observation_offset,
            LockupSignalSnapshot.snapshot_version == SNAPSHOT_VERSION)
            .order_by(LockupSignalSnapshot.id))
        # Primary categories partition the clean cohort; shadow_signals is a
        # separate secondary count and never enters prospective_signals.
        if strict_signal is not None:
            categories["prospective_signals"] += 1
            prospective_rows.append(strict_signal)
        elif (shadow_signal is None and lifecycle_row is not None and
              lifecycle_row.signal_status == "unavailable"):
            categories["missed_t5_window"] += 1
        elif (snapshot is not None and
              snapshot.observation_date <= spec.prospective_start_date):
            categories["historical_unavailable"] += 1
        else:
            event_date = (lockup.stated_expiration_date or
                          lockup.calculated_expiration_date)
            required = resolve_observation_session(
                event_date, spec.observation_offset).observation_session
            categories[_classify_non_signaled(required, today)] += 1

    eligible_lockups = len(cohort)
    return {"eligible_lockups": eligible_lockups,
            **categories,
            "awaiting_event": sum(r.signal_status == "awaiting_event" for r in prospective_rows),
            "awaiting_outcome": sum(r.signal_status == "awaiting_outcome" for r in prospective_rows),
            "matured_signals": sum(r.signal_status == "matured" for r in prospective_rows),
            "high_high_signals": sum(r.is_high_high for r in prospective_rows),
            "high_high_matured": sum(r.is_high_high and r.signal_status == "matured"
                                     for r in prospective_rows),
            "latest_market_date": db.scalar(select(func.max(DailyPrice.trade_date)))}


def get_prospective_evaluation(db):
    return evaluate_prospective_signals(db, hypothesis_id=HYPOTHESIS_ID)


def get_shadow_evaluation(db):
    """Secondary evidence only; never contributes to the primary M8 scorecard."""
    return evaluate_prospective_signals(
        db, hypothesis_id=HYPOTHESIS_ID, evaluation_mode="shadow_prospective")


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
