from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.main import app
from app.models import Company, Filing, FilingDocument, IPO, IPOLockup
from app.services.lockup_parser import ParsedLockup
from app.services.lockup_processing import process_cached_lockups, select_primary_lockup, store_lockups
from app.services.schema_upgrade import upgrade_milestone_4


def _database():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return engine


def _ipo_and_filing(db: Session) -> tuple[IPO, Filing]:
    company = Company(cik="0000000001", name="Example, Inc.")
    filing = Filing(company=company, form_type="424B4", filed_at=date(2026, 2, 1),
                    accession_number="0000000001-26-000001", filing_path="example/filing",
                    sec_url="https://www.sec.gov/example")
    ipo = IPO(company=company, first_filing_date=date(2026, 1, 1))
    db.add_all([ipo, filing])
    db.flush()
    return ipo, filing


def _lockup(group: str, expiration: date, *, duration: int = 180,
            excerpt: str | None = None, lockup_type: str = "underwriter_lockup") -> ParsedLockup:
    return ParsedLockup(
        group, group.replace("_", " "), lockup_type, duration, None, expiration,
        None, None, False, None, Decimal("0.92"), excerpt or f"{group} evidence",
        "Underwriting, line 10",
    )


def _cached_ipo(db: Session, number: int, text_path: str, *, classification_status: str,
                candidate_type: str, offering_status: str) -> IPO:
    company = Company(cik=f"{number:010d}", name=f"Cached issuer {number}")
    filing = Filing(company=company, form_type="424B4", filed_at=date(2026, 1, number),
                    accession_number=f"0000000000-26-{number:06d}", filing_path=f"cached-{number}",
                    sec_url=f"https://www.sec.gov/{number}")
    ipo = IPO(company=company, first_filing_date=date(2026, 1, number),
              classification_status=classification_status, candidate_type=candidate_type,
              offering_status=offering_status)
    db.add_all([filing, ipo])
    db.flush()
    ipo.final_prospectus_filing_id = filing.id
    db.add(FilingDocument(filing=filing, source_url=filing.sec_url, fetch_status="success",
                          text_path=text_path))
    return ipo


def _run_filtered(tmp_path, ipos, **filters):
    cached_text = tmp_path / "cached.txt"
    cached_text.write_text("No lockup agreements in this cached document.")
    with Session(_database()) as db:
        for number, attributes in enumerate(ipos, start=1):
            _cached_ipo(db, number, str(cached_text), **attributes)
        db.commit()
        return process_cached_lockups(db, **filters)


def test_classification_status_filters_cached_lockups(tmp_path):
    result = _run_filtered(tmp_path, [
        dict(classification_status="needs_review", candidate_type="unknown", offering_status="filed"),
        dict(classification_status="classified", candidate_type="unknown", offering_status="filed"),
    ], classification_status="classified")
    assert result["ipos_seen"] == 1


def test_candidate_type_filters_cached_lockups(tmp_path):
    result = _run_filtered(tmp_path, [
        dict(classification_status="classified", candidate_type="spac", offering_status="priced"),
        dict(classification_status="classified", candidate_type="operating_company_ipo", offering_status="priced"),
    ], candidate_type="operating_company_ipo")
    assert result["ipos_seen"] == 1


def test_offering_status_filters_cached_lockups(tmp_path):
    result = _run_filtered(tmp_path, [
        dict(classification_status="classified", candidate_type="operating_company_ipo", offering_status="withdrawn"),
        dict(classification_status="classified", candidate_type="operating_company_ipo", offering_status="priced"),
    ], offering_status="priced")
    assert result["ipos_seen"] == 1


def test_cached_lockup_filters_compose(tmp_path):
    result = _run_filtered(tmp_path, [
        dict(classification_status="needs_review", candidate_type="operating_company_ipo", offering_status="priced"),
        dict(classification_status="classified", candidate_type="spac", offering_status="priced"),
        dict(classification_status="classified", candidate_type="operating_company_ipo", offering_status="filed"),
        dict(classification_status="classified", candidate_type="operating_company_ipo", offering_status="priced"),
    ], classification_status="classified", candidate_type="operating_company_ipo",
       offering_status="priced")
    assert result["ipos_seen"] == 1


def test_cached_lockup_filters_are_applied_before_limit(tmp_path):
    result = _run_filtered(tmp_path, [
        dict(classification_status="needs_review", candidate_type="unknown", offering_status="filed"),
        dict(classification_status="classified", candidate_type="operating_company_ipo", offering_status="priced"),
    ], classification_status="classified", candidate_type="operating_company_ipo",
       offering_status="priced", limit=1)
    assert result["ipos_seen"] == 1
    assert result["documents_available"] == 1


def test_cached_lockups_without_filters_still_process_all_linked_ipos(tmp_path):
    result = _run_filtered(tmp_path, [
        dict(classification_status="needs_review", candidate_type="unknown", offering_status="filed"),
        dict(classification_status="classified", candidate_type="operating_company_ipo", offering_status="priced"),
    ])
    assert result["ipos_seen"] == 2
    assert result["documents_available"] == 2


def test_existing_stockholders_take_priority_over_directors_officers():
    with Session(_database()) as db:
        ipo, filing = _ipo_and_filing(db)
        store_lockups(db, ipo, filing.id, [
            _lockup("existing_stockholders", date(2026, 7, 31)),
            _lockup("directors_officers", date(2026, 5, 2), duration=90),
        ])

        assert select_primary_lockup(db, ipo) == {"selected": 1, "ambiguity": 0, "cleared": 0}
        assert db.get(IPOLockup, ipo.primary_lockup_id).holder_group == "existing_stockholders"
        assert ipo.primary_lockup_expiration_date == date(2026, 7, 31)


def test_conflicting_existing_stockholder_dates_are_ambiguous():
    with Session(_database()) as db:
        ipo, filing = _ipo_and_filing(db)
        store_lockups(db, ipo, filing.id, [
            _lockup("existing_stockholders", date(2026, 7, 31), excerpt="first"),
            _lockup("existing_stockholders", date(2026, 8, 15), excerpt="second"),
        ])

        assert select_primary_lockup(db, ipo)["ambiguity"] == 1
        assert ipo.primary_lockup_id is None
        assert ipo.primary_lockup_expiration_date is None


def test_new_same_tier_conflict_clears_a_previously_selected_lockup():
    with Session(_database()) as db:
        ipo, filing = _ipo_and_filing(db)
        store_lockups(db, ipo, filing.id, [_lockup("existing_stockholders", date(2026, 7, 31))])
        select_primary_lockup(db, ipo)
        assert ipo.primary_lockup_id is not None

        store_lockups(db, ipo, filing.id, [
            _lockup("existing_stockholders", date(2026, 8, 15), excerpt="new conflict")
        ])
        result = select_primary_lockup(db, ipo)

        assert result == {"selected": 0, "ambiguity": 1, "cleared": 1}
        assert ipo.primary_lockup_id is None
        assert ipo.primary_lockup_expiration_date is None


def test_company_only_lockup_is_not_primary():
    with Session(_database()) as db:
        ipo, filing = _ipo_and_filing(db)
        store_lockups(db, ipo, filing.id, [
            _lockup("company", date(2026, 5, 2), duration=90, lockup_type="company_lockup")
        ])
        assert select_primary_lockup(db, ipo)["selected"] == 0
        assert ipo.primary_lockup_id is None


def test_exact_rerun_is_idempotent_but_distinct_group_provenance_is_retained():
    with Session(_database()) as db:
        ipo, filing = _ipo_and_filing(db)
        evidence = [
            _lockup("existing_stockholders", date(2026, 7, 31)),
            _lockup("directors_officers", date(2026, 7, 31)),
        ]
        assert store_lockups(db, ipo, filing.id, evidence) == 2
        assert store_lockups(db, ipo, filing.id, evidence) == 0
        assert db.scalar(select(func.count()).select_from(IPOLockup)) == 2


def test_milestone_4_schema_upgrade_is_idempotent():
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE companies (id INTEGER PRIMARY KEY)"))
        connection.execute(text("CREATE TABLE filings (id INTEGER PRIMARY KEY)"))
        connection.execute(text("CREATE TABLE ipos (id INTEGER PRIMARY KEY)"))

    assert set(upgrade_milestone_4(engine)) == {
        "ipo_lockups", "primary_lockup_id", "primary_lockup_expiration_date"
    }
    assert upgrade_milestone_4(engine) == []


def test_api_serializes_canonical_and_agreement_lockup_fields():
    engine = _database()
    with Session(engine) as db:
        ipo, filing = _ipo_and_filing(db)
        store_lockups(db, ipo, filing.id, [_lockup("existing_stockholders", date(2026, 7, 31))])
        select_primary_lockup(db, ipo)
        ipo_id = ipo.id
        db.commit()

    def override_db():
        with Session(engine) as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    try:
        response = TestClient(app).get(f"/api/ipos/{ipo_id}")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["ipo"]["primary_lockup_id"] == payload["lockups"][0]["id"]
    assert payload["ipo"]["primary_lockup_expiration_date"] == "2026-07-31"
    assert payload["lockups"][0]["holder_group"] == "existing_stockholders"
    assert payload["lockups"][0]["calculated_expiration_date"] == "2026-07-31"
