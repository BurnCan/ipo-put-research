"""Conservative, metadata-only IPO candidate classification.

The registration date anchors a 180-day evidence window. A final prospectus is
only selected when the nearest post-registration 424B4 is not accompanied by
another 424B4 within three days; that close grouping is treated as ambiguous.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import Company, Filing, IPO

REGISTRATION_FORMS = {"S-1", "S-1/A", "F-1", "F-1/A"}
PERIODIC_FORMS = {"10-K", "10-Q", "20-F", "6-K"}
SPAC_TERMS = ("acquisition corp", "acquisition company", "capital corp", "blank check")
FUND_TERMS = (" fund", " etf", "portfolio", "investment trust")
MAX_SEQUENCE_DAYS = 180
AMBIGUITY_DAYS = 3
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Classification:
    candidate_type: str
    classification_status: str
    offering_status: str
    reason: str
    final_prospectus: Filing | None


def _anchor(ipo: IPO, filings: list[Filing]) -> date | None:
    registrations = [f.filed_at for f in filings if f.form_type.upper() in REGISTRATION_FORMS]
    return ipo.first_filing_date or (min(registrations) if registrations else None)


def _relevant(filings: list[Filing], anchor: date | None) -> list[Filing]:
    if anchor is None:
        return []
    end = anchor + timedelta(days=MAX_SEQUENCE_DAYS)
    return [f for f in filings if anchor <= f.filed_at <= end]


def find_final_prospectus(ipo: IPO, filings: list[Filing]) -> tuple[Filing | None, bool]:
    """Return the nearest plausible 424B4 and whether close alternatives make it ambiguous."""
    anchor = _anchor(ipo, filings)
    candidates = sorted(
        (f for f in _relevant(filings, anchor) if f.form_type.upper() == "424B4"),
        key=lambda f: (f.filed_at, f.accession_number),
    )
    if not candidates:
        return None, False
    if len(candidates) > 1 and (candidates[1].filed_at - candidates[0].filed_at).days <= AMBIGUITY_DAYS:
        return None, True
    return candidates[0], False


def determine_offering_status(ipo: IPO, filings: list[Filing], prospectus: Filing | None, ambiguous: bool = False) -> str:
    evidence = _relevant(filings, _anchor(ipo, filings))
    forms = {f.form_type.upper() for f in evidence}
    if ambiguous:
        return "unknown"
    if prospectus is not None:
        return "priced"  # a later RW may belong to another registration sequence
    if "RW" in forms:
        return "withdrawn"
    if "EFFECT" in forms:
        return "effective"
    return "filed" if forms & REGISTRATION_FORMS else "unknown"


def classify_ipo_candidate(ipo: IPO, filings: list[Filing]) -> Classification:
    anchor = _anchor(ipo, filings)
    prospectus, ambiguous = find_final_prospectus(ipo, filings)
    offering = determine_offering_status(ipo, filings, prospectus, ambiguous)
    if anchor is None:
        return Classification("unknown", "needs_review", "unknown", "Needs review: no S-1/F-1 registration anchor is available.", None)

    prior_periodic = [f for f in filings if f.filed_at < anchor and f.form_type.upper() in PERIODIC_FORMS]
    if prior_periodic:
        forms = ", ".join(sorted({f.form_type.upper() for f in prior_periodic}))
        return Classification("unknown", "needs_review", offering, f"Needs review: periodic filing history ({forms}) predates registration, suggesting an already-public issuer.", prospectus)
    if ambiguous:
        return Classification("unknown", "needs_review", offering, "Needs review: multiple closely dated 424B4 filings make final-prospectus association ambiguous.", None)

    name = ipo.company.name.lower()
    sequence_evidence = prospectus is not None or any(
        f.form_type.upper() in {"EFFECT", "8-A12B", "8-A12G"} for f in _relevant(filings, anchor)
    )
    if any(term in name for term in SPAC_TERMS) and sequence_evidence:
        return Classification("spac", "classified", offering, "Likely SPAC: issuer name matches a blank-check/acquisition pattern and registration has subsequent offering evidence.", prospectus)
    if any(term in name for term in FUND_TERMS) and sequence_evidence:
        return Classification("fund", "classified", offering, "Likely fund: issuer name matches a fund/trust pattern and registration has subsequent offering evidence.", prospectus)
    if prospectus is not None and sequence_evidence:
        return Classification("operating_company_ipo", "classified", offering, "Likely operating-company IPO: registration is followed by a nearby 424B4 with no prior periodic filing history.", prospectus)
    return Classification("unknown", "needs_review", offering, "Needs review: registration metadata lacks enough independent evidence for a confident candidate type.", prospectus)


def classify_ipo_candidates(db: Session, *, limit: int | None = None, company_id: int | None = None, ipo_id: int | None = None) -> dict[str, int]:
    stmt = select(IPO).options(selectinload(IPO.company).selectinload(Company.filings)).order_by(IPO.id)
    if company_id is not None:
        stmt = stmt.where(IPO.company_id == company_id)
    if ipo_id is not None:
        stmt = stmt.where(IPO.id == ipo_id)
    if limit is not None:
        stmt = stmt.limit(limit)
    counts = {"ipos_seen": 0, "classified": 0, "needs_review": 0, "unchanged": 0, "prospectuses_linked": 0, "errors": 0}
    for ipo in db.scalars(stmt):
        counts["ipos_seen"] += 1
        try:
            result = classify_ipo_candidate(ipo, list(ipo.company.filings))
            new_values = (result.candidate_type, result.classification_status, result.offering_status, result.reason, result.final_prospectus.id if result.final_prospectus else None)
            old_values = (ipo.candidate_type, ipo.classification_status, ipo.offering_status, ipo.classification_reason, ipo.final_prospectus_filing_id)
            if new_values == old_values:
                counts["unchanged"] += 1
            else:
                ipo.candidate_type, ipo.classification_status, ipo.offering_status, ipo.classification_reason, ipo.final_prospectus_filing_id = new_values
            counts[result.classification_status] += 1
            counts["prospectuses_linked"] += int(result.final_prospectus is not None)
            db.commit()
        except Exception:
            db.rollback()
            counts["errors"] += 1
            logger.exception("Classification failed for IPO %s", ipo.id)
    return counts
