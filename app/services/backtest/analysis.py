"""Small-sample descriptive analysis without pooled repeated-measures tests."""
from __future__ import annotations

import math
import statistics
from collections import defaultdict

from .dataset import FEATURE_COLUMNS, OUTCOME_COLUMNS

STANDARD_OFFSETS = (-60, -40, -20, -10, -5, -1)


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
