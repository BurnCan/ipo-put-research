from datetime import date
from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from app.db import get_db
from app.models import Company, Filing, FilingDocument, IPO, IPOFact, IPOLockup
from app.services.ipo_ingest import ingest_registration_filings

router = APIRouter(prefix="/api")


@router.get("/health")
def health():
    return {"ok": True}


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


@router.post("/ingest/sec")
def ingest_sec(days: int = Query(365, ge=1, le=3650), db: Session = Depends(get_db)):
    return ingest_registration_filings(db, days=days)
