from datetime import date
from decimal import Decimal

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import Company, Filing, IPO, IPOFact
from app.services.prospectus_facts import promote_canonical_facts, store_facts
from app.services.prospectus_parser import ParsedFact


def _database():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return engine


def _ipo_with_prospectus(db: Session) -> tuple[IPO, Filing]:
    company = Company(cik="0000000001", name="Example, Inc.")
    filing = Filing(company=company, form_type="424B4", filed_at=date(2026, 2, 1),
                    accession_number="0000000001-26-000001", filing_path="example/filing",
                    sec_url="https://www.sec.gov/example")
    ipo = IPO(company=company, first_filing_date=date(2026, 1, 1))
    db.add_all([ipo, filing]); db.flush()
    ipo.final_prospectus_filing_id = filing.id
    return ipo, filing


def _fact(field: str, value: str, excerpt: str, locator: str = "Cover") -> ParsedFact:
    unit = "USD/share" if field == "ipo_price" else "shares"
    return ParsedFact(field, Decimal(value), unit, Decimal("0.98"), excerpt, locator)


def test_same_value_with_distinct_provenance_is_retained_and_exact_rerun_is_idempotent():
    with Session(_database()) as db:
        ipo, filing = _ipo_with_prospectus(db)
        facts = [_fact("ipo_price", "12", "Offering price is $12", "Cover"),
                 _fact("ipo_price", "12", "Price to public: $12", "Underwriting table")]

        assert store_facts(db, ipo, filing.id, facts) == 2
        assert store_facts(db, ipo, filing.id, facts) == 0
        assert db.scalar(select(func.count()).select_from(IPOFact)) == 2


def test_conflicting_evidence_clears_previously_promoted_canonical_value():
    with Session(_database()) as db:
        ipo, filing = _ipo_with_prospectus(db)
        store_facts(db, ipo, filing.id, [_fact("ipo_price", "12", "Offering price is $12")])
        promote_canonical_facts(db, ipo)
        assert ipo.ipo_price == Decimal("12")

        store_facts(db, ipo, filing.id, [_fact("ipo_price", "13", "Offering price is $13", "Pricing table")])
        result = promote_canonical_facts(db, ipo)

        assert ipo.ipo_price is None
        assert result["ambiguities"] == 1


def test_deal_size_is_cleared_when_a_canonical_input_becomes_ambiguous():
    with Session(_database()) as db:
        ipo, filing = _ipo_with_prospectus(db)
        store_facts(db, ipo, filing.id, [
            _fact("ipo_price", "10", "Offering price is $10"),
            _fact("shares_offered", "1000000", "Offering of 1,000,000 shares"),
        ])
        promote_canonical_facts(db, ipo)
        assert ipo.deal_size == Decimal("10000000")

        store_facts(db, ipo, filing.id, [
            _fact("shares_offered", "1100000", "Offering of 1,100,000 shares", "Pricing table")
        ])
        promote_canonical_facts(db, ipo)

        assert ipo.shares_offered is None
        assert ipo.deal_size is None


def test_v1_facts_coexist_but_v2_semantics_drive_reparse_promotion():
    with Session(_database()) as db:
        ipo, filing = _ipo_with_prospectus(db)
        db.add(IPOFact(
            ipo_id=ipo.id, filing_id=filing.id, field_name="shares_offered",
            value_numeric=Decimal("24390243"), value_key="old-v1", unit="shares",
            confidence=Decimal("0.94"), parser_name="final_prospectus_offering",
            parser_version="1", source_excerpt="We are offering 24,390,243 shares",
            source_locator="Prospectus cover", is_derived=True,
            derivation="primary_shares (no selling shares stated)"))
        store_facts(db, ipo, filing.id, [
            _fact("ipo_price", "22", "IPO price is $22"),
            _fact("primary_shares", "24390243", "We are offering 24,390,243 shares"),
            _fact("secondary_shares", "13170731", "Stockholder is offering 13,170,731 shares"),
            ParsedFact("shares_offered", Decimal("37560974"), "shares", Decimal("0.96"),
                       "Derived from issuer and stockholder shares", "Prospectus cover", True,
                       "primary_shares + secondary_shares"),
        ])

        result = promote_canonical_facts(db, ipo)

        assert ipo.ipo_price == Decimal("22")
        assert ipo.primary_shares == Decimal("24390243")
        assert ipo.secondary_shares == Decimal("13170731")
        assert ipo.shares_offered == Decimal("37560974")
        assert ipo.deal_size == Decimal("826341428")
        assert result["ambiguities"] == 0
        assert db.scalar(select(func.count()).select_from(IPOFact).where(
            IPOFact.parser_version == "1")) == 1
