"""Read-only M9A aggregation over frozen, persisted M8 evidence.

M9A consumes frozen M8 prospective evidence. It does not alter signal
generation or optimize the frozen hypothesis. Strict and shadow prospective
populations remain separate.
"""
from __future__ import annotations

import statistics

from sqlalchemy import select

from app.models import Company, IPO, LockupProspectiveSignal
from app.services.backtest.analysis import FROZEN_HYPOTHESES
from app.services.prospective.signals import LEGACY_STRICT, SHADOW, STRICT

from .evaluation import is_bearish_outcome
from .m9a_spec import M9A_PROSPECTIVE_EVAL_V1, M9AEvaluationSpec

GROUPS = ("high_high", "high_low", "low_high", "low_low")


def _mean(values):
    return statistics.fmean(values) if values else None


def _median(values):
    return statistics.median(values) if values else None


def _stats(rows):
    values = [float(row.realized_outcome_value) for row in rows
              if row.realized_outcome_value is not None]
    bearish = sum(is_bearish_outcome(value) for value in values)
    return {
        "matured_signals": len(values),
        "bearish_outcomes": bearish,
        "non_bearish_outcomes": len(values) - bearish,
        "bearish_rate": bearish / len(values) if values else None,
        "mean_post_20d_return": _mean(values),
        "median_post_20d_return": _median(values),
    }


def evaluate_m9a_prospective(db, *, evaluation_mode: str = STRICT,
                             spec: M9AEvaluationSpec = M9A_PROSPECTIVE_EVAL_V1):
    """Evaluate one prospective population without flushing or modifying it."""
    if evaluation_mode not in (STRICT, SHADOW):
        raise ValueError("evaluation_mode must be strict_prospective or shadow_prospective")
    frozen = FROZEN_HYPOTHESES[spec.hypothesis_id]
    modes = (STRICT, LEGACY_STRICT) if evaluation_mode == STRICT else (SHADOW,)
    stmt = (select(LockupProspectiveSignal, IPO, Company)
            .join(IPO, IPO.id == LockupProspectiveSignal.ipo_id)
            .join(Company, Company.id == IPO.company_id)
            .where(
                LockupProspectiveSignal.hypothesis_id == spec.hypothesis_id,
                LockupProspectiveSignal.hypothesis_version == frozen.analysis_version,
                LockupProspectiveSignal.evaluation_mode.in_(modes),
                LockupProspectiveSignal.signal_status != "unavailable",
            ).order_by(LockupProspectiveSignal.id))
    records = list(db.execute(stmt))
    rows = [record[0] for record in records]
    matured = [row for row in rows if row.realized_outcome_value is not None]
    target = [row for row in rows if row.interaction_group == spec.target_group]
    matured_target = [row for row in target if row.realized_outcome_value is not None]
    non_target = [row for row in matured if row.interaction_group != spec.target_group]
    overall_stats, target_stats, non_target_stats = map(
        _stats, (matured, matured_target, non_target))

    group_breakdown = {}
    for group in GROUPS:
        selected = [row for row in rows if row.interaction_group == group]
        group_breakdown[group] = {"total_signals": len(selected), **_stats(selected)}

    strict_population = evaluation_mode == STRICT
    overall_ready = strict_population and len(matured) >= spec.minimum_matured_strict_count
    target_ready = strict_population and len(matured_target) >= spec.minimum_matured_target_count
    eligible = overall_ready and target_ready

    observations = []
    for row, ipo, company in records:
        outcome = (float(row.realized_outcome_value)
                   if row.realized_outcome_value is not None else None)
        observations.append({
            "signal_id": row.id,
            "ticker": company.ticker,
            "company_id": ipo.company_id,
            "ipo_id": row.ipo_id,
            "lockup_id": row.lockup_id,
            "event_date": row.event_date.isoformat() if row.event_date else None,
            "observation_date": row.observation_date.isoformat() if row.observation_date else None,
            "signal_locked_at": row.created_at.isoformat() if row.created_at else None,
            "evaluation_mode": evaluation_mode,
            "persisted_evaluation_mode": row.evaluation_mode,
            "interaction_group": row.interaction_group,
            "return_20d": float(row.feature1_value) if row.feature1_value is not None else None,
            "realized_vol_20d": float(row.feature2_value) if row.feature2_value is not None else None,
            "signal_status": row.signal_status,
            "post_20d_return": outcome,
            "bearish_hit": is_bearish_outcome(outcome) if outcome is not None else None,
            "is_target_group": row.interaction_group == spec.target_group,
        })

    return {
        "evaluation_id": spec.evaluation_id,
        "hypothesis_id": spec.hypothesis_id,
        "evaluation_mode": evaluation_mode,
        "specification": {
            "target_group": spec.target_group,
            "primary_outcome": spec.primary_outcome,
            "bearish_hit_rule": spec.bearish_hit_rule,
        },
        "population": {
            "total_prospective_signals": len(rows),
            "pending_immature_signals": len(rows) - len(matured),
            "matured_signals": len(matured),
            "target_group_signals": len(target),
            "matured_target_group_signals": len(matured_target),
        },
        "primary_outcome": {
            "matured_bearish_outcomes": overall_stats["bearish_outcomes"],
            "matured_non_bearish_outcomes": overall_stats["non_bearish_outcomes"],
            "bearish_outcome_rate": overall_stats["bearish_rate"],
            "target_bearish_hits": target_stats["bearish_outcomes"],
            "target_non_bearish_outcomes": target_stats["non_bearish_outcomes"],
            "target_bearish_hit_rate": target_stats["bearish_rate"],
        },
        "continuous_outcome": {
            "mean_post_20d_return": overall_stats["mean_post_20d_return"],
            "median_post_20d_return": overall_stats["median_post_20d_return"],
            "target_mean_post_20d_return": target_stats["mean_post_20d_return"],
            "target_median_post_20d_return": target_stats["median_post_20d_return"],
            "non_target_mean_post_20d_return": non_target_stats["mean_post_20d_return"],
            "non_target_median_post_20d_return": non_target_stats["median_post_20d_return"],
            "target_minus_non_target_median_return": (
                target_stats["median_post_20d_return"] - non_target_stats["median_post_20d_return"]
                if target_stats["median_post_20d_return"] is not None
                and non_target_stats["median_post_20d_return"] is not None else None),
        },
        "interpretation_readiness": {
            "threshold_population": STRICT,
            "minimum_matured_strict_count": spec.minimum_matured_strict_count,
            "minimum_matured_target_count": spec.minimum_matured_target_count,
            "matured_strict_threshold_satisfied": overall_ready,
            "matured_target_threshold_satisfied": target_ready,
            "eligible_for_provisional_interpretation": eligible,
            "status": "provisional_interpretation_ready" if eligible else "descriptive_only",
        },
        "group_breakdown": group_breakdown,
        "observations": observations,
    }
