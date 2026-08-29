"""Read-only comparison of immutable M6 v1 and canonical M6 v2 snapshots."""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select

from app.models import Company, IPO, IPOLockup, LockupSignalSnapshot
from app.services.backtest.analysis import FROZEN_HYPOTHESES
from app.services.backtest.dataset import build_backtest_dataset
from .constants import SNAPSHOT_VERSION_V1, SNAPSHOT_VERSION_V2

DEFAULT_ATOL = 1e-9
DEFAULT_RTOL = 1e-7
HYPOTHESIS_ID = "m7_return20_vol20_minus5_post20"

# Only columns persisted by both versions are audited.  In particular, the
# current schema has no realized_vol_10d or avg_dollar_volume_40d columns.
AUDITED_FEATURES = (
    "close", "return_5d", "return_10d", "return_20d", "return_40d",
    "realized_vol_5d", "realized_vol_20d", "realized_vol_40d",
    "avg_volume_5d", "avg_volume_20d", "avg_volume_40d",
    "avg_dollar_volume_5d", "avg_dollar_volume_20d",
    "volume_ratio_5d_to_20d", "post_ipo_high_to_date",
    "post_ipo_low_to_date", "drawdown_from_post_ipo_high",
    "position_in_post_ipo_range", "ipo_gain_retention",
)
UNAVAILABLE_CLASSIFICATIONS = {
    "observation_not_reached": "v2_observation_not_reached",
    "missing_observation_bar": "v2_missing_observation_bar",
}
CLASSIFICATIONS = (
    "exact_match", "observation_session_mismatch", "event_session_mismatch",
    "sparse_history_feature_mismatch", "v2_partial_missing_feature_history",
    "v2_missing_observation_bar", "v2_observation_not_reached",
    "no_v1_snapshot", "no_v2_snapshot",
    "numeric_feature_mismatch_complete_history",
)


def numeric_match(v1, v2, *, atol=DEFAULT_ATOL, rtol=DEFAULT_RTOL):
    """Compare persisted numerics with ``abs(a-b) <= atol + rtol*abs(a)``."""
    if v1 is None or v2 is None:
        return v1 is None and v2 is None
    a, b = float(v1), float(v2)
    return abs(a - b) <= atol + rtol * abs(a)


def _scalar(value):
    if isinstance(value, Decimal):
        return float(value)
    return value


@dataclass(frozen=True)
class PairAudit:
    ipo_id: int
    lockup_id: int
    security_id: int
    ticker: str | None
    observation_offset: int
    event_date: date | None
    v1_observation_date: date | None
    v2_observation_date: date | None
    v1_event_trade_date: date | None
    v2_event_trade_date: date | None
    v2_snapshot_status: str | None
    v2_unavailable_reason: str | None
    v2_calendar_id: str | None
    v2_calendar_provider: str | None
    v2_calendar_version: str | None
    expected_history_sessions: int | None
    missing_history_sessions: int | None
    observation_date_match: bool | None
    event_trade_date_match: bool | None
    exact_identity_match: bool | None
    classification: str
    feature_matches: dict[str, bool]
    feature_classifications: dict[str, str]
    feature_values: dict[str, dict[str, Any]]

    def to_dict(self):
        return asdict(self)


_POST_IPO_RANGE_FEATURES = frozenset({
    "post_ipo_high_to_date", "post_ipo_low_to_date",
    "drawdown_from_post_ipo_high", "position_in_post_ipo_range",
    "ipo_gain_retention",
})


def _feature_history_complete(name, v2):
    """Conservatively establish completeness of this feature's prerequisites.

    A non-null canonical fixed-window value can only be produced from its exact
    window.  The schema does not retain post-IPO completeness separately, so
    range-derived fields require the stronger whole-snapshot completeness flag.
    This deliberately avoids over-labelling range mismatches on partial rows.
    """
    if name in _POST_IPO_RANGE_FEATURES:
        return v2.snapshot_status == "complete"
    return getattr(v2, name) is not None


def _feature_classification(name, v1, v2, *, identity_match, atol, rtol):
    a, b = getattr(v1, name), getattr(v2, name)
    if a is None and b is None:
        return "both_null"
    if a is None:
        return "v1_null_v2_value"
    if b is None:
        return ("v1_value_v2_null_incomplete_history"
                if not _feature_history_complete(name, v2)
                else "v1_value_v2_null")
    if numeric_match(a, b, atol=atol, rtol=rtol):
        return "match"
    if identity_match and _feature_history_complete(name, v2):
        return "numeric_mismatch_complete_history"
    return "numeric_mismatch_other"


def _pair(v1, v2, ticker, *, atol, rtol):
    row = v1 or v2
    if v1 is None:
        classification = "no_v1_snapshot"
        observation_match = event_match = identity_match = None
    elif v2 is None:
        classification = "no_v2_snapshot"
        observation_match = event_match = identity_match = None
    else:
        observation_match = v1.observation_date == v2.observation_date
        event_match = v1.event_trade_date == v2.event_trade_date
        identity_match = observation_match and event_match
        classification = ""  # assigned after feature comparison
    matches = {}
    feature_classifications = {}
    values = {name: {"v1": _scalar(getattr(v1, name)) if v1 else None,
                     "v2": _scalar(getattr(v2, name)) if v2 else None}
              for name in AUDITED_FEATURES}
    if v1 is not None and v2 is not None:
        for name in AUDITED_FEATURES:
            a, b = getattr(v1, name), getattr(v2, name)
            matches[name] = numeric_match(a, b, atol=atol, rtol=rtol)
            feature_classifications[name] = _feature_classification(
                name, v1, v2, identity_match=identity_match, atol=atol, rtol=rtol)
        states = set(feature_classifications.values())
        any_mismatch = bool(states - {"match", "both_null"})
        if not observation_match:
            classification = "observation_session_mismatch"
        elif not event_match:
            classification = "event_session_mismatch"
        elif v2.unavailable_reason in UNAVAILABLE_CLASSIFICATIONS:
            classification = UNAVAILABLE_CLASSIFICATIONS[v2.unavailable_reason]
        elif "numeric_mismatch_complete_history" in states:
            classification = "numeric_feature_mismatch_complete_history"
        elif any_mismatch:
            classification = "sparse_history_feature_mismatch"
        elif v2.snapshot_status == "partial" and v2.unavailable_reason == "missing_feature_history":
            classification = "v2_partial_missing_feature_history"
        else:
            classification = "exact_match"
    return PairAudit(
        row.ipo_id, row.lockup_id, row.security_id, ticker, row.observation_offset,
        row.event_date, v1.observation_date if v1 else None,
        v2.observation_date if v2 else None, v1.event_trade_date if v1 else None,
        v2.event_trade_date if v2 else None, v2.snapshot_status if v2 else None,
        v2.unavailable_reason if v2 else None, v2.calendar_id if v2 else None,
        v2.calendar_provider if v2 else None, v2.calendar_version if v2 else None,
        v2.expected_history_sessions if v2 else None,
        v2.missing_history_sessions if v2 else None, observation_match, event_match,
        identity_match, classification, matches, feature_classifications, values)


def _m7_summary(db, pairs):
    spec = FROZEN_HYPOTHESES[HYPOTHESIS_ID]
    # No separate M7-membership table exists.  Re-run the original, frozen M7
    # service's exact dataset/cutoff/deduplication constraints without writing
    # or recomputing snapshots, outcomes, or membership.
    discovery = [r for r in build_backtest_dataset(db)
                 if r.get("observation_date") is not None and
                 r["observation_date"] <= spec.prospective_start_date and
                 r.get("observation_offset") == spec.observation_offset]
    unique = {}
    for row in discovery:
        unique.setdefault(row["lockup_id"], row)
    member_keys = {(r["lockup_id"], r["security_id"])
                   for r in unique.values()
                   if r.get(spec.feature1) is not None and
                   r.get(spec.feature2) is not None and
                   r.get(spec.outcome) is not None}
    rows = [p for p in pairs if p.observation_offset == spec.observation_offset and
            (p.lockup_id, p.security_id) in member_keys]
    statuses = Counter(p.v2_snapshot_status for p in rows)
    feature_results = {}
    crossings = []
    for name, threshold in ((spec.feature1, spec.feature1_threshold),
                            (spec.feature2, spec.feature2_threshold)):
        counts = Counter()
        for p in rows:
            vals = p.feature_values.get(name, {})
            state = p.feature_classifications.get(name)
            if state in ("match", "both_null"):
                counts["matches_v1"] += 1
            elif state == "numeric_mismatch_complete_history":
                counts["differs_complete_canonical_data"] += 1
            else:
                counts["differs_missing_canonical_data"] += 1
            if vals.get("v1") is not None and vals.get("v2") is not None and threshold is not None:
                v1_side = "low" if vals["v1"] <= threshold else "high"
                v2_side = "low" if vals["v2"] <= threshold else "high"
                if v1_side != v2_side:
                    counts["threshold_crossings"] += 1
                    crossings.append({"ticker": p.ticker, "lockup_id": p.lockup_id,
                                      "feature": name, "threshold": threshold,
                                      "v1": vals["v1"], "v2": vals["v2"],
                                      "v1_side": v1_side, "v2_side": v2_side})
        feature_results[name] = {
            key: counts[key] for key in ("matches_v1", "differs_missing_canonical_data",
                                          "differs_complete_canonical_data",
                                          "threshold_crossings")}
    return {
        "hypothesis_id": HYPOTHESIS_ID, "observation_offset": spec.observation_offset,
        "m7_rows_seen": len(rows),
        "m7_v2_exact_identity_matches": sum(p.exact_identity_match is True for p in rows),
        "m7_observation_mismatches": sum(p.observation_date_match is False for p in rows),
        "m7_event_mismatches": sum(p.event_trade_date_match is False for p in rows),
        "m7_v2_complete": statuses["complete"], "m7_v2_partial": statuses["partial"],
        "m7_v2_unavailable": statuses["unavailable"], "features": feature_results,
        "hypothetical_threshold_crossings": crossings,
    }


def summarize_m6_parity(db, pairs, *, max_examples=25, atol=DEFAULT_ATOL,
                        rtol=DEFAULT_RTOL):
    classifications = Counter(p.classification for p in pairs)
    statuses = Counter(p.v2_snapshot_status for p in pairs if p.v2_snapshot_status)
    reasons = Counter(p.v2_unavailable_reason for p in pairs if p.v2_unavailable_reason)
    by_offset = defaultdict(Counter)
    history = Counter()
    features = {name: Counter() for name in AUDITED_FEATURES}
    comparable = [p for p in pairs if p.feature_matches]
    for p in pairs:
        if p.v2_snapshot_status:
            by_offset[str(p.observation_offset)][p.v2_snapshot_status] += 1
        if p.expected_history_sessions is not None:
            history["expected_history_sessions"] += p.expected_history_sessions
            history["missing_history_sessions"] += p.missing_history_sessions or 0
        for name, matched in p.feature_matches.items():
            c = features[name]; c["compared"] += 1
            vals = p.feature_values[name]
            state = p.feature_classifications[name]
            if state == "match": c["matched"] += 1
            elif state != "both_null": c["mismatched"] += 1
            if state == "both_null": c["both_null"] += 1
            if vals["v1"] is None and vals["v2"] is not None: c["v1_null_v2_value"] += 1
            if vals["v1"] is not None and vals["v2"] is None: c["v1_value_v2_null"] += 1
            if state == "numeric_mismatch_complete_history":
                c["numeric_mismatch_complete_history"] += 1
            if state in ("v1_value_v2_null_incomplete_history", "numeric_mismatch_other"):
                c["incomplete_history_mismatch"] += 1
    result = {
        "audit": "m6_v1_v2_canonical_parity", "read_only": True,
        "numeric_tolerance": {"atol": atol, "rtol": rtol,
                              "formula": "abs(v1-v2) <= atol + rtol * abs(v1)"},
        "audited_features": list(AUDITED_FEATURES),
        "schema_features_not_available": ["realized_vol_10d", "avg_dollar_volume_40d"],
        "rows_seen": len(pairs), "comparable_pairs": len(comparable),
        "exact_identity_matches": sum(p.exact_identity_match is True for p in pairs),
        "observation_date_mismatches": sum(p.observation_date_match is False for p in pairs),
        "event_trade_date_mismatches": sum(p.event_trade_date_match is False for p in pairs),
        "v2_complete": statuses["complete"], "v2_partial": statuses["partial"],
        "v2_unavailable": statuses["unavailable"],
        "exact_feature_matches": sum(all(p.feature_matches.values()) for p in comparable),
        "explained_sparse_history_mismatches": classifications["sparse_history_feature_mismatch"],
        "unexplained_complete_history_mismatches": classifications["numeric_feature_mismatch_complete_history"],
        "no_v1_snapshot": classifications["no_v1_snapshot"],
        "no_v2_snapshot": classifications["no_v2_snapshot"],
        "classifications": {key: classifications[key] for key in CLASSIFICATIONS},
        "feature_parity": {name: {key: counts[key] for key in
                                   ("compared", "matched", "mismatched",
                                    "both_null", "v1_null_v2_value", "v1_value_v2_null",
                                    "numeric_mismatch_complete_history",
                                    "incomplete_history_mismatch")}
                           for name, counts in features.items()},
        "v2_completeness": {"by_status": {key: statuses[key] for key in
                                             ("complete", "partial", "unavailable")},
                            "by_unavailable_reason": dict(reasons),
                            "by_observation_offset": {k: dict(v) for k, v in sorted(by_offset.items(), key=lambda x: int(x[0]))},
                            **dict(history)},
        "mismatch_examples": [p.to_dict() for p in pairs
                              if p.classification != "exact_match"][:max_examples],
    }
    result["m7_frozen_hypothesis"] = _m7_summary(db, pairs)
    return result


def audit_m6_v1_v2(db, *, classification_status="classified",
                    candidate_type="operating_company_ipo", offering_status="priced",
                    primary_lockup_only=True, ticker=None, ipo_id=None, lockup_id=None,
                    limit=None, max_examples=25, atol=DEFAULT_ATOL, rtol=DEFAULT_RTOL):
    """Pair persisted versions and report without flush, assignment, or commit."""
    cohort = (select(LockupSignalSnapshot.lockup_id)
              .join(IPO, IPO.id == LockupSignalSnapshot.ipo_id)
              .join(Company, Company.id == IPO.company_id)
              .join(IPOLockup, IPOLockup.id == LockupSignalSnapshot.lockup_id))
    predicates = []
    if classification_status is not None: predicates.append(IPO.classification_status == classification_status)
    if candidate_type is not None: predicates.append(IPO.candidate_type == candidate_type)
    if offering_status is not None: predicates.append(IPO.offering_status == offering_status)
    if primary_lockup_only: predicates.append(IPOLockup.id == IPO.primary_lockup_id)
    if ticker: predicates.append(Company.ticker.ilike(ticker.strip()))
    if ipo_id is not None: predicates.append(IPO.id == ipo_id)
    if lockup_id is not None: predicates.append(IPOLockup.id == lockup_id)
    cohort = (cohort.where(*predicates).group_by(LockupSignalSnapshot.lockup_id)
              .order_by(func.min(LockupSignalSnapshot.event_date), LockupSignalSnapshot.lockup_id))
    if limit is not None: cohort = cohort.limit(limit)
    stmt = (select(LockupSignalSnapshot, Company.ticker)
            .join(IPO, IPO.id == LockupSignalSnapshot.ipo_id)
            .join(Company, Company.id == IPO.company_id)
            .where(LockupSignalSnapshot.lockup_id.in_(cohort),
                   LockupSignalSnapshot.snapshot_version.in_((SNAPSHOT_VERSION_V1, SNAPSHOT_VERSION_V2)))
            .order_by(LockupSignalSnapshot.lockup_id, LockupSignalSnapshot.security_id,
                      LockupSignalSnapshot.observation_offset, LockupSignalSnapshot.snapshot_version))
    grouped = defaultdict(dict); tickers = {}
    for snapshot, company_ticker in db.execute(stmt):
        key = (snapshot.lockup_id, snapshot.security_id, snapshot.observation_offset)
        grouped[key][snapshot.snapshot_version] = snapshot; tickers[key] = company_ticker
    pairs = [_pair(versions.get(SNAPSHOT_VERSION_V1), versions.get(SNAPSHOT_VERSION_V2),
                   tickers[key], atol=atol, rtol=rtol)
             for key, versions in grouped.items()]
    return summarize_m6_parity(db, pairs, max_examples=max_examples, atol=atol, rtol=rtol)
