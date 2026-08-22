from datetime import date
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import Company, Filing, IPO
from app.services.prospectus_processing import process_final_prospectuses


def _database():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return engine


def _add_ipo(db, number, *, classification_status, candidate_type, offering_status):
    company = Company(cik=f"{number:010d}", name=f"Issuer {number}")
    filing = Filing(company=company, form_type="424B4", filed_at=date(2026, 1, number),
                    accession_number=f"0000000000-26-{number:06d}", filing_path=f"filing-{number}",
                    sec_url=f"https://www.sec.gov/{number}")
    ipo = IPO(company=company, first_filing_date=date(2026, 1, number),
              classification_status=classification_status, candidate_type=candidate_type,
              offering_status=offering_status)
    db.add_all([filing, ipo])
    db.flush()
    ipo.final_prospectus_filing_id = filing.id
    return ipo


def test_research_universe_filters_are_composable_and_precede_limit(monkeypatch, tmp_path):
    text_path = tmp_path / "cached.txt"
    text_path.write_text("No extractable offering facts.")

    def cached_document(_db, filing, **_kwargs):
        return SimpleNamespace(fetch_status="success", filing_id=filing.id,
                               text_path=str(text_path)), False

    monkeypatch.setattr("app.services.prospectus_processing.fetch_filing_document", cached_document)
    monkeypatch.setattr("app.services.prospectus_processing.normalize_filing_document",
                        lambda *_args, **_kwargs: False)

    with Session(_database()) as db:
        _add_ipo(db, 1, classification_status="needs_review",
                 candidate_type="unknown", offering_status="filed")
        _add_ipo(db, 2, classification_status="classified",
                 candidate_type="operating_company_ipo", offering_status="effective")
        selected = _add_ipo(db, 3, classification_status="classified",
                            candidate_type="operating_company_ipo", offering_status="priced")
        db.commit()

        result = process_final_prospectuses(
            db, classification_status="classified", candidate_type="operating_company_ipo",
            offering_status="priced", limit=1)

        assert result["ipos_seen"] == 1
        assert result["documents_cached"] == 1
        assert selected.id == 3  # the match occurs after two filtered-out rows
