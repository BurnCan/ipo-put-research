#!/usr/bin/env python3
"""Run the daily market -> M6 -> M8 research pipeline."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Any, Callable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

DEFAULT_LOCK_FILE = PROJECT_ROOT / "data" / "update_research_pipeline.lock"
FROZEN_HYPOTHESIS_ID = "m7_return20_vol20_minus5_post20"
COHORT = {
    "classification_status": "classified",
    "candidate_type": "operating_company_ipo",
    "offering_status": "priced",
    "primary_lockup_only": True,
}


class AlreadyRunningError(RuntimeError):
    """Raised when another process owns the pipeline lock."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _jsonable(result: Any) -> Any:
    return result.to_dict() if hasattr(result, "to_dict") else result


@contextmanager
def pipeline_lock(path: Path):
    """Hold a non-blocking advisory lock, releasing it on every exit path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise AlreadyRunningError(f"pipeline lock is already held: {path}") from exc
        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()}\n")
        handle.flush()
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def run_market_history() -> Any:
    from app.db import SessionLocal
    from app.services.market_history import create_provider, ingest_market_history

    with SessionLocal() as db:
        return ingest_market_history(db, create_provider(), sleep_seconds=12.0, **COHORT)


def run_m6_analysis() -> Any:
    from app.db import SessionLocal
    from app.services.event_analysis import recompute_lockup_analyses

    with SessionLocal() as db:
        return recompute_lockup_analyses(db, recompute=False, **COHORT)


def run_m8_prospective(*, dry_run: bool = False) -> Any:
    from app.db import SessionLocal
    from app.services.prospective import update_prospective_lockup_signals

    with SessionLocal() as db:
        return update_prospective_lockup_signals(
            db, hypothesis_id=FROZEN_HYPOTHESIS_ID, dry_run=dry_run, **COHORT
        )


def run_pipeline(
    *, dry_run: bool = False, skip_market_history: bool = False,
    skip_m6: bool = False, skip_m8: bool = False,
    market_stage: Callable[[], Any] = run_market_history,
    m6_stage: Callable[[], Any] = run_m6_analysis,
    m8_stage: Callable[..., Any] = run_m8_prospective,
) -> dict[str, Any]:
    """Execute enabled stages in dependency order and return one report."""
    report: dict[str, Any] = {"status": "ok", "started_at": _now(), "stages": {}}
    stages = (
        ("market_history", skip_market_history, market_stage, {}),
        ("m6_analysis", skip_m6, m6_stage, {}),
        ("m8_prospective", skip_m8, m8_stage, {"dry_run": dry_run}),
    )
    for name, skipped, operation, kwargs in stages:
        if skipped:
            report["stages"][name] = {"status": "skipped"}
            continue
        started_at, timer = _now(), monotonic()
        try:
            result = operation(**kwargs)
        except Exception as exc:  # The structured failure is the CLI's public error contract.
            report["stages"][name] = {
                "status": "failed", "started_at": started_at, "finished_at": _now(),
                "duration_seconds": round(monotonic() - timer, 6),
                "error": f"{type(exc).__name__}: {exc}",
            }
            report.update(status="failed", failed_stage=name,
                          error=f"{type(exc).__name__}: {exc}", finished_at=_now())
            return report
        report["stages"][name] = {
            "status": "ok", "started_at": started_at, "finished_at": _now(),
            "duration_seconds": round(monotonic() - timer, 6), "result": _jsonable(result),
        }
    report["finished_at"] = _now()
    return report


def _append_log(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(report, sort_keys=True) + "\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log-file", type=Path)
    parser.add_argument("--dry-run", action="store_true",
                        help="Run upstream refreshes normally, but roll back M8 changes")
    parser.add_argument("--skip-market-history", action="store_true")
    parser.add_argument("--skip-m6", action="store_true")
    parser.add_argument("--skip-m8", action="store_true")
    parser.add_argument("--lock-file", type=Path, default=DEFAULT_LOCK_FILE,
                        help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # Preserve existing relative .env/data behavior even when cron starts elsewhere.
    original_cwd = Path.cwd()
    os.chdir(PROJECT_ROOT)
    try:
        try:
            with pipeline_lock(args.lock_file.resolve()):
                report = run_pipeline(
                    dry_run=args.dry_run, skip_market_history=args.skip_market_history,
                    skip_m6=args.skip_m6, skip_m8=args.skip_m8,
                )
        except AlreadyRunningError as exc:
            timestamp = _now()
            report = {"status": "already_running", "started_at": timestamp,
                      "finished_at": timestamp, "stages": {}, "error": str(exc)}
        log_file = args.log_file.resolve() if args.log_file else None
    finally:
        os.chdir(original_cwd)
    output = json.dumps(report, indent=2, sort_keys=True)
    print(output)
    if log_file:
        _append_log(log_file, report)
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
