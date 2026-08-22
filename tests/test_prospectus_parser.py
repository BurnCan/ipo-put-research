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
    assert PARSER_VERSION == "4"


def test_beta_security_description_table_and_dual_class_total():
    text = Path("tests/fixtures/beta_final_prospectus.txt").read_text()
    assert text.index("Total Class A and Class B") > 20000
    facts = extract_ipo_facts(text)
    assert set(_values(facts, "ipo_price")) == {Decimal("34.00")}
    assert _values(facts, "primary_shares") == [Decimal("29852941")]
    assert _values(facts, "shares_offered") == [Decimal("29852941")]
    assert _values(facts, "shares_outstanding_post_ipo") == [Decimal("223807603")]
    assert Decimal("215306119") not in _values(facts, "shares_outstanding_post_ipo")
    assert Decimal("228285544") not in _values(facts, "shares_outstanding_post_ipo")
    assert _values(facts, "secondary_shares") == []


def test_pxed_pure_secondary_explicit_zero_and_base_counts():
    text = Path("tests/fixtures/pxed_final_prospectus.txt").read_text()
    assert text.index("Common stock offered by us") > 20000
    facts = extract_ipo_facts(text)
    assert set(_values(facts, "ipo_price")) == {Decimal("32.00")}
    assert _values(facts, "primary_shares") == [Decimal("0")]
    assert _values(facts, "secondary_shares") == [Decimal("4250000")]
    assert _values(facts, "shares_offered") == [Decimal("4250000")]
    assert _values(facts, "shares_outstanding_post_ipo") == [Decimal("35596255")]
    assert Decimal("4887500") not in _values(facts, "shares_offered")


def test_explicit_zero_is_distinct_from_unknown_component():
    explicit = extract_ipo_facts("Common stock offered by us\nNone.")
    unknown = extract_ipo_facts("This prospectus does not state issuer share allocation here.")
    assert _values(explicit, "primary_shares") == [Decimal("0")]
    assert _values(unknown, "primary_shares") == []


def test_pricing_table_uses_first_amount_not_total_and_is_cover_bounded():
    facts = extract_ipo_facts("Public offering price\n$32.00\n$136,000,000")
    assert _values(facts, "ipo_price") == [Decimal("32.00")]
    assert Decimal("136000000") not in _values(facts, "ipo_price")
    late = "x" * 20001 + "\nPublic offering price\n$15.00\n$150,000,000"
    assert _values(extract_ipo_facts(late), "ipo_price") == []


def test_single_class_post_offering_and_unrelated_outstanding_are_distinguished():
    text = """Options to purchase 8,000,000 shares were outstanding.
    Common stock to be outstanding after this offering
    100,000,000 shares
    (or 101,000,000 if the underwriters exercise their option)."""
    assert _values(extract_ipo_facts(text), "shares_outstanding_post_ipo") == [Decimal("100000000")]


def test_late_generic_outstanding_language_is_not_a_summary_context():
    text = "Cover text.\n" + "x" * 21000 + "\nThere were 91,234,567 shares outstanding at year end."
    assert _values(extract_ipo_facts(text), "shares_outstanding_post_ipo") == []


def test_ordinary_secondary_wording_does_not_imply_primary_zero():
    facts = extract_ipo_facts("Selling stockholders are offering 4,250,000 shares.")
    assert _values(facts, "secondary_shares") == [Decimal("4250000")]
    assert _values(facts, "primary_shares") == []
