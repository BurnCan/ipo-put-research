"""Descriptive evaluation of genuine prospective M8 rows only."""
import statistics
from sqlalchemy import select
from app.models import LockupProspectiveSignal
from app.services.backtest.analysis import FROZEN_HYPOTHESES

GROUPS = ("low_low", "low_high", "high_low", "high_high")

def _median(xs): return statistics.median(xs) if xs else None

def evaluate_prospective_signals(db, *, hypothesis_id, evaluation_mode="strict_prospective"):
    if hypothesis_id not in FROZEN_HYPOTHESES: raise ValueError(f"unknown frozen hypothesis: {hypothesis_id}")
    rows = list(db.scalars(select(LockupProspectiveSignal).where(
        LockupProspectiveSignal.hypothesis_id == hypothesis_id,
        LockupProspectiveSignal.evaluation_mode.in_(
            ("strict_prospective", "prospective") if evaluation_mode == "strict_prospective"
            else (evaluation_mode,)),
        LockupProspectiveSignal.signal_status != "unavailable"
    ).order_by(LockupProspectiveSignal.id)))
    matured = [r for r in rows if r.realized_outcome_value is not None]
    groups = {}
    for name in GROUPS:
        selected = [r for r in matured if r.interaction_group == name]
        values = [float(r.realized_outcome_value) for r in selected]; n = len(values)
        groups[name] = {"n_events": n, "bearish_hit_count": sum(v < 0 for v in values),
            "bearish_hit_rate": sum(v < 0 for v in values)/n if n else None,
            "mean_outcome": statistics.fmean(values) if values else None, "median_outcome": _median(values),
            "le_5pct_rate": sum(v <= -.05 for v in values)/n if n else None,
            "le_10pct_rate": sum(v <= -.10 for v in values)/n if n else None,
            "le_20pct_rate": sum(v <= -.20 for v in values)/n if n else None,
            "median_bearish_mfe": _median([float(r.bearish_mfe_20d) for r in selected if r.bearish_mfe_20d is not None]),
            "median_bearish_mae": _median([float(r.bearish_mae_20d) for r in selected if r.bearish_mae_20d is not None])}
    return {"analysis_type": "prospective_out_of_sample_evaluation", "hypothesis_id": hypothesis_id,
            "evaluation_mode": evaluation_mode,
            "total_signals": len(rows), "matured_signals": len(matured),
            "pending_signals": len(rows)-len(matured), "groups": groups}
