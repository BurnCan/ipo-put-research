"""Durable operational provenance for pipeline and stage executions."""
from __future__ import annotations

import socket
from datetime import UTC, datetime

from sqlalchemy import func, select

from app.models import DailyPrice, PipelineRun, PipelineStageRun

PIPELINE_NAME = "daily_pipeline"


def now_utc() -> datetime:
    return datetime.now(UTC)


def concise_error(error: object | None) -> str | None:
    if error is None:
        return None
    return str(error).replace("\n", " ")[:500]


def start_run(db, *, trigger: str = "manual", stages_total: int = 3) -> PipelineRun:
    run = PipelineRun(pipeline_name=PIPELINE_NAME, started_at=now_utc(), status="running",
                      trigger=trigger, hostname=socket.gethostname(), stages_total=stages_total)
    db.add(run); db.commit(); db.refresh(run)
    return run


def start_stage(db, run_id: int, stage_name: str) -> PipelineStageRun:
    stage = PipelineStageRun(pipeline_run_id=run_id, stage_name=stage_name,
                             started_at=now_utc(), status="running")
    db.add(stage); db.commit(); db.refresh(stage)
    return stage


def finish_stage(db, stage_id: int, *, exit_code: int, error: object | None = None):
    stage = db.get(PipelineStageRun, stage_id)
    stage.finished_at = now_utc()
    stage.exit_code = exit_code
    stage.status = "succeeded" if exit_code == 0 else "failed"
    stage.error_summary = concise_error(error)
    db.commit()


def finish_run(db, run_id: int, *, exit_code: int, error: object | None = None):
    run = db.get(PipelineRun, run_id)
    run.finished_at = now_utc()
    run.exit_code = exit_code
    run.status = "succeeded" if exit_code == 0 else "failed"
    run.error_summary = concise_error(error)
    stages = db.scalars(select(PipelineStageRun).where(
        PipelineStageRun.pipeline_run_id == run_id)).all()
    run.stages_succeeded = sum(s.status == "succeeded" for s in stages)
    run.stages_failed = sum(s.status == "failed" for s in stages)
    db.commit()


def _aware(value):
    return value.replace(tzinfo=UTC) if value is not None and value.tzinfo is None else value


def _stage_dict(stage):
    return {"stage_name": stage.stage_name, "started_at": _aware(stage.started_at),
            "finished_at": _aware(stage.finished_at), "status": stage.status,
            "exit_code": stage.exit_code, "error_summary": stage.error_summary}


def _run_dict(run, *, include_stages=False):
    if run is None:
        return None
    started, finished = _aware(run.started_at), _aware(run.finished_at)
    result = {name: getattr(run, name) for name in
              ("id", "pipeline_name", "status", "exit_code", "trigger", "hostname",
               "stages_total", "stages_succeeded", "stages_failed", "error_summary")}
    result.update(started_at=started, finished_at=finished,
                  duration_seconds=((finished - started).total_seconds() if finished else None))
    if include_stages:
        result["stages"] = [_stage_dict(s) for s in sorted(run.stages, key=lambda s: s.id)]
    return result


def get_pipeline_status(db):
    order = (PipelineRun.started_at.desc(), PipelineRun.id.desc())
    last = db.scalar(select(PipelineRun).where(PipelineRun.pipeline_name == PIPELINE_NAME).order_by(*order))
    successful = db.scalar(select(PipelineRun).where(
        PipelineRun.pipeline_name == PIPELINE_NAME, PipelineRun.status == "succeeded").order_by(*order))
    return {"pipeline_name": PIPELINE_NAME, "last_run": _run_dict(last, include_stages=True),
            "last_successful_run": _run_dict(successful),
            "latest_market_date": db.scalar(select(func.max(DailyPrice.trade_date)))}
