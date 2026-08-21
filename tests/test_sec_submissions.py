from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import Company, Filing
from app.services.company_enrichment import enrich_companies_from_sec
from app.services.sec_edgar import parse_company_submissions


def payload(name="Example Incorporated", tickers=None, exchanges=None):
    return {
        "name": name,
        "tickers": ["EXM"] if tickers is None else tickers,
        "exchanges": ["Nasdaq"] if exchanges is None else exchanges,
        "filings": {
            "recent": {
                "accessionNumber": [
                    "0000012345-26-000010",
                    "0000012345-26-000011",
                    "bad-accession",
                ],
                "filingDate": ["2026-08-10", "2026-08-11", "2026-08-12"],
                "form": ["424B4", "S-1", "8-K"],
                "primaryDocument": ["final-prospectus.htm", "registration.htm", "bad.htm"],
            }
        },
    }


def test_parse_submissions_metadata_filings_and_url():
    result = parse_company_submissions(payload(), "0000012345")
    assert (result.company_name, result.ticker, result.exchange) == (
        "Example Incorporated", "EXM", "Nasdaq"
    )
    assert len(result.filings) == 1  # irrelevant S-1 and malformed accession are ignored
    filing = result.filings[0]
    assert filing.accession_number == "0000012345-26-000010"
    assert filing.filing_path == "edgar/data/12345/000001234526000010/final-prospectus.htm"
    assert filing.sec_url == (
        "https://www.sec.gov/Archives/edgar/data/12345/"
        "000001234526000010/final-prospectus.htm"
    )


def test_missing_primary_document_uses_filing_index():
    data = payload()
    data["filings"]["recent"]["primaryDocument"][0] = ""
    filing = parse_company_submissions(data, "12345").filings[0]
    assert filing.sec_url.endswith("/0000012345-26-000010-index.html")


def test_enrichment_is_idempotent_and_allows_missing_security_metadata():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        company = Company(cik="0000012345", name="Old Name")
        no_ticker = Company(cik="0000099999", name="No Security")
        db.add_all([company, no_ticker])
        db.commit()

        responses = {
            company.cik: payload(),
            no_ticker.cik: payload("No Security Ltd", [], []),
        }
        first = enrich_companies_from_sec(db, sleep=0, fetcher=responses.__getitem__)
        second = enrich_companies_from_sec(db, sleep=0, fetcher=responses.__getitem__)

        assert first == {"companies_seen": 2, "companies_updated": 2, "filings_seen": 2, "filings_created": 1, "errors": 0}
        assert second["filings_created"] == 0
        assert db.scalar(select(Company).where(Company.cik == company.cik)).ticker == "EXM"
        missing = db.scalar(select(Company).where(Company.cik == no_ticker.cik))
        assert (missing.ticker, missing.exchange) == (None, None)
        assert len(list(db.scalars(select(Filing)))) == 1


def test_malformed_issuer_does_not_stop_batch(caplog):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add_all([
            Company(cik="0000000001", name="Broken"),
            Company(cik="0000012345", name="Working"),
        ])
        db.commit()

        def fetch(cik):
            return {"name": "Broken"} if cik == "0000000001" else payload()

        result = enrich_companies_from_sec(db, sleep=0, fetcher=fetch)
        assert result["errors"] == 1
        assert result["companies_seen"] == 2
        assert result["filings_created"] == 1
        assert "CIK 0000000001" in caplog.text
