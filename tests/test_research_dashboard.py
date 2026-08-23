"""Offline checks for the read-only research dashboard projection."""
from datetime import date
from pathlib import Path

from app.services.backtest.analysis import FROZEN_HYPOTHESES
from app.services.research_dashboard import HYPOTHESIS_ID, hypothesis_metadata


def test_hypothesis_metadata_is_registry_projection():
    spec = FROZEN_HYPOTHESES[HYPOTHESIS_ID]
    payload = hypothesis_metadata()

    assert payload["feature1"]["threshold"] is spec.feature1_threshold
    assert payload["feature2"]["threshold"] is spec.feature2_threshold
    assert payload["prospective_start_date"] == date(2026, 8, 23)
    assert payload["observation_offset"] == spec.observation_offset


def test_root_is_research_dashboard_without_legacy_actions_or_raw_row_navigation():
    page = Path("app/templates/index.html").read_text(encoding="utf-8")

    assert "Lockup Expiration Research Dashboard" in page
    assert "FROZEN HYPOTHESIS" in page
    assert "PROSPECTIVE · OUT-OF-SAMPLE" in page
    assert "NOT OUT-OF-SAMPLE" in page
    assert "Ingest last 365 days" not in page
    assert "window.location='/api/ipos/" not in page
    assert "v??'—'" in page  # the escaping helper has an explicit null fallback


def test_research_routes_are_get_only():
    routes = Path("app/api/routes.py").read_text(encoding="utf-8")
    assert routes.count('@router.get("/research/') == 6
    assert '@router.post("/research/' not in routes
