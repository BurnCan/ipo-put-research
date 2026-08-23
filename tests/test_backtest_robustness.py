"""Offline robustness tests for the frozen M7 interaction hypothesis."""
import pytest

from app.services.backtest.analysis import (FROZEN_HYPOTHESES,
                                            analyze_two_feature_interaction)


def row(lockup_id, x, z, y, **extra):
    return {"lockup_id": lockup_id, "observation_offset": -5,
            "return_20d": x, "realized_vol_20d": z,
            "post_20d_return": y, **extra}


def sample():
    points = [(0, 0), (1, 0), (0, 1), (2, 1), (1, 3), (4, 2)]
    return [row(i, x, z, 2 + 3*x - 4*z + (i % 2)*.1,
                ticker=None if i == 2 else f"T{i}")
            for i, (x, z) in enumerate(points, 1)]


def test_leave_one_out_count_exclusion_order_and_summary():
    report = analyze_two_feature_interaction(
        sample(), "return_20d", "realized_vol_20d", "post_20d_return", -5,
        robustness=True)
    robust = report["robustness"]
    assert robust["hypothesis_id"] == "m7_return20_vol20_minus5_post20"
    runs = robust["leave_one_out"]
    assert len(runs) == report["n_valid_rows"] == 6
    assert [run["excluded_lockup_id"] for run in runs] == list(range(1, 7))
    assert all(run["n"] == 5 for run in runs)
    assert runs[1]["excluded_ticker"] is None
    assert robust["run_counts"] == {"successful_runs": 6, "failed_runs": 0,
                                     "total_runs": 6}
    values = [run["feature1_coefficient"] for run in runs]
    summary = robust["coefficient_summary"]["feature1_coefficient"]
    assert summary["full_sample_value"] == report["ols"]["feature1_coefficient"]
    assert summary["min"] == min(values)
    assert summary["max"] == max(values)
    assert summary["mean"] == pytest.approx(sum(values)/len(values))


def test_singular_run_is_retained_and_counted_failed():
    # The first four form a full-rank design; omitting point 4 leaves collinearity.
    rows = [row(1, 0, 0, 0), row(2, 1, 1, 1), row(3, 2, 2, 2), row(4, 0, 1, -1)]
    robust = analyze_two_feature_interaction(
        rows, "return_20d", "realized_vol_20d", "post_20d_return", -5,
        robustness=True)["robustness"]
    assert len(robust["leave_one_out"]) == 4
    assert all(run["status"] == "insufficient_sample" for run in robust["leave_one_out"])
    assert robust["run_counts"] == {"successful_runs": 0, "failed_runs": 4,
                                     "total_runs": 4}


def test_reduced_medians_change_high_high_membership_and_empty_is_safe():
    rows = [row(1, 0, 0, -.1), row(2, 1, 10, -.2), row(3, 2, 1, -.3),
            row(4, 3, 3, -.4), row(5, 4, 4, -.5)]
    runs = analyze_two_feature_interaction(
        rows, "return_20d", "realized_vol_20d", "post_20d_return", -5,
        robustness=True)["robustness"]["leave_one_out"]
    # Excluding the top point promotes lockup 4 into high_high; fixed membership
    # would instead leave that cell empty.
    assert runs[-1]["n_high_high"] == 1
    assert runs[-1]["median_outcome"] == -.4

    empty_rows = [row(1, 0, 2, 0), row(2, 1, 1, 0), row(3, 2, 0, 0), row(4, 3, -1, 0)]
    robust = analyze_two_feature_interaction(
        empty_rows, "return_20d", "realized_vol_20d", "post_20d_return", -5,
        robustness=True)["robustness"]
    assert robust["high_high_summary"]["empty_high_high_runs"] == 4
    assert robust["high_high_summary"]["n_high_high"]["min"] == 0
    assert robust["high_high_summary"]["median_outcome"]["median"] is None


def test_predictions_residuals_leverage_cooks_and_ranking_are_exact():
    rows = sample()
    robust = analyze_two_feature_interaction(
        rows, "return_20d", "realized_vol_20d", "post_20d_return", -5,
        robustness=True)["robustness"]
    influence = robust["influence"]
    assert influence["ranking_rule"] == "cooks_distance_desc_then_lockup_id"
    for diagnostic in influence["rows"]:
        assert diagnostic["predicted_outcome"] + diagnostic["residual"] == pytest.approx(
            diagnostic["actual_outcome"])
        assert 0 <= diagnostic["leverage"] <= 1
        assert diagnostic["cooks_distance"] >= 0
    assert sum(item["leverage"] for item in influence["rows"]) == pytest.approx(3)
    expected = sorted(influence["rows"],
                      key=lambda item: (-item["cooks_distance"], item["lockup_id"]))
    assert influence["rows"] == expected
    assert influence["top_5"] == expected[:5]


def test_frozen_spec_and_non_robust_shape_are_stable():
    frozen = FROZEN_HYPOTHESES["m7_return20_vol20_minus5_post20"]
    assert (frozen.feature1, frozen.feature2, frozen.outcome,
            frozen.observation_offset, frozen.grouping_rule,
            frozen.analysis_version) == (
                "return_20d", "realized_vol_20d", "post_20d_return", -5,
                "median_split", "m7_robustness_v1")
    ordinary = analyze_two_feature_interaction(
        sample(), frozen.feature1, frozen.feature2, frozen.outcome,
        frozen.observation_offset)
    assert "robustness" not in ordinary
