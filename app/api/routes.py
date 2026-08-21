from datetime import date
from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from app.db import get_db
from app.models import Company, Filing, IPO
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
    return [
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
            "locked_shares": float(ipo.locked_shares) if ipo.locked_shares is not None else None,
            "unlock_date": ipo.unlock_date,
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
            "unlock_date": ipo.unlock_date,
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
    }


@router.post("/ingest/sec")
def ingest_sec(days: int = Query(365, ge=1, le=3650), db: Session = Depends(get_db)):
    return ingest_registration_filings(db, days=days)
