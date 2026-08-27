"""Offline tests for explicit daily-pipeline execution provenance."""
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import Company, DailyPrice, PipelineRun, PipelineStageRun, Security
from app.services.pipeline_runs import (finish_run, finish_stage, get_pipeline_status,
                                        start_run, start_stage)
from app.services.schema_upgrade import upgrade_pipeline_runs


def database():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return engine, Session(engine)


def test_successful_run_and_stage_are_recorded():
    _engine, db = database()
    run = start_run(db, trigger="cron", stages_total=1)
    stage = start_stage(db, run.id, "market_history")
    finish_stage(db, stage.id, exit_code=0)
    finish_run(db, run.id, exit_code=0)
    db.refresh(run); db.refresh(stage)
    assert (run.status, run.exit_code, run.stages_succeeded, run.stages_failed) == (
        "succeeded", 0, 1, 0)
    assert run.finished_at and stage.status == "succeeded" and stage.finished_at


def test_failed_stage_and_run_preserve_previous_success():
    _engine, db = database()
    prior = start_run(db); finish_run(db, prior.id, exit_code=0)
    failed = start_run(db, stages_total=1)
    stage = start_stage(db, failed.id, "m6_analysis")
    finish_stage(db, stage.id, exit_code=7, error="provider failed\ntrace omitted")
    finish_run(db, failed.id, exit_code=7, error="m6_analysis failed")
    status = get_pipeline_status(db)
    assert status["last_run"]["id"] == failed.id
    assert status["last_run"]["status"] == "failed"
    assert status["last_run"]["exit_code"] == 7
    assert status["last_run"]["stages_failed"] == 1
    assert status["last_run"]["stages"][0]["stage_name"] == "m6_analysis"
    assert "\n" not in status["last_run"]["stages"][0]["error_summary"]
    assert status["last_successful_run"]["id"] == prior.id


def test_already_running_is_latest_without_replacing_previous_success():
    _engine, db = database()
    prior = start_run(db); finish_run(db, prior.id, exit_code=0)
    blocked = start_run(db, stages_total=3)
    finish_run(db, blocked.id, exit_code=1, status="already_running",
               error="pipeline lock is already held")

    status = get_pipeline_status(db)
    db.refresh(blocked)
    assert status["last_run"]["id"] == blocked.id
    assert status["last_run"]["status"] == "already_running"
    assert status["last_successful_run"]["id"] == prior.id
    assert blocked.finished_at is not None
    assert blocked.stages_failed == 0
    assert db.scalars(select(PipelineStageRun).where(
        PipelineStageRun.pipeline_run_id == blocked.id)).all() == []


def test_distinct_same_day_runs_and_latest_actual_execution():
    _engine, db = database()
    first = start_run(db); finish_run(db, first.id, exit_code=0)
    second = start_run(db); finish_run(db, second.id, exit_code=0)
    assert first.id != second.id
    assert db.query(PipelineRun).count() == 2
    assert get_pipeline_status(db)["last_run"]["id"] == second.id


def test_no_run_and_market_date_are_independent_of_success():
    _engine, db = database()
    assert get_pipeline_status(db) == {"pipeline_name": "daily_pipeline", "last_run": None,
                                       "last_successful_run": None, "latest_market_date": None}
    company = Company(cik="1", name="Test", ticker="T")
    security = Security(company=company, ticker="T", source="test")
    db.add(DailyPrice(security=security, trade_date=date(2020, 1, 2), open=1, high=1,
                      low=1, close=1, volume=1, provider="test", provider_symbol="T"))
    db.commit()
    run = start_run(db); finish_run(db, run.id, exit_code=0)
    status = get_pipeline_status(db)
    assert status["last_run"]["status"] == "succeeded"
    assert status["latest_market_date"] == date(2020, 1, 2)


def test_duration_and_utc_serialization():
    _engine, db = database()
    run = start_run(db)
    run.started_at = datetime(2026, 8, 27, 18, 30, tzinfo=UTC)
    run.finished_at = run.started_at + timedelta(seconds=42)
    run.status = "succeeded"; run.exit_code = 0
    db.commit()
    result = get_pipeline_status(db)["last_run"]
    assert result["duration_seconds"] == 42
    assert result["started_at"].utcoffset() == timedelta(0)


def test_pipeline_schema_upgrade_is_idempotent():
    engine = create_engine("sqlite://")
    # The operational tables have no dependency on the research tables.
    assert upgrade_pipeline_runs(engine) == ["pipeline_runs", "pipeline_stage_runs"]
    assert upgrade_pipeline_runs(engine) == []
    assert {"pipeline_runs", "pipeline_stage_runs"} <= set(inspect(engine).get_table_names())
