from decimal import Decimal
from pathlib import Path

import pytest

from app.services.prospectus_parser import PARSER_VERSION, extract_ipo_facts


def _values(facts, field):
    return [fact.value for fact in facts if fact.field_name == field]


def test_alliance_cover_extracts_price_components_and_base_total_only():
    text = Path("tests/fixtures/alliance_final_prospectus.txt").read_text()
    facts = extract_ipo_facts(text)

    assert _values(facts, "ipo_price") == [Decimal("22.00")]
    assert _values(facts, "primary_shares") == [Decimal("24390243")]
    assert _values(facts, "secondary_shares") == [Decimal("13170731")]
    assert _values(facts, "shares_offered") == [Decimal("37560974")]
    total = next(f for f in facts if f.field_name == "shares_offered")
    assert total.is_derived
    assert total.derivation == "primary_shares + secondary_shares"
    assert Decimal("43195120") not in _values(facts, "shares_offered")


@pytest.mark.parametrize("text, expected", [
    ("Initial public offering price per share is $18.50", "18.50"),
    ("Initial public offering price of $19.25 per share", "19.25"),
    ("Initial public offering price: $20.00 per share", "20.00"),
    ("Public offering price: $21.00 per share", "21.00"),
    ("Price to public:\n  $22.00 per share", "22.00"),
])
def test_explicit_final_price_variants(text, expected):
    assert _values(extract_ipo_facts(text), "ipo_price") == [Decimal(expected)]


def test_unrelated_per_share_values_are_ignored():
    text = """Weighted-average option exercise price was $4.25 per share.
    Dilution to new investors is $12.00 per share. Stock-plan value is $8.00 per share."""
    assert _values(extract_ipo_facts(text), "ipo_price") == []


def test_agreeing_direct_and_derived_totals_have_one_numeric_meaning():
    text = """We are offering 60 shares and the selling stockholders are offering 40 shares.
    This is an offering of 100 shares."""
    facts = extract_ipo_facts(text)
    assert _values(facts, "shares_offered") == [Decimal("100"), Decimal("100")]


def test_conflicting_explicit_prices_are_retained_for_ambiguity_handling():
    facts = extract_ipo_facts("Public offering price: $20 per share. Price to public: $21 per share.")
    assert set(_values(facts, "ipo_price")) == {Decimal("20"), Decimal("21")}


def test_parser_version_reflects_corrected_offering_semantics():
    assert PARSER_VERSION == "2"
