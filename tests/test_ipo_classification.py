from datetime import date, datetime

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import Company, Filing, IPO, utc_now
from app.schemas import IPORead
from app.services.ipo_classification import (
    classify_ipo_candidate,
    classify_ipo_candidates,
    determine_offering_status,
    find_final_prospectus,
)


def filing(form, day, n):
    return Filing(form_type=form, filed_at=day, accession_number=f"0000000001-26-{n:06d}",
                  filing_path=f"path/{n}", sec_url=f"https://www.sec.gov/{n}")


def candidate(name="Example Robotics, Inc."):
    company = Company(cik="0000000001", name=name)
    ipo = IPO(company=company, first_filing_date=date(2026, 1, 1))
    registration = filing("S-1", date(2026, 1, 1), 1)
    company.filings.append(registration)
    return ipo, company.filings


def test_operating_company_and_final_prospectus_and_reason():
    ipo, filings = candidate()
    prospectus = filing("424B4", date(2026, 2, 10), 2)
    filings.extend([filing("EFFECT", date(2026, 2, 8), 3), prospectus])
    result = classify_ipo_candidate(ipo, filings)
    assert (result.candidate_type, result.classification_status, result.offering_status) == (
        "operating_company_ipo", "classified", "priced")
    assert result.final_prospectus is prospectus
    assert result.reason


def test_spac_requires_name_and_sequence_evidence():
    ipo, filings = candidate("North Star Acquisition Corp")
    filings.append(filing("EFFECT", date(2026, 2, 1), 2))
    assert classify_ipo_candidate(ipo, filings).candidate_type == "spac"


def test_weak_evidence_is_unknown_needs_review():
    ipo, filings = candidate()
    result = classify_ipo_candidate(ipo, filings)
    assert (result.candidate_type, result.classification_status, result.offering_status) == (
        "unknown", "needs_review", "filed")


def test_prior_public_history_stays_conservative():
    ipo, filings = candidate()
    filings.extend([filing("10-K", date(2025, 3, 1), 2), filing("10-Q", date(2025, 8, 1), 3),
                    filing("424B4", date(2026, 2, 1), 4)])
    result = classify_ipo_candidate(ipo, filings)
    assert (result.candidate_type, result.classification_status) == ("unknown", "needs_review")
    assert "predates registration" in result.reason


def test_effect_and_rw_statuses():
    ipo, filings = candidate()
    filings.append(filing("EFFECT", date(2026, 1, 20), 2))
    assert determine_offering_status(ipo, filings, None) == "effective"
    filings.append(filing("RW", date(2026, 1, 25), 3))
    assert determine_offering_status(ipo, filings, None) == "withdrawn"


def test_prospectus_must_be_nearby_and_close_multiple_are_ambiguous():
    ipo, filings = candidate()
    filings.append(filing("424B4", date(2027, 1, 1), 2))
    assert find_final_prospectus(ipo, filings) == (None, False)
    filings.extend([filing("424B4", date(2026, 2, 1), 3), filing("424B4", date(2026, 2, 3), 4)])
    assert find_final_prospectus(ipo, filings) == (None, True)
    result = classify_ipo_candidate(ipo, filings)
    assert (result.classification_status, result.offering_status) == ("needs_review", "unknown")


def test_service_rerun_is_idempotent_and_schema_serializes_fields():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        ipo, filings = candidate()
        filings.append(filing("424B4", date(2026, 2, 1), 2))
        db.add(ipo)
        db.commit()
        first = classify_ipo_candidates(db)
        second = classify_ipo_candidates(db)
        stored = db.scalar(select(IPO))
        assert first["prospectuses_linked"] == second["prospectuses_linked"] == 1
        assert second["unchanged"] == 1
        payload = IPORead(id=stored.id, cik=stored.company.cik, company_name=stored.company.name,
                          status=stored.status, candidate_type=stored.candidate_type,
                          classification_status=stored.classification_status,
                          offering_status=stored.offering_status,
                          classification_reason=stored.classification_reason,
                          final_prospectus={"url": stored.final_prospectus.sec_url})
        assert payload.model_dump()["final_prospectus"]["url"].endswith("/2")


def test_timestamp_default_is_utc_aware_before_database_conversion():
    value = utc_now()
    assert isinstance(value, datetime) and value.tzinfo is not None
