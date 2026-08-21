from __future__ import annotations

from datetime import date, timedelta
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.models import Company, Filing, IPO
from app.services.sec_edgar import fetch_registration_filings


def ingest_registration_filings(db: Session, days: int = 365) -> dict[str, int]:
    end = date.today()
    start = end - timedelta(days=days)
    filings = fetch_registration_filings(start, end)

    companies_created = 0
    filings_created = 0
    ipos_created = 0

    for item in filings:
        company = db.scalar(select(Company).where(Company.cik == item.cik))
        if company is None:
            company = Company(cik=item.cik, name=item.company_name)
            db.add(company)
            db.flush()
            companies_created += 1
        elif company.name != item.company_name and len(item.company_name) > 2:
            company.name = item.company_name

        existing_filing = db.scalar(select(Filing).where(Filing.accession_number == item.accession_number))
        if existing_filing is None:
            db.add(
                Filing(
                    company_id=company.id,
                    form_type=item.form_type,
                    filed_at=item.filed_at,
                    accession_number=item.accession_number,
                    filing_path=item.filing_path,
                    sec_url=item.sec_url,
                )
            )
            filings_created += 1

        ipo = db.scalar(select(IPO).where(IPO.company_id == company.id))
        if ipo is None:
            ipo = IPO(company_id=company.id, first_filing_date=item.filed_at, status="filed")
            db.add(ipo)
            db.flush()
            ipos_created += 1
        elif ipo.first_filing_date is None or item.filed_at < ipo.first_filing_date:
            ipo.first_filing_date = item.filed_at

    db.commit()
    return {
        "candidate_filings_seen": len(filings),
        "companies_created": companies_created,
        "filings_created": filings_created,
        "ipo_candidates_created": ipos_created,
    }
