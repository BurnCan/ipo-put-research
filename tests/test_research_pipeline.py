import fcntl
import importlib.util
import json
import subprocess
import sys
import types
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.db
from app.db import Base
from app.models import PipelineRun, PipelineStageRun


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "update_research_pipeline.py"
SPEC = importlib.util.spec_from_file_location("update_research_pipeline", SCRIPT)
pipeline = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(pipeline)


def test_success_order_results_timestamps_cohort_and_frozen_hypothesis(monkeypatch):
    calls = []

    def market():
        calls.append("market")
        return {"bars": 2}

    def m6():
        calls.append("m6")
        return {"snapshots": 3}

    def m8(**kwargs):
        calls.append(("m8", kwargs))
        return {"signals": 1}

    report = pipeline.run_pipeline(market_stage=market, m6_stage=m6, m8_stage=m8)
    assert report["status"] == "ok"
    assert calls == ["market", "m6", ("m8", {"dry_run": False})]
    assert report["started_at"].endswith("+00:00")
    assert report["finished_at"].endswith("+00:00")
    assert report["stages"]["market_history"]["result"] == {"bars": 2}
    assert report["stages"]["m6_analysis"]["result"] == {"snapshots": 3}
    assert report["stages"]["m8_prospective"]["result"] == {"signals": 1}
    assert pipeline.FROZEN_HYPOTHESIS_ID == "m7_return20_vol20_minus5_post20"
    assert pipeline.COHORT == {
        "classification_status": "classified",
        "candidate_type": "operating_company_ipo",
        "offering_status": "priced",
        "primary_lockup_only": True,
    }


@pytest.mark.parametrize("failed_stage", ["market", "m6"])
def test_failure_is_fail_fast(failed_stage):
    calls = []

    def stage(name):
        def operation():
            calls.append(name)
            if name == failed_stage:
                raise RuntimeError("offline failure")
            return {}
        return operation

    report = pipeline.run_pipeline(
        market_stage=stage("market"), m6_stage=stage("m6"),
        m8_stage=lambda **kwargs: calls.append("m8"),
    )
    assert report["status"] == "failed"
    assert report["failed_stage"] == ("market_history" if failed_stage == "market" else "m6_analysis")
    assert "m8" not in calls
    assert report["finished_at"].endswith("+00:00")


def test_main_exit_logging_parent_creation_dry_run_and_skips(tmp_path, monkeypatch, capsys):
    seen = []

    def fake_run(**kwargs):
        seen.append(kwargs)
        return {"status": "ok", "started_at": "a", "finished_at": "b", "stages": {
            "market_history": {"status": "skipped"}, "m6_analysis": {"status": "skipped"},
            "m8_prospective": {"status": "ok", "result": {}},
        }}

    monkeypatch.setattr(pipeline, "run_pipeline", fake_run)
    log = tmp_path / "missing" / "daily.log"
    lock = tmp_path / "pipeline.lock"
    assert pipeline.main(["--dry-run", "--skip-market-history", "--skip-m6",
                          "--log-file", str(log), "--lock-file", str(lock)]) == 0
    assert seen[0]["dry_run"] is True
    assert seen[0]["skip_market_history"] is True
    assert json.loads(capsys.readouterr().out)["status"] == "ok"
    assert json.loads(log.read_text())['status'] == "ok"
    pipeline._append_log(log, {"status": "second"})
    assert len(log.read_text().splitlines()) == 2


def test_main_failed_status_has_nonzero_exit(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "run_pipeline", lambda **kwargs: {
        "status": "failed", "started_at": "a", "finished_at": "b", "stages": {},
        "failed_stage": "market_history", "error": "failure",
    })
    assert pipeline.main(["--lock-file", str(tmp_path / "lock")]) == 1


def test_lock_prevents_overlap_and_releases_after_success_and_exception(tmp_path):
    lock = tmp_path / "lock"
    with pipeline.pipeline_lock(lock):
        with pytest.raises(pipeline.AlreadyRunningError):
            with pipeline.pipeline_lock(lock):
                pass
    with pipeline.pipeline_lock(lock):
        pass
    with pytest.raises(RuntimeError):
        with pipeline.pipeline_lock(lock):
            raise RuntimeError("boom")
    with pipeline.pipeline_lock(lock):
        pass


def test_already_running_main_status_is_persisted_without_failed_stage(
        tmp_path, capsys, monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    monkeypatch.setattr(app.db, "SessionLocal", lambda: Session(engine))
    lock = tmp_path / "lock"
    lock.touch()
    with lock.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert pipeline.main(["--lock-file", str(lock)]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "already_running"
    with Session(engine) as db:
        runs = db.scalars(select(PipelineRun)).all()
        assert len(runs) == 1
        assert runs[0].status == "already_running"
        assert runs[0].exit_code == 1
        assert runs[0].finished_at is not None
        assert runs[0].stages_failed == 0
        assert db.scalars(select(PipelineStageRun)).all() == []


def test_script_help_is_cwd_independent_and_noninteractive(tmp_path):
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"], cwd=tmp_path,
        text=True, capture_output=True, timeout=10, check=False,
    )
    assert completed.returncode == 0
    assert "--dry-run" in completed.stdout


def test_project_root_is_on_sys_path_for_runtime_imports():
    original_sys_path = sys.path.copy()
    try:
        sys.path[:] = [entry for entry in sys.path if entry != str(ROOT)]
        isolated_spec = importlib.util.spec_from_file_location("isolated_pipeline", SCRIPT)
        isolated_pipeline = importlib.util.module_from_spec(isolated_spec)
        assert isolated_spec.loader is not None
        isolated_spec.loader.exec_module(isolated_pipeline)
        assert sys.path[0] == str(ROOT)
    finally:
        sys.path[:] = original_sys_path


def test_direct_script_run_is_cwd_independent(tmp_path):
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--skip-market-history", "--skip-m6", "--skip-m8",
         "--lock-file", str(tmp_path / "pipeline.lock")],
        cwd=tmp_path, text=True, capture_output=True, timeout=10, check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["status"] == "ok"


def test_main_restores_callers_cwd(tmp_path, monkeypatch):
    caller_cwd = tmp_path / "caller"
    caller_cwd.mkdir()
    monkeypatch.chdir(caller_cwd)

    assert pipeline.main([
        "--skip-market-history", "--skip-m6", "--skip-m8",
        "--lock-file", str(tmp_path / "pipeline.lock"),
    ]) == 0
    assert Path.cwd() == caller_cwd


def test_market_history_runtime_imports_resolve(monkeypatch):
    class FakeSession:
        def __enter__(self):
            return "database-session"

        def __exit__(self, *args):
            return None

    fake_db = types.ModuleType("app.db")
    fake_db.SessionLocal = FakeSession
    fake_market_history = types.ModuleType("app.services.market_history")
    fake_market_history.create_provider = lambda: "provider"
    fake_market_history.ingest_market_history = (
        lambda db, provider, **kwargs: {"db": db, "provider": provider, **kwargs}
    )
    monkeypatch.setitem(sys.modules, "app.db", fake_db)
    monkeypatch.setitem(sys.modules, "app.services.market_history", fake_market_history)

    result = pipeline.run_market_history()
    assert result["db"] == "database-session"
    assert result["provider"] == "provider"
    assert result["sleep_seconds"] == 12.0
    assert {key: result[key] for key in pipeline.COHORT} == pipeline.COHORT


def test_skips_are_explicit_and_dry_run_reaches_only_m8():
    calls = []
    report = pipeline.run_pipeline(
        dry_run=True, skip_market_history=True, skip_m6=True,
        market_stage=lambda: calls.append("market"), m6_stage=lambda: calls.append("m6"),
        m8_stage=lambda **kwargs: calls.append(kwargs) or {},
    )
    assert calls == [{"dry_run": True}]
    assert report["stages"]["market_history"] == {"status": "skipped"}
    assert report["stages"]["m6_analysis"] == {"status": "skipped"}
