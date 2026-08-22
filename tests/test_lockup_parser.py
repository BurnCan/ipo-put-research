from datetime import date
from pathlib import Path

from app.services.lockup_parser import extract_lockup_agreements, find_lockup_sections


FIXTURE = Path(__file__).parent / "fixtures" / "lockup_prospectus.txt"


def test_section_first_extraction_dates_shares_and_release_terms():
    text = FIXTURE.read_text()
    assert len(find_lockup_sections(text)) == 2
    rows = extract_lockup_agreements(text, date(2026, 2, 1))

    shareholder = next(row for row in rows if row.duration_days == 180)
    assert shareholder.holder_group == "existing_stockholders"
    assert shareholder.lockup_type == "underwriter_lockup"
    assert shareholder.calculated_expiration_date == date(2026, 7, 31)
    assert shareholder.shares_locked == 12_500_000
    assert shareholder.early_release_exists is True
    assert "waive" in shareholder.early_release_terms.lower()

    company = next(row for row in rows if row.holder_group == "company")
    assert company.duration_days == 90
    assert company.lockup_type == "company_lockup"

    explicit = next(row for row in rows if row.stated_expiration_date)
    assert explicit.stated_expiration_date == date(2027, 1, 15)
    assert explicit.calculated_expiration_date is None
    assert explicit.early_release_exists is True
    assert "blackout" in explicit.early_release_terms.lower()


def test_unrelated_day_count_outside_discovered_sections_is_ignored():
    rows = extract_lockup_agreements("BUSINESS\nThe trial lasts 180 days after the date of this prospectus.", date(2026, 1, 1))
    assert rows == []
