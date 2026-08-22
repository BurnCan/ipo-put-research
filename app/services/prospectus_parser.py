"""Small, conservative parser for final prospectus offering facts."""
from dataclasses import dataclass
from decimal import Decimal
import re

PARSER_NAME = "final_prospectus_offering"
PARSER_VERSION = "3"
CANONICAL_PROMOTION_CONFIDENCE = Decimal("0.90")


@dataclass(frozen=True)
class ParsedFact:
    field_name: str
    value: Decimal
    unit: str
    confidence: Decimal
    source_excerpt: str
    source_locator: str
    is_derived: bool = False
    derivation: str | None = None


def _number(value: str) -> Decimal:
    return Decimal(value.replace(",", ""))


def _fact(field, match, group, unit, confidence, locator="Prospectus cover", value=None):
    excerpt = re.sub(r"\s+", " ", match.group(0)).strip()[:500]
    parsed = _number(match.group(group)) if value is None else Decimal(value)
    return ParsedFact(field, parsed, unit, Decimal(confidence), excerpt, locator)


def _first_match(patterns: list[str], text: str):
    """Return the earliest match, using pattern order to break position ties."""
    matches = [(m.start(), index, m) for index, pattern in enumerate(patterns)
               if (m := re.search(pattern, text, re.I | re.M))]
    return min(matches, default=(None, None, None))[2]


def extract_ipo_facts(text: str) -> list[ParsedFact]:
    """Extract explicit base-offering language; optional share counts are ignored."""
    # Offering summaries can follow the literal cover, but this remains tightly
    # bounded so option-plan and historical-financing disclosures are excluded.
    cover = text[:20000]
    facts: list[ParsedFact] = []

    price_patterns = [
        r"initial\s+public\s+offering\s+price\s+per\s+share\s+(?:will\s+be|is)\s*\$\s*(\d+(?:\.\d+)?)",
        # Security description is bounded and may not cross sentence punctuation.
        r"(?:initial\s+)?public\s+offering\s+price\s+of\s+[^.!?\n]{1,120}?\s+is\s*\$\s*(\d+(?:\.\d+)?)\s+per\s+share",
        r"initial\s+public\s+offering\s+price\s+(?:of|:)\s*\$\s*(\d+(?:\.\d+)?)\s+per\s+share",
        r"(?:initial\s+public\s+)?offering\s+price\s+is\s*\$\s*(\d+(?:\.\d+)?)\s+per\s+share",
        r"public\s+offering\s+price\s*:\s*\$\s*(\d+(?:\.\d+)?)\s+per\s+share",
        r"price\s+to\s+public\s*:\s*\$\s*(\d+(?:\.\d+)?)\s+per\s+share",
    ]
    for pattern in price_patterns:
        facts.extend(_fact("ipo_price", match, 1, "USD/share", "0.99")
                     for match in re.finditer(pattern, cover, re.I))

    # Cover pricing tables put the per-share amount first. Requiring the next
    # normalized line and a plausible per-share magnitude prevents selecting the
    # total offering proceeds on the following line.
    table_pattern = (r"(?im)^\s*(?:initial\s+)?public\s+offering\s+price\s*$"
                     r"\s*^\s*\$\s*(\d{1,4}(?:\.\d{1,4})?)\s*$")
    facts.extend(_fact("ipo_price", match, 1, "USD/share", "0.99", "Prospectus cover pricing table")
                 for match in re.finditer(table_pattern, cover))

    primary_patterns = [
        r"(?:common stock|shares of common stock)\s+offered\s+by\s+(?:us|the company)\s*[:\-]?\s*(None\.?|[\d,]+\s+shares)",
        r"(?:we|the company)\s+(?:are|is)\s+offering\s+([\d,]+)\s+shares",
        r"([\d,]+)\s+shares\s+(?:are being )?offered\s+by\s+(?:us|the company)",
    ]
    secondary_patterns = [
        r"(?:common stock|shares of common stock)\s+offered\s+by\s+(?:the\s+)?selling\s+(?:stockholders|shareholders)\s*[:\-]?\s*(None\.?|[\d,]+\s+shares)",
        r"all\s+of\s+the\s+([\d,]+)\s+shares\s+of\s+common\s+stock\s+are\s+being\s+sold\s+by\s+(?:the\s+)?selling\s+(?:stockholders|shareholders)",
        r"selling\s+(?:stockholders|shareholders)\s+(?:are\s+)?offering\s+([\d,]+)\s+shares",
        r"([\d,]+)\s+shares\s+(?:are being )?offered\s+by\s+(?:the\s+)?selling\s+(?:stockholders|shareholders)",
        r"our principal (?:stockholder|shareholder)[^.!?]{0,400}?\bis offering\s+([\d,]+)\s+shares",
    ]
    primary = _first_match(primary_patterns, cover)
    secondary = _first_match(secondary_patterns, cover)

    def component_fact(field, match):
        raw = match.group(1)
        if raw.lower().startswith("none"):
            return _fact(field, match, 1, "shares", "0.99", "Offering summary", value="0")
        # Summary captures include the word shares; _number needs only the digits.
        numeric = re.search(r"[\d,]+", raw).group(0)
        return _fact(field, match, 1, "shares", "0.98", value=_number(numeric))

    if primary:
        facts.append(component_fact("primary_shares", primary))
    if secondary:
        facts.append(component_fact("secondary_shares", secondary))

    total_matches = list(re.finditer(
        r"(?:a total of|offering consists of|offering of)\s+([\d,]+)\s+shares", cover, re.I))
    distinct = {_number(m.group(1)) for m in total_matches}
    if len(distinct) == 1:
        facts.append(_fact("shares_offered", total_matches[0], 1, "shares", "0.96"))
    elif len(distinct) > 1:
        facts.extend(_fact("shares_offered", m, 1, "shares", "0.80") for m in total_matches)

    primary_value = next((f.value for f in facts if f.field_name == "primary_shares"), None)
    secondary_value = next((f.value for f in facts if f.field_name == "secondary_shares"), None)
    if primary_value is not None and secondary_value is not None:
        facts.append(ParsedFact(
            "shares_offered", primary_value + secondary_value, "shares", Decimal("0.98"),
            "Derived from explicit issuer and selling-stockholder base offering shares",
            "Prospectus cover / offering summary", True, "primary_shares + secondary_shares"))
    elif (not total_matches and primary and secondary_value is None
          and re.match(r"(?:we|the company)\s+(?:are|is)\s+offering", primary.group(0), re.I)):
        # "We are offering N" is itself a direct statement of the base offering
        # count, rather than an inference that an unstated secondary side is zero.
        facts.append(ParsedFact("shares_offered", primary_value, "shares", Decimal("0.94"),
                                re.sub(r"\s+", " ", primary.group(0))[:500], "Prospectus cover",
                                False, None))
    elif not total_matches and secondary and primary_value is None:
        # The pure-secondary "all of the N shares" construction explicitly gives
        # the total. Other isolated secondary component wording does not.
        if re.match(r"all\s+of\s+the", secondary.group(0), re.I):
            facts.append(ParsedFact("shares_offered", secondary_value, "shares", Decimal("0.98"),
                                    re.sub(r"\s+", " ", secondary.group(0))[:500],
                                    "Prospectus cover", False, None))

    # Prefer an explicit aggregate across classes. If none exists, accept only a
    # non-class-specific post-offering label; never sum class counts ourselves.
    post_total = re.search(
        r"total\s+class\s+[A-Za-z0-9]+(?:\s+and\s+class\s+[A-Za-z0-9]+)+\s+common\s+stock\s+"
        r"to\s+be\s+outstanding\s+after\s+this\s+offering\s*[:\-]?\s*([\d,]+)\s+shares", cover, re.I)
    post_patterns = [
        r"(?:shares\s+of\s+)?common\s+stock\s+outstanding\s+immediately\s+after\s+giving\s+effect\s+to\s+this\s+offering\s*[:\-]?\s*([\d,]+)\s+shares",
        r"(?:shares\s+of\s+)?common\s+stock\s+(?:to\s+be\s+)?outstanding\s+immediately\s+(?:after|following)\s+(?:this|the)\s+offering\s*[:\-]?\s*([\d,]+)\s+shares",
        r"common\s+stock\s+to\s+be\s+outstanding\s+after\s+this\s+offering\s*[:\-]?\s*([\d,]+)\s+shares",
        r"([\d,]+)\s+shares\s+of\s+(?:our\s+)?(?:common stock|ordinary shares)\s+will\s+be\s+outstanding\s+immediately\s+(?:after|following)\s+(?:this|the)\s+offering",
    ]
    post = post_total or _first_match(post_patterns, cover)
    if post:
        facts.append(_fact("shares_outstanding_post_ipo", post, 1, "shares", "0.98", "Offering summary"))
    return facts
