"""Small-sample descriptive analysis without pooled repeated-measures tests."""
from __future__ import annotations

import math
import statistics
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import date

from .dataset import FEATURE_COLUMNS, OUTCOME_COLUMNS

STANDARD_OFFSETS = (-60, -40, -20, -10, -5, -1)


@dataclass(frozen=True)
class FrozenHypothesis:
    """Stable, non-database identity for a prospectively testable hypothesis.

    ``prospective_start_date`` is the inclusive historical cutoff (the date the
    hypothesis was frozen), not the first observation date admitted to M8.
    Prospective observations must be strictly later than this date.
    """
    feature1: str
    feature2: str
    outcome: str
    observation_offset: int
    grouping_rule: str = "median_split"
    analysis_version: str = "m7_robustness_v1"
    feature1_threshold: float | None = None
    feature2_threshold: float | None = None
    prospective_start_date: date | None = None


FROZEN_HYPOTHESES = {
    "m7_return20_vol20_minus5_post20": FrozenHypothesis(
        "return_20d", "realized_vol_20d", "post_20d_return", -5,
        feature1_threshold=0.0332778702,
        feature2_threshold=0.8446461455,
        prospective_start_date=date(2026, 8, 23))
}


def _mean(values): return statistics.fmean(values) if values else None
def _median(values): return statistics.median(values) if values else None


def _ranks(values):
    order = sorted(range(len(values)), key=values.__getitem__)
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i + 1
        while j < len(order) and values[order[j]] == values[order[i]]: j += 1
        rank = (i + 1 + j) / 2
        for k in order[i:j]: ranks[k] = rank
        i = j
    return ranks


def spearman_correlation(x, y):
    """Spearman rho and an exploratory, normal-approximation two-sided p-value."""
    pairs = [(float(a), float(b)) for a, b in zip(x, y) if a is not None and b is not None]
    n = len(pairs)
    if n < 3: return {"n": n, "spearman_rho": None, "p_value": None}
    rx, ry = _ranks([p[0] for p in pairs]), _ranks([p[1] for p in pairs])
    if len(set(rx)) == 1 or len(set(ry)) == 1:
        return {"n": n, "spearman_rho": None, "p_value": None}
    mx, my = _mean(rx), _mean(ry)
    numerator = sum((a-mx)*(b-my) for a, b in zip(rx, ry))
    denominator = math.sqrt(sum((a-mx)**2 for a in rx) * sum((b-my)**2 for b in ry))
    rho = numerator / denominator
    if abs(rho) >= 1: p = 0.0
    else:
        z = abs(rho) * math.sqrt((n - 2) / max(1e-15, 1 - rho*rho))
        p = math.erfc(z / math.sqrt(2))
    return {"n": n, "spearman_rho": rho, "p_value": p}


def _outcome_stats(rows, outcome):
    values = [float(r[outcome]) for r in rows if r.get(outcome) is not None]
    n = len(values)
    rates = {}
    for label, cutoff in (("le_5pct", -.05), ("le_10pct", -.10), ("le_20pct", -.20)):
        count = sum(v <= cutoff for v in values)
        rates[label] = {"count": count, "rate": count / n if n else None}
    return {"mean_outcome": _mean(values), "median_outcome": _median(values),
            "bearish_hit_count": sum(v < 0 for v in values),
            "bearish_hit_rate": sum(v < 0 for v in values) / n if n else None,
            "magnitude_thresholds": rates}


def _group(rows, feature, outcome):
    result = {"n_events": len({r["lockup_id"] for r in rows}), "n_observations": len(rows),
              "median_feature": _median([float(r[feature]) for r in rows]), **_outcome_stats(rows, outcome)}
    suffix = outcome.removeprefix("post_").removesuffix("_return")
    for kind in ("mfe", "mae"):
        column = f"bearish_{kind}_{suffix}"
        values = [float(r[column]) for r in rows if r.get(column) is not None]
        result[f"median_bearish_{kind}"] = _median(values)
    return result


def _interaction_group(rows, feature1, feature2, outcome):
    """Summarize one of the four predefined two-feature median cells."""
    result = {
        "n_events": len({r["lockup_id"] for r in rows}),
        "n_observations": len(rows),
        "feature1_median": _median([float(r[feature1]) for r in rows]),
        "feature2_median": _median([float(r[feature2]) for r in rows]),
        **_outcome_stats(rows, outcome),
    }
    suffix = outcome.removeprefix("post_").removesuffix("_return")
    for kind in ("mfe", "mae"):
        column = f"bearish_{kind}_{suffix}"
        values = [float(r[column]) for r in rows if r.get(column) is not None]
        result[f"median_bearish_{kind}"] = _median(values)
    return result


def _coefficient_sign(value):
    if value is None: return None
    return "positive" if value > 0 else "negative" if value < 0 else "zero"


def _ols_two_feature(rows, feature1, feature2, outcome):
    """Fit intercept plus two slopes through centered cross-products.

    The two-by-two solve is equivalent to ordinary least squares.  Scaling the
    singularity tolerance to the covariance matrix avoids silently returning
    unstable coefficients for constant or nearly collinear inputs.
    """
    n = len(rows)
    empty = {"n": n, "status": "insufficient_sample" if n < 4 else "singular_design",
             "intercept": None, "feature1_coefficient": None,
             "feature2_coefficient": None, "feature1_coefficient_sign": None,
             "feature2_coefficient_sign": None, "r_squared": None,
             "standardized_beta_feature1": None, "standardized_beta_feature2": None}
    if n < 4: return empty
    x1 = [float(r[feature1]) for r in rows]
    x2 = [float(r[feature2]) for r in rows]
    y = [float(r[outcome]) for r in rows]
    m1, m2, my = _mean(x1), _mean(x2), _mean(y)
    c1, c2, cy = ([v - mean for v in values]
                  for values, mean in ((x1, m1), (x2, m2), (y, my)))
    s11, s22 = sum(v*v for v in c1), sum(v*v for v in c2)
    s12 = sum(a*b for a, b in zip(c1, c2))
    det = s11*s22 - s12*s12
    scale = max(s11*s22, s12*s12)
    if s11 == 0 or s22 == 0 or abs(det) <= 1e-12 * scale: return empty
    s1y, s2y = sum(a*b for a, b in zip(c1, cy)), sum(a*b for a, b in zip(c2, cy))
    b1 = (s1y*s22 - s2y*s12) / det
    b2 = (s2y*s11 - s1y*s12) / det
    intercept = my - b1*m1 - b2*m2
    residual_ss = sum((actual - (intercept + b1*a + b2*b))**2
                      for a, b, actual in zip(x1, x2, y))
    total_ss = sum(v*v for v in cy)
    r_squared = None if total_ss == 0 else max(0.0, 1 - residual_ss / total_ss)
    sy = math.sqrt(total_ss / n) if total_ss else 0.0
    return {"n": n, "status": "ok" if total_ss else "constant_outcome",
            "intercept": intercept, "feature1_coefficient": b1,
            "feature2_coefficient": b2,
            "feature1_coefficient_sign": _coefficient_sign(b1),
            "feature2_coefficient_sign": _coefficient_sign(b2), "r_squared": r_squared,
            "standardized_beta_feature1": b1 * math.sqrt(s11 / n) / sy if sy else None,
            "standardized_beta_feature2": b2 * math.sqrt(s22 / n) / sy if sy else None}


def _complete_interaction_rows(rows, feature1, feature2, outcome, offset):
    unique = {}
    input_rows = [r for r in rows if r.get("observation_offset") == offset]
    for row in input_rows:
        unique.setdefault(row["lockup_id"], row)
    valid = [r for r in unique.values() if r.get(feature1) is not None and
             r.get(feature2) is not None and r.get(outcome) is not None]
    return input_rows, unique, valid


def _high_high(rows, feature1, feature2, outcome):
    median1 = _median([float(r[feature1]) for r in rows])
    median2 = _median([float(r[feature2]) for r in rows])
    members = [] if median1 is None else [
        r for r in rows if float(r[feature1]) > median1 and float(r[feature2]) > median2]
    group = _interaction_group(members, feature1, feature2, outcome)
    return {
        "n_high_high": group["n_observations"],
        "bearish_hit_rate": group["bearish_hit_rate"],
        "mean_outcome": group["mean_outcome"],
        "median_outcome": group["median_outcome"],
        "le_5pct_rate": group["magnitude_thresholds"]["le_5pct"]["rate"],
        "le_10pct_rate": group["magnitude_thresholds"]["le_10pct"]["rate"],
        "le_20pct_rate": group["magnitude_thresholds"]["le_20pct"]["rate"],
        "median_bearish_mfe": group["median_bearish_mfe"],
        "median_bearish_mae": group["median_bearish_mae"],
    }


def _range_summary(values):
    values = [v for v in values if v is not None]
    return {"min": min(values) if values else None, "max": max(values) if values else None,
            "median": _median(values), "mean": _mean(values)}


def _different_sign(value, reference):
    return value is not None and reference is not None and _coefficient_sign(value) != _coefficient_sign(reference)


def _influence(rows, feature1, feature2, outcome, ols):
    """Exact hat diagonals and standard OLS residual diagnostics."""
    if ols["intercept"] is None:
        return {"status": ols["status"], "ranking_rule": "unavailable", "rows": [], "top_5": []}
    n, p = len(rows), 3
    x1, x2 = [float(r[feature1]) for r in rows], [float(r[feature2]) for r in rows]
    m1, m2 = _mean(x1), _mean(x2)
    c1, c2 = [v-m1 for v in x1], [v-m2 for v in x2]
    s11, s22 = sum(v*v for v in c1), sum(v*v for v in c2)
    s12 = sum(a*b for a, b in zip(c1, c2)); det = s11*s22-s12*s12
    residuals = [float(r[outcome])-(ols["intercept"]+ols["feature1_coefficient"]*a+
                 ols["feature2_coefficient"]*b) for r, a, b in zip(rows, x1, x2)]
    rss = sum(e*e for e in residuals)
    mse = rss/(n-p) if n > p else None
    result = []
    for row, a, b, residual in zip(rows, c1, c2, residuals):
        leverage = 1/n + (s22*a*a - 2*s12*a*b + s11*b*b)/det
        if -1e-12 < leverage < 0: leverage = 0.0
        if 1 < leverage < 1+1e-12: leverage = 1.0
        predicted = float(row[outcome])-residual
        usable = mse is not None and mse > 0 and leverage < 1
        standardized = residual/math.sqrt(mse*(1-leverage)) if usable else None
        cooks = (residual*residual/(p*mse))*leverage/((1-leverage)**2) if usable else None
        result.append({"lockup_id": row["lockup_id"], "ticker": row.get("ticker"),
                       "actual_outcome": float(row[outcome]), "predicted_outcome": predicted,
                       "residual": residual, "leverage": leverage,
                       "standardized_residual": standardized, "cooks_distance": cooks})
    if any(r["cooks_distance"] is not None for r in result):
        rule = "cooks_distance_desc_then_lockup_id"
        ranked = sorted(result, key=lambda r: (r["cooks_distance"] is None,
                                                -(r["cooks_distance"] or 0),
                                                r["lockup_id"]))
    else:
        rule = "absolute_standardized_residual_desc_then_leverage_desc_then_lockup_id"
        ranked = sorted(result, key=lambda r: (-(abs(r["standardized_residual"]) if r["standardized_residual"] is not None else -1),
                                                -r["leverage"], r["lockup_id"]))
    return {"status": "ok", "ranking_rule": rule, "rows": ranked, "top_5": ranked[:5]}


def analyze_interaction_robustness(rows, feature1, feature2, outcome, offset, full_ols=None):
    """Leave one event out without searching features, offsets, or thresholds."""
    _, _, valid = _complete_interaction_rows(rows, feature1, feature2, outcome, offset)
    valid = sorted(valid, key=lambda r: r["lockup_id"])
    full_ols = full_ols or _ols_two_feature(valid, feature1, feature2, outcome)
    runs = []
    for excluded in valid:
        reduced = [r for r in valid if r["lockup_id"] != excluded["lockup_id"]]
        fit = _ols_two_feature(reduced, feature1, feature2, outcome)
        runs.append({"excluded_lockup_id": excluded["lockup_id"],
                     "excluded_ticker": excluded.get("ticker"), "n": len(reduced),
                     "feature1_coefficient": fit["feature1_coefficient"],
                     "feature2_coefficient": fit["feature2_coefficient"],
                     "standardized_beta_feature1": fit["standardized_beta_feature1"],
                     "standardized_beta_feature2": fit["standardized_beta_feature2"],
                     "r_squared": fit["r_squared"], "status": fit["status"],
                     **_high_high(reduced, feature1, feature2, outcome)})
    successful = [r for r in runs if r["status"] == "ok"]
    fields = ("feature1_coefficient", "feature2_coefficient", "standardized_beta_feature1",
              "standardized_beta_feature2", "r_squared")
    coefficient_summary = {}
    for field in fields:
        summary = {"full_sample_value": full_ols[field], **_range_summary([r[field] for r in successful])}
        if field in ("feature1_coefficient", "feature2_coefficient"):
            summary["sign_flip_count"] = sum(_different_sign(r[field], full_ols[field]) for r in successful)
        coefficient_summary[field] = summary
    group_fields = ("n_high_high", "bearish_hit_rate", "mean_outcome", "median_outcome",
                    "le_20pct_rate", "median_bearish_mfe", "median_bearish_mae")
    high_summary = {field: _range_summary(
        [r[field] for r in runs if field == "n_high_high" or r["n_high_high"]])
        for field in group_fields}
    high_summary["empty_high_high_runs"] = sum(r["n_high_high"] == 0 for r in runs)
    hypothesis_id = next((name for name, spec in FROZEN_HYPOTHESES.items()
                          if (spec.feature1, spec.feature2, spec.outcome, spec.observation_offset)
                          == (feature1, feature2, outcome, offset)), None)
    hypothesis = FROZEN_HYPOTHESES.get(
        hypothesis_id, FrozenHypothesis(feature1, feature2, outcome, offset))
    return {
        "hypothesis_id": hypothesis_id,
        "hypothesis": asdict(hypothesis),
        "leave_one_out": runs,
        "coefficient_summary": coefficient_summary,
        "run_counts": {"successful_runs": len(successful), "failed_runs": len(runs)-len(successful),
                       "total_runs": len(runs)},
        "high_high_summary": high_summary,
        "influence": _influence(valid, feature1, feature2, outcome, full_ols),
        "feature1_sign_stable": coefficient_summary["feature1_coefficient"]["sign_flip_count"] == 0,
        "feature2_sign_stable": coefficient_summary["feature2_coefficient"]["sign_flip_count"] == 0,
        "high_high_median_outcome_always_negative": bool(runs) and all(
            r["median_outcome"] is not None and r["median_outcome"] < 0 for r in runs),
        "high_high_bearish_hit_rate_min": high_summary["bearish_hit_rate"]["min"],
        "small_sample_warning": True, "exploratory_only": True,
        "no_multiple_testing_correction": True,
        "leave_one_out_is_not_out_of_sample_validation": True,
    }


def analyze_two_feature_interaction(rows, feature1, feature2, outcome, offset, robustness=False):
    """Run one explicitly requested two-feature analysis at one offset."""
    if feature1 not in FEATURE_COLUMNS:
        raise ValueError(f"not an allowed pre-event feature: {feature1}")
    if feature2 not in FEATURE_COLUMNS:
        raise ValueError(f"not an allowed pre-event feature: {feature2}")
    if outcome not in OUTCOME_COLUMNS:
        raise ValueError(f"not an allowed retrospective outcome: {outcome}")
    if offset is None: raise ValueError("two-feature interaction requires an explicit offset")
    input_rows, unique, valid = _complete_interaction_rows(rows, feature1, feature2, outcome, offset)
    values1, values2 = ([float(r[name]) for r in valid] for name in (feature1, feature2))
    median1, median2 = _median(values1), _median(values2)
    cells = {name: [] for name in ("low_low", "low_high", "high_low", "high_high")}
    if median1 is not None:
        for row in valid:
            side1 = "low" if float(row[feature1]) <= median1 else "high"
            side2 = "low" if float(row[feature2]) <= median2 else "high"
            cells[f"{side1}_{side2}"].append(row)
    report = {"analysis_type": "two_feature_interaction", "feature1": feature1,
            "feature2": feature2, "outcome": outcome, "observation_offset": offset,
            "n_input_rows": len(input_rows), "n_valid_rows": len(valid),
            "n_excluded_missing": len(unique) - len(valid),
            "n_events": len(valid), "n_observations": len(valid),
            "feature1_median": median1, "feature2_median": median2,
            "groups": {name: _interaction_group(group, feature1, feature2, outcome)
                       for name, group in cells.items()},
            "ols": _ols_two_feature(valid, feature1, feature2, outcome),
            "repeated_measures_by_lockup": True, "p_values_are_exploratory": True,
            "small_sample_warning": True}
    if robustness:
        report["robustness"] = analyze_interaction_robustness(
            valid, feature1, feature2, outcome, offset, report["ols"])
    return report


def analyze_offset(rows, feature, outcome, offset):
    valid = [r for r in rows if r.get("observation_offset") == offset and
             r.get(feature) is not None and r.get(outcome) is not None]
    features = [float(r[feature]) for r in valid]
    result = {"observation_offset": offset, "n_events": len({r["lockup_id"] for r in valid}),
              "n_observations": len(valid), "feature_mean": _mean(features),
              "feature_median": _median(features), **_outcome_stats(valid, outcome),
              **spearman_correlation(features, [r[outcome] for r in valid])}
    median = _median(features)
    result["median_split"] = {} if median is None else {
        "feature_lte_median": _group([r for r in valid if float(r[feature]) <= median], feature, outcome),
        "feature_gt_median": _group([r for r in valid if float(r[feature]) > median], feature, outcome)}
    return result


def analyze_feature(rows, feature, outcome, offset=None):
    if feature not in FEATURE_COLUMNS: raise ValueError(f"not an allowed pre-event feature: {feature}")
    if outcome not in OUTCOME_COLUMNS: raise ValueError(f"not an allowed retrospective outcome: {outcome}")
    offsets = (offset,) if offset is not None else STANDARD_OFFSETS
    reports = [analyze_offset(rows, feature, outcome, value) for value in offsets
               if any(r.get("observation_offset") == value for r in rows)]
    return {"feature": feature, "outcome": outcome, "repeated_measures_by_lockup": True,
            "p_values_are_exploratory": True, "offsets": reports}


def classify_signal_persistence(rows, feature, offsets=(-20, -10, -5, -1)):
    """Classify complete, non-zero sign paths; incomplete paths are omitted."""
    grouped = defaultdict(dict)
    for row in rows:
        if row.get("observation_offset") in offsets and row.get(feature) is not None:
            grouped[row["lockup_id"]][row["observation_offset"]] = float(row[feature])
    result = []
    for lockup_id, path in sorted(grouped.items()):
        if any(o not in path for o in offsets): continue
        signs = [-1 if path[o] < 0 else 1 if path[o] > 0 else 0 for o in offsets]
        if all(s < 0 for s in signs): category = "persistent_negative"
        elif all(s > 0 for s in signs): category = "persistent_positive"
        elif signs[-1] > 0 and any(s < 0 for s in signs[:-1]): category = "negative_then_reversed"
        elif signs[-1] < 0 and any(s > 0 for s in signs[:-1]): category = "positive_then_reversed"
        else: category = "mixed"
        result.append({"lockup_id": lockup_id, "category": category})
    return result
