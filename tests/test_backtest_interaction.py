"""Offline tests for the narrowly predefined M7 two-feature analysis."""
import sys

import pytest

from app.services.backtest.analysis import analyze_feature, analyze_two_feature_interaction
from scripts import analyze_lockup_backtest


def row(lockup_id, x, z, y, **extra):
    return {"lockup_id": lockup_id, "observation_offset": -5, "return_20d": x,
            "realized_vol_20d": z, "post_20d_return": y, **extra}


def test_four_fixed_groups_complete_cases_and_stored_excursions():
    rows = [
        row(1, 1, 10, -.25, bearish_mfe_20d=.3, bearish_mae_20d=.1),
        row(2, 2, 40, -.10, bearish_mfe_20d=.2, bearish_mae_20d=.2),
        row(3, 3, 20, .05, bearish_mfe_20d=.1, bearish_mae_20d=.3),
        row(4, 4, 30, -.05, bearish_mfe_20d=.4, bearish_mae_20d=.4),
    ]
    report = analyze_two_feature_interaction(
        rows, "return_20d", "realized_vol_20d", "post_20d_return", -5)

    assert report["feature1_median"] == 2.5
    assert report["feature2_median"] == 25
    assert {name: group["n_observations"] for name, group in report["groups"].items()} == {
        "low_low": 1, "low_high": 1, "high_low": 1, "high_high": 1}
    assert report["groups"]["low_low"]["magnitude_thresholds"]["le_20pct"] == {
        "count": 1, "rate": 1.0}
    assert report["groups"]["high_high"]["median_bearish_mfe"] == .4
    assert report["groups"]["high_high"]["median_bearish_mae"] == .4


def test_lte_median_semantics_empty_cell_and_determinism():
    rows = [row(1, 1, 1, 1), row(2, 1, 1, 2), row(3, 2, 2, 3)]
    first = analyze_two_feature_interaction(
        rows, "return_20d", "realized_vol_20d", "post_20d_return", -5)
    second = analyze_two_feature_interaction(
        list(reversed(rows)), "return_20d", "realized_vol_20d", "post_20d_return", -5)
    assert first["groups"] == second["groups"]
    assert first["groups"]["low_low"]["n_events"] == 2
    assert first["groups"]["low_high"]["n_observations"] == 0
    assert first["groups"]["low_high"]["mean_outcome"] is None


def test_missing_values_and_duplicate_event_are_not_weighted():
    rows = [row(1, 1, 1, 1), row(1, 99, 99, 99), row(2, None, 2, 2),
            row(3, 3, None, 3), row(4, 4, 4, None),
            {**row(5, 5, 5, 5), "observation_offset": -10}]
    report = analyze_two_feature_interaction(
        rows, "return_20d", "realized_vol_20d", "post_20d_return", -5)
    assert report["n_input_rows"] == 5
    assert report["n_valid_rows"] == report["n_events"] == report["n_observations"] == 1
    assert report["n_excluded_missing"] == 3


def test_ols_recovers_known_relationship_with_intercept_and_standardization():
    rows = [row(i, x, z, 2 + 3*x - 4*z) for i, (x, z) in enumerate(
        [(0, 0), (1, 0), (0, 1), (2, 1), (1, 3), (4, 2)], 1)]
    ols = analyze_two_feature_interaction(
        rows, "return_20d", "realized_vol_20d", "post_20d_return", -5)["ols"]
    assert ols["status"] == "ok"
    assert ols["intercept"] == pytest.approx(2)
    assert ols["feature1_coefficient"] == pytest.approx(3)
    assert ols["feature2_coefficient"] == pytest.approx(-4)
    assert ols["feature1_coefficient_sign"] == "positive"
    assert ols["feature2_coefficient_sign"] == "negative"
    assert ols["r_squared"] == pytest.approx(1)
    assert ols["standardized_beta_feature1"] is not None


@pytest.mark.parametrize("rows,status", [
    ([row(i, 1, i, i) for i in range(4)], "singular_design"),
    ([row(i, i, 2*i, i) for i in range(4)], "singular_design"),
    ([row(i, i, i*i, i) for i in range(3)], "insufficient_sample"),
])
def test_regression_degeneracy_is_safe(rows, status):
    ols = analyze_two_feature_interaction(
        rows, "return_20d", "realized_vol_20d", "post_20d_return", -5)["ols"]
    assert ols["status"] == status
    assert ols["intercept"] is None


def test_allowlists_apply_to_both_features_and_outcome():
    with pytest.raises(ValueError, match="pre-event"):
        analyze_two_feature_interaction([], "post_20d_return", "return_20d",
                                        "post_20d_return", -5)
    with pytest.raises(ValueError, match="pre-event"):
        analyze_two_feature_interaction([], "return_20d", "post_20d_return",
                                        "post_20d_return", -5)
    with pytest.raises(ValueError, match="retrospective"):
        analyze_two_feature_interaction([], "return_20d", "realized_vol_20d",
                                        "return_20d", -5)


@pytest.mark.parametrize("arguments,expected", [
    (["tool", "--interaction", "--offset", "-5"], "--second-feature"),
    (["tool", "--interaction", "--second-feature", "realized_vol_20d"], "--offset"),
    (["tool", "--robustness"], "--interaction"),
])
def test_cli_interaction_requirements(monkeypatch, capsys, arguments, expected):
    monkeypatch.setattr(sys, "argv", arguments)
    with pytest.raises(SystemExit): analyze_lockup_backtest.main()
    assert expected in capsys.readouterr().err


def test_existing_single_feature_shape_is_unchanged():
    report = analyze_feature([row(1, 1, 2, -.1)], "return_20d", "post_20d_return", -5)
    assert set(report) == {"feature", "outcome", "repeated_measures_by_lockup",
                           "p_values_are_exploratory", "offsets"}
