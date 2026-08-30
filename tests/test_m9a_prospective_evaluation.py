"""Focused tests for the read-only M9A v1 scorecard."""
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.main import app
from app.models import Company, Filing, IPO, IPOLockup, LockupProspectiveSignal
from app.services.backtest.analysis import FROZEN_HYPOTHESES
from app.services.prospective.m9a_evaluation import evaluate_m9a_prospective
from app.services.research_dashboard import get_m9a_dashboard_payload

HYPOTHESIS = "m7_return20_vol20_minus5_post20"
VERSION = FROZEN_HYPOTHESES[HYPOTHESIS].analysis_version


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = Session(engine)
    company = Company(cik="0000000099", name="M9A Co", ticker="M9A")
    filing = Filing(company=company, form_type="424B4", filed_at=date(2026, 1, 2),
                    accession_number="m9a", filing_path="m9a.txt", sec_url="https://example.test")
    ipo = IPO(company=company, ipo_date=date(2026, 1, 2), ipo_price=10,
              classification_status="classified", candidate_type="operating_company_ipo",
              offering_status="priced")
    lockup = IPOLockup(ipo=ipo, filing=filing, holder_group="all", lockup_type="standard",
                       stated_expiration_date=date(2026, 9, 1), confidence=.9,
                       parser_name="test", parser_version="1", source_excerpt="x",
                       source_locator="x", evidence_key="m9a")
    session.add(lockup); session.flush(); ipo.primary_lockup_id = lockup.id
    session.commit()
    yield session, ipo, lockup
    session.close()


def add_signal(db, ipo, lockup, *, mode="strict_prospective", group="high_high",
               outcome=None, hypothesis=HYPOTHESIS, status=None):
    row = LockupProspectiveSignal(
        hypothesis_id=hypothesis, hypothesis_version=VERSION, ipo_id=lockup.ipo_id,
        lockup_id=lockup.id, observation_offset=-5, observation_date=date(2026, 8, 25),
        event_date=date(2026, 9, 1), feature1_name="return_20d",
        feature1_value=Decimal("0.1"), feature1_threshold=Decimal("0.03"),
        feature2_name="realized_vol_20d", feature2_value=Decimal("0.9"),
        feature2_threshold=Decimal("0.8"), interaction_group=group,
        is_high_high=group == "high_high", signal_status=status or ("matured" if outcome is not None else "awaiting_outcome"),
        evaluation_mode=mode, realized_outcome_name="post_20d_return" if outcome is not None else None,
        realized_outcome_value=outcome, created_at=datetime(2026, 8, 25, tzinfo=UTC))
    db.add(row); db.flush()
    return row


def create_synthetic_lockup(db, ipo, suffix):
    company = Company(cik=f"{100000 + suffix:010d}", name=f"M9A Clone {suffix}",
                      ticker=f"M9A{suffix}")
    filing = Filing(company=company, form_type="424B4", filed_at=date(2026, 1, 2),
                    accession_number=f"m9a-clone-{suffix}",
                    filing_path=f"m9a-clone-{suffix}.txt", sec_url="https://example.test")
    clone_ipo = IPO(company=company, ipo_date=ipo.ipo_date, ipo_price=ipo.ipo_price,
                    classification_status="classified", candidate_type="operating_company_ipo",
                    offering_status="priced")
    clone = IPOLockup(ipo=clone_ipo, filing=filing, holder_group="all",
        lockup_type="standard", stated_expiration_date=date(2026, 9, 1), confidence=.9,
        parser_name="test", parser_version="1", source_excerpt="x", source_locator="x",
        evidence_key=f"m9a-clone-{suffix}")
    db.add(clone); db.flush(); clone_ipo.primary_lockup_id = clone.id
    return clone


@pytest.mark.parametrize(("field", "outside_value"), [
    ("classification_status", "needs_review"),
    ("candidate_type", "spac"),
    ("offering_status", "withdrawn"),
])
def test_ipo_cohort_predicates_independently_exclude_signal(db, field, outside_value):
    session, ipo, lockup = db
    included = add_signal(session, ipo, lockup, outcome=Decimal("-0.1"))
    setattr(ipo, field, outside_value)
    session.commit()

    result = evaluate_m9a_prospective(session)

    assert result["population"]["total_prospective_signals"] == 0
    assert all(row["signal_id"] != included.id for row in result["observations"])


def test_non_primary_persisted_lockup_is_excluded(db):
    session, ipo, primary_lockup = db
    non_primary = IPOLockup(
        ipo_id=ipo.id, filing_id=primary_lockup.filing_id, holder_group="all",
        lockup_type="standard", stated_expiration_date=date(2026, 9, 2), confidence=.9,
        parser_name="test", parser_version="1", source_excerpt="x", source_locator="x",
        evidence_key="m9a-non-primary")
    session.add(non_primary); session.flush()
    excluded = add_signal(session, ipo, non_primary, outcome=Decimal("-0.1"))
    session.commit()

    result = evaluate_m9a_prospective(session)

    assert ipo.primary_lockup_id == primary_lockup.id
    assert result["population"]["total_prospective_signals"] == 0
    assert all(row["signal_id"] != excluded.id for row in result["observations"])


def test_modes_historical_hypothesis_and_missing_outcome_are_isolated(db):
    session, ipo, lockup = db
    add_signal(session, ipo, lockup, outcome=Decimal("-0.1"))
    add_signal(session, ipo, create_synthetic_lockup(session, ipo, 2), mode="shadow_prospective", outcome=Decimal("-0.2"))
    add_signal(session, ipo, create_synthetic_lockup(session, ipo, 3), mode="historical", outcome=Decimal("-0.3"))
    add_signal(session, ipo, create_synthetic_lockup(session, ipo, 4), hypothesis="another", outcome=Decimal("-0.4"))
    add_signal(session, ipo, create_synthetic_lockup(session, ipo, 5), outcome=None)
    session.commit()
    strict = evaluate_m9a_prospective(session)
    shadow = evaluate_m9a_prospective(session, evaluation_mode="shadow_prospective")
    assert strict["population"] == {"total_prospective_signals": 2, "pending_immature_signals": 1,
        "matured_signals": 1, "target_group_signals": 2, "matured_target_group_signals": 1}
    assert shadow["population"]["total_prospective_signals"] == 1
    assert shadow["interpretation_readiness"]["status"] == "descriptive_only"


def test_classification_groups_statistics_and_json(db):
    session, ipo, lockup = db
    values = [("high_high", "-0.2"), ("high_high", "0"), ("high_low", "0.1"),
              ("low_high", "-0.4")]
    for i, (group, value) in enumerate(values):
        add_signal(session, ipo, lockup if i == 0 else create_synthetic_lockup(session, ipo, i),
                   group=group, outcome=Decimal(value))
    add_signal(session, ipo, create_synthetic_lockup(session, ipo, 9), group="low_low")
    session.commit()
    result = evaluate_m9a_prospective(session)
    assert result["primary_outcome"] == {"matured_bearish_outcomes": 2,
        "matured_non_bearish_outcomes": 2, "bearish_outcome_rate": .5,
        "target_bearish_hits": 1, "target_non_bearish_outcomes": 1,
        "target_bearish_hit_rate": .5}
    assert result["continuous_outcome"]["median_post_20d_return"] == pytest.approx(-.1)
    assert result["continuous_outcome"]["target_median_post_20d_return"] == pytest.approx(-.1)
    assert result["continuous_outcome"]["non_target_median_post_20d_return"] == pytest.approx(-.15)
    assert result["continuous_outcome"]["target_minus_non_target_median_return"] == pytest.approx(.05)
    assert set(result["group_breakdown"]) == {"high_high", "high_low", "low_high", "low_low"}
    assert result["group_breakdown"]["low_low"]["matured_signals"] == 0
    assert result["group_breakdown"]["low_low"]["mean_post_20d_return"] is None
    assert [o["bearish_hit"] for o in result["observations"][:4]] == [True, False, False, True]
    assert result["observations"][-1]["bearish_hit"] is None
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize(("total", "targets", "ready"), [(19, 5, False), (20, 4, False), (20, 5, True)])
def test_strict_readiness_thresholds(db, total, targets, ready):
    session, ipo, lockup = db
    for i in range(total):
        add_signal(session, ipo, lockup if i == 0 else create_synthetic_lockup(session, ipo, i),
                   group="high_high" if i < targets else "low_low", outcome=Decimal("-0.01"))
    session.commit()
    result = evaluate_m9a_prospective(session)
    assert result["interpretation_readiness"]["eligible_for_provisional_interpretation"] is ready
    assert result["interpretation_readiness"]["status"] == ("provisional_interpretation_ready" if ready else "descriptive_only")


def test_evaluation_is_read_only_and_idempotent(db):
    session, ipo, lockup = db
    row = add_signal(session, ipo, lockup, outcome=Decimal("-0.1")); session.commit()
    before = {c.name: getattr(row, c.name) for c in row.__table__.columns}
    first = evaluate_m9a_prospective(session); second = evaluate_m9a_prospective(session)
    session.expire_all(); persisted = session.get(LockupProspectiveSignal, row.id)
    assert first == second
    assert {c.name: getattr(persisted, c.name) for c in persisted.__table__.columns} == before
    assert session.scalar(select(func.count()).select_from(LockupProspectiveSignal)) == 1


def test_m9a_get_endpoint_returns_both_evaluations(db):
    session, ipo, lockup = db
    add_signal(session, ipo, lockup, outcome=None)
    add_signal(session, ipo, create_synthetic_lockup(session, ipo, 77),
               mode="shadow_prospective", group="high_low", outcome=None)
    session.commit()

    def override_db():
        yield session

    app.dependency_overrides[get_db] = override_db
    try:
        response = TestClient(app).get("/api/research/m9a-evaluation")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["strict_prospective"]["population"]["total_prospective_signals"] == 1
    assert body["shadow_prospective"]["population"]["total_prospective_signals"] == 1
    assert body["strict_prospective"]["observations"][0]["evaluation_mode"] == "strict_prospective"
    assert body["shadow_prospective"]["observations"][0]["evaluation_mode"] == "shadow_prospective"


def test_dashboard_payload_returns_separate_strict_and_shadow_evaluations_read_only(db):
    session, ipo, lockup = db
    strict_row = add_signal(session, ipo, lockup, group="high_high", outcome=None)
    shadow_lockup = create_synthetic_lockup(session, ipo, 42)
    shadow_row = add_signal(
        session, ipo, shadow_lockup, mode="shadow_prospective",
        group="high_low", outcome=Decimal("-0.25"))
    session.commit()
    before = session.scalar(select(func.count()).select_from(LockupProspectiveSignal))

    payload = get_m9a_dashboard_payload(session)

    assert set(payload) == {"strict_prospective", "shadow_prospective"}
    strict = payload["strict_prospective"]
    shadow = payload["shadow_prospective"]
    assert [row["signal_id"] for row in strict["observations"]] == [strict_row.id]
    assert [row["signal_id"] for row in shadow["observations"]] == [shadow_row.id]
    assert strict["population"]["matured_signals"] == 0
    assert strict["observations"][0]["post_20d_return"] is None
    assert strict["observations"][0]["bearish_hit"] is None
    assert strict["observations"][0]["signal_status"] == "awaiting_outcome"
    assert shadow["observations"][0]["ticker"] == "M9A42"
    assert shadow["observations"][0]["post_20d_return"] == -.25
    assert shadow["interpretation_readiness"]["threshold_population"] == "strict_prospective"
    assert shadow["interpretation_readiness"]["eligible_for_provisional_interpretation"] is False
    assert not session.new and not session.dirty and not session.deleted
    assert session.scalar(select(func.count()).select_from(LockupProspectiveSignal)) == before


def test_m9a_dashboard_uses_one_combined_get_endpoint_and_null_safe_rendering():
    routes = Path("app/api/routes.py").read_text(encoding="utf-8")
    page = Path("app/templates/index.html").read_text(encoding="utf-8")

    assert '@router.get("/research/m9a-evaluation")' in routes
    assert "json('/api/research/m9a-evaluation')" in page
    assert "json('/api/research/prospective-evaluation')" not in page
    assert "json('/api/research/shadow-evaluation')" not in page
    assert "o.bearish_hit===null?'—'" in page
    assert "pct(o.post_20d_return,true)" in page
    assert "SHADOW / DESCRIPTIVE" in page
    assert "Shadow observations never contribute" in page
