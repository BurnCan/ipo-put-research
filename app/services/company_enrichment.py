from __future__ import annotations

import logging
import time
from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Company, Filing
from app.services.sec_edgar import fetch_company_submissions, parse_company_submissions

logger = logging.getLogger(__name__)


def enrich_companies_from_sec(
    db: Session,
    *,
    limit: int | None = None,
    sleep: float = 0.12,
    fetcher: Callable[[str], dict] = fetch_company_submissions,
) -> dict[str, int]:
    """Enrich existing companies independently, so one bad issuer cannot stop a batch."""
    statement = select(Company).order_by(Company.id)
    if limit is not None:
        statement = statement.limit(limit)
    companies = list(db.scalars(statement))
    counts = {"companies_seen": 0, "companies_updated": 0, "filings_seen": 0, "filings_created": 0, "errors": 0}

    for company in companies:
        counts["companies_seen"] += 1
        try:
            submission = parse_company_submissions(fetcher(company.cik), company.cik)
            changed = False
            for attribute, value in (("name", submission.company_name), ("ticker", submission.ticker), ("exchange", submission.exchange)):
                if value is not None and getattr(company, attribute) != value:
                    setattr(company, attribute, value)
                    changed = True
            counts["companies_updated"] += int(changed)
            counts["filings_seen"] += len(submission.filings)

            accessions = [item.accession_number for item in submission.filings]
            existing = set(db.scalars(select(Filing.accession_number).where(Filing.accession_number.in_(accessions)))) if accessions else set()
            for item in submission.filings:
                if item.accession_number in existing:
                    continue
                db.add(Filing(company_id=company.id, form_type=item.form_type, filed_at=item.filed_at,
                              accession_number=item.accession_number, filing_path=item.filing_path,
                              sec_url=item.sec_url, source="sec_submissions"))
                existing.add(item.accession_number)
                counts["filings_created"] += 1
            db.commit()
        except Exception:
            db.rollback()
            counts["errors"] += 1
            logger.exception("SEC submissions enrichment failed for CIK %s", company.cik)
        if sleep > 0:
            time.sleep(sleep)
    return counts
