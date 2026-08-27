from datetime import date
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from app.db import get_db
from app.models import (Company, DailyPrice, Filing, FilingDocument, IPO, IPOFact, IPOLockup,
                        IPOMarketSummary, LockupEventAnalysis, LockupSignalSnapshot)
from app.services.ipo_ingest import ingest_registration_filings
from app.services.research_dashboard import (
    GROUPS, get_historical_reference, get_prospective_evaluation, get_shadow_evaluation,
    get_prospective_signal_rows, get_research_summary, get_upcoming_lockups,
    hypothesis_metadata,
)
from app.services.pipeline_runs import get_pipeline_status

router = APIRouter(prefix="/api")


def market_summary_dict(summary):
    if summary is None:
        return None
    fields = ("first_trade_date", "first_day_open", "first_day_close", "latest_trade_date", "latest_close",
              "post_ipo_high", "first_day_close_return_vs_ipo_price", "return_from_ipo_price",
              "drawdown_from_post_ipo_high", "as_of_date")
    return {name: (float(value) if value is not None and name not in {"first_trade_date", "latest_trade_date", "as_of_date"} else value)
            for name in fields for value in [getattr(summary, name)]}


def _analysis_dict(row):
    if row is None: return None
    fields = ("event_date", "event_trade_date", "event_status", "pre_20d_return", "pre_10d_return",
              "pre_5d_return", "pre_1d_return", "event_close_return", "post_1d_return", "post_5d_return",
              "post_10d_return", "post_20d_return", "post_40d_return", "event_volume_ratio",
              "max_post_event_session_available")
    return {field: (float(value) if value is not None and field not in
                    {"event_date", "event_trade_date", "event_status", "max_post_event_session_available"} else value)
            for field in fields for value in [getattr(row, field)]}


@router.get("/health")
def health():
    return {"ok": True}


@router.get("/research/hypothesis")
def research_hypothesis():
    return hypothesis_metadata()


@router.get("/research/summary")
def research_summary(db: Session = Depends(get_db)):
    return get_research_summary(db)


@router.get("/research/pipeline-status")
def research_pipeline_status(db: Session = Depends(get_db)):
    return get_pipeline_status(db)


@router.get("/research/upcoming-lockups")
def research_upcoming_lockups(db: Session = Depends(get_db)):
    return get_upcoming_lockups(db)


@router.get("/research/prospective-signals")
def research_prospective_signals(
    status: str | None = None,
    interaction_group: str | None = Query(None),
    ticker: str | None = None,
    evaluation_mode: str = Query("strict_prospective"),
    db: Session = Depends(get_db),
):
    if interaction_group is not None and interaction_group not in GROUPS:
        raise HTTPException(status_code=422, detail="unknown interaction group")
    if evaluation_mode not in ("strict_prospective", "shadow_prospective"):
        raise HTTPException(status_code=422, detail="unknown evaluation mode")
    return get_prospective_signal_rows(db, status=status, evaluation_mode=evaluation_mode,
                                       interaction_group=interaction_group, ticker=ticker)


@router.get("/research/prospective-evaluation")
def research_prospective_evaluation(db: Session = Depends(get_db)):
    return get_prospective_evaluation(db)


@router.get("/research/shadow-evaluation")
def research_shadow_evaluation(db: Session = Depends(get_db)):
    return get_shadow_evaluation(db)


@router.get("/research/historical-reference")
def research_historical_reference(db: Session = Depends(get_db)):
    return get_historical_reference(db)


@router.get("/ipos")
def list_ipos(
    q: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
):
    filing_count = func.count(Filing.id).label("filing_count")
    stmt = (
        select(IPO, Company, filing_count)
        .join(Company, Company.id == IPO.company_id)
        .outerjoin(Filing, Filing.company_id == Company.id)
        .group_by(IPO.id, Company.id)
        .order_by(IPO.first_filing_date.desc().nullslast(), Company.name.asc())
        .limit(limit)
    )
    if q:
        pattern = f"%{q.strip()}%"
        stmt = stmt.where((Company.name.ilike(pattern)) | (Company.ticker.ilike(pattern)))

    rows = db.execute(stmt).all()
    result = [
        {
            "id": ipo.id,
            "cik": company.cik,
            "company_name": company.name,
            "ticker": company.ticker,
            "exchange": company.exchange,
            "status": ipo.status,
            "first_filing_date": ipo.first_filing_date,
            "ipo_date": ipo.ipo_date,
            "ipo_price": float(ipo.ipo_price) if ipo.ipo_price is not None else None,
            "shares_offered": float(ipo.shares_offered) if ipo.shares_offered is not None else None,
            "primary_shares": float(ipo.primary_shares) if ipo.primary_shares is not None else None,
            "secondary_shares": float(ipo.secondary_shares) if ipo.secondary_shares is not None else None,
            "shares_outstanding_post_ipo": float(ipo.shares_outstanding_post_ipo) if ipo.shares_outstanding_post_ipo is not None else None,
            "deal_size": float(ipo.deal_size) if ipo.deal_size is not None else None,
            "locked_shares": float(ipo.locked_shares) if ipo.locked_shares is not None else None,
            "unlock_date": ipo.unlock_date,
            "primary_lockup_expiration_date": ipo.primary_lockup_expiration_date,
            "filing_count": count,
            "candidate_type": ipo.candidate_type,
            "classification_status": ipo.classification_status,
            "offering_status": ipo.offering_status,
            "classification_reason": ipo.classification_reason,
            "final_prospectus": ({"id": ipo.final_prospectus.id, "filed_at": ipo.final_prospectus.filed_at,
                                  "accession": ipo.final_prospectus.accession_number, "url": ipo.final_prospectus.sec_url}
                                 if ipo.final_prospectus else None),
        }
        for ipo, company, count in rows
    ]
    for item in result:
        document = db.scalar(select(FilingDocument).where(FilingDocument.filing_id == item["final_prospectus"]["id"])) if item["final_prospectus"] else None
        item["document_cached"] = bool(document and document.fetch_status == "success")
        item["document_sha256"] = document.sha256 if document else None
        item["fact_count"] = db.scalar(select(func.count(IPOFact.id)).where(IPOFact.ipo_id == item["id"])) or 0
        item["market_summary"] = market_summary_dict(db.scalar(select(IPOMarketSummary).where(IPOMarketSummary.ipo_id == item["id"])))
        item["primary_lockup_event"] = _analysis_dict(db.scalar(select(LockupEventAnalysis).where(
            LockupEventAnalysis.ipo_id == item["id"], LockupEventAnalysis.lockup_id == db.get(IPO, item["id"]).primary_lockup_id)))
    return result


@router.get("/ipos/{ipo_id}")
def ipo_detail(ipo_id: int, db: Session = Depends(get_db)):
    row = db.execute(
        select(IPO, Company).join(Company, Company.id == IPO.company_id).where(IPO.id == ipo_id)
    ).first()
    if not row:
        return {"error": "not found"}
    ipo, company = row
    filings = db.scalars(
        select(Filing).where(Filing.company_id == company.id).order_by(Filing.filed_at.desc())
    ).all()
    return {
        "ipo": {
            "id": ipo.id,
            "status": ipo.status,
            "first_filing_date": ipo.first_filing_date,
            "ipo_date": ipo.ipo_date,
            "ipo_price": float(ipo.ipo_price) if ipo.ipo_price is not None else None,
            "shares_offered": float(ipo.shares_offered) if ipo.shares_offered is not None else None,
            "primary_shares": float(ipo.primary_shares) if ipo.primary_shares is not None else None,
            "secondary_shares": float(ipo.secondary_shares) if ipo.secondary_shares is not None else None,
            "shares_outstanding_post_ipo": float(ipo.shares_outstanding_post_ipo) if ipo.shares_outstanding_post_ipo is not None else None,
            "deal_size": float(ipo.deal_size) if ipo.deal_size is not None else None,
            "unlock_date": ipo.unlock_date,
            "primary_lockup_id": ipo.primary_lockup_id,
            "primary_lockup_expiration_date": ipo.primary_lockup_expiration_date,
            "candidate_type": ipo.candidate_type,
            "classification_status": ipo.classification_status,
            "offering_status": ipo.offering_status,
            "classification_reason": ipo.classification_reason,
            "final_prospectus": ({"id": ipo.final_prospectus.id, "filed_at": ipo.final_prospectus.filed_at,
                                  "accession": ipo.final_prospectus.accession_number, "url": ipo.final_prospectus.sec_url}
                                 if ipo.final_prospectus else None),
        },
        "company": {
            "cik": company.cik,
            "name": company.name,
            "ticker": company.ticker,
            "exchange": company.exchange,
        },
        "market_summary": market_summary_dict(ipo.market_summary),
        "primary_lockup_event": _analysis_dict(db.scalar(select(LockupEventAnalysis).where(
            LockupEventAnalysis.ipo_id == ipo.id, LockupEventAnalysis.lockup_id == ipo.primary_lockup_id))),
        "filings": [
            {
                "form": f.form_type,
                "filed_at": f.filed_at,
                "accession": f.accession_number,
                "url": f.sec_url,
            }
            for f in filings
        ],
        "facts": [{
            "field_name": fact.field_name,
            "value": (float(fact.value_numeric) if fact.value_numeric is not None else fact.value_text or fact.value_date),
            "unit": fact.unit, "confidence": float(fact.confidence),
            "source_excerpt": fact.source_excerpt, "source_locator": fact.source_locator,
            "parser_name": fact.parser_name, "parser_version": fact.parser_version,
            "filing_url": fact.filing.sec_url,
        } for fact in db.scalars(select(IPOFact).where(IPOFact.ipo_id == ipo.id).order_by(IPOFact.created_at)).all()],
        "lockups": [{
            "id": lockup.id,
            "lockup_type": lockup.lockup_type,
            "holder_group": lockup.holder_group,
            "holder_group_text": lockup.holder_group_text,
            "duration_days": lockup.duration_days,
            "stated_expiration_date": lockup.stated_expiration_date,
            "calculated_expiration_date": lockup.calculated_expiration_date,
            "shares_locked": float(lockup.shares_locked) if lockup.shares_locked is not None else None,
            "percentage_locked": float(lockup.percentage_locked) if lockup.percentage_locked is not None else None,
            "percentage_is_derived": lockup.percentage_is_derived,
            "early_release_exists": lockup.early_release_exists,
            "early_release_terms": lockup.early_release_terms,
            "confidence": float(lockup.confidence),
            "source_excerpt": lockup.source_excerpt,
            "source_locator": lockup.source_locator,
            "filing_url": lockup.filing.sec_url,
        } for lockup in db.scalars(select(IPOLockup).where(IPOLockup.ipo_id == ipo.id).order_by(IPOLockup.created_at)).all()],
    }


@router.get("/ipos/{ipo_id}/lockup-snapshots")
def lockup_snapshots(ipo_id: int, db: Session = Depends(get_db)):
    ipo = db.get(IPO, ipo_id)
    if ipo is None:
        raise HTTPException(status_code=404, detail="IPO not found")
    rows = db.scalars(select(LockupSignalSnapshot).where(
        LockupSignalSnapshot.ipo_id == ipo_id,
        LockupSignalSnapshot.lockup_id == ipo.primary_lockup_id
    ).order_by(LockupSignalSnapshot.observation_offset)).all()
    fields = ("observation_offset", "observation_date", "data_cutoff_date", "trading_sessions_to_event",
              "close", "return_from_ipo_price", "return_5d", "return_10d", "return_20d", "return_40d",
              "drawdown_from_post_ipo_high", "position_in_post_ipo_range", "avg_volume_20d",
              "realized_vol_20d", "available_history_sessions")
    non_numeric = {"observation_offset", "observation_date", "data_cutoff_date", "trading_sessions_to_event",
                   "available_history_sessions"}
    return [{name: (float(value) if value is not None and name not in non_numeric else value)
             for name in fields for value in [getattr(row, name)]} for row in rows]


@router.get("/ipos/{ipo_id}/prices")
def ipo_prices(ipo_id: int, limit: int = Query(500, ge=1, le=5000), db: Session = Depends(get_db)):
    summary = db.scalar(select(IPOMarketSummary).where(IPOMarketSummary.ipo_id == ipo_id))
    if summary is None:
        if db.get(IPO, ipo_id) is None:
            raise HTTPException(status_code=404, detail="IPO not found")
        return []
    bars = db.scalars(select(DailyPrice).where(DailyPrice.security_id == summary.security_id)
                      .order_by(DailyPrice.trade_date.desc()).limit(limit)).all()
    return [{"date": bar.trade_date, "open": float(bar.open), "high": float(bar.high), "low": float(bar.low),
             "close": float(bar.close), "volume": bar.volume, "provider": bar.provider} for bar in reversed(bars)]


@router.post("/ingest/sec")
def ingest_sec(days: int = Query(365, ge=1, le=3650), db: Session = Depends(get_db)):
    return ingest_registration_filings(db, days=days)
