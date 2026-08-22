"""Small, conservative parser for final prospectus offering facts."""
from dataclasses import dataclass
from decimal import Decimal
import re

PARSER_NAME = "final_prospectus_offering"
PARSER_VERSION = "2"
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

def _fact(field, match, group, unit, confidence, locator="Prospectus cover"):
    excerpt = re.sub(r"\s+", " ", match.group(0)).strip()[:500]
    return ParsedFact(field, _number(match.group(group)), unit, Decimal(confidence), excerpt, locator)

def extract_ipo_facts(text: str) -> list[ParsedFact]:
    """Extract only explicit offering language; unrelated share figures are ignored."""
    # Cover-page evidence is normally at the start; a bounded window prevents later
    # historical financing and option-plan language becoming offering totals.
    cover = text[:20000]
    facts: list[ParsedFact] = []
    # These deliberately require an explicit public/offering-price label.  A broad
    # "$X per share" match would pick up dilution, option plans and financings.
    price_patterns = [
        r"initial\s+public\s+offering\s+price\s+per\s+share\s+(?:will\s+be|is)\s*\$\s*(\d+(?:\.\d+)?)",
        r"initial\s+public\s+offering\s+price\s+(?:of|:)\s*\$\s*(\d+(?:\.\d+)?)\s+per\s+share",
        r"(?:initial\s+public\s+)?offering\s+price\s+is\s*\$\s*(\d+(?:\.\d+)?)\s+per\s+share",
        r"public\s+offering\s+price\s*:\s*\$\s*(\d+(?:\.\d+)?)\s+per\s+share",
        r"price\s+to\s+public\s*:\s*\$\s*(\d+(?:\.\d+)?)\s+per\s+share",
    ]
    for pattern in price_patterns:
        facts.extend(_fact("ipo_price", match, 1, "USD/share", "0.99")
                     for match in re.finditer(pattern, cover, re.I))

    primary_patterns = [
        r"(?:we|the company) (?:are|is) offering\s+([\d,]+)\s+shares",
        r"([\d,]+)\s+shares (?:are being )?offered by (?:us|the company)",
    ]
    secondary_patterns = [
        r"selling (?:stockholders|shareholders) (?:are )?offering\s+([\d,]+)\s+shares",
        r"([\d,]+)\s+shares (?:are being )?offered by (?:the )?selling (?:stockholders|shareholders)",
        # Bounded to one sentence on the cover so later descriptions of a
        # principal shareholder's transactions cannot become offering facts.
        r"our principal (?:stockholder|shareholder)[^.!?]{0,400}?\bis offering\s+([\d,]+)\s+shares",
    ]
    primary = next((re.search(p, cover, re.I) for p in primary_patterns if re.search(p, cover, re.I)), None)
    secondary = next((re.search(p, cover, re.I) for p in secondary_patterns if re.search(p, cover, re.I)), None)
    if primary: facts.append(_fact("primary_shares", primary, 1, "shares", "0.96"))
    if secondary: facts.append(_fact("secondary_shares", secondary, 1, "shares", "0.96"))

    total_matches = list(re.finditer(r"(?:a total of|offering consists of|offering of)\s+([\d,]+)\s+shares", cover, re.I))
    distinct = {_number(m.group(1)) for m in total_matches}
    if len(distinct) == 1:
        facts.append(_fact("shares_offered", total_matches[0], 1, "shares", "0.96"))
    elif len(distinct) > 1:
        facts.extend(_fact("shares_offered", m, 1, "shares", "0.80") for m in total_matches)
    if primary and secondary:
        primary_value = _number(primary.group(1))
        secondary_value = _number(secondary.group(1))
        facts.append(ParsedFact(
            "shares_offered", primary_value + secondary_value, "shares", Decimal("0.96"),
            "Derived from explicit issuer and selling-stockholder shares on the prospectus cover",
            "Prospectus cover", True, "primary_shares + secondary_shares"))
    elif not total_matches and primary and not secondary:
        facts.append(ParsedFact("shares_offered", _number(primary.group(1)), "shares", Decimal("0.94"),
                                re.sub(r"\s+", " ", primary.group(0))[:500], "Prospectus cover", True, "primary_shares (no selling shares stated)"))
    elif not total_matches and secondary and not primary:
        facts.append(ParsedFact("shares_offered", _number(secondary.group(1)), "shares", Decimal("0.94"),
                                re.sub(r"\s+", " ", secondary.group(0))[:500], "Prospectus cover", True, "secondary_shares (no company shares stated)"))

    post = re.search(r"([\d,]+)\s+shares (?:of (?:our )?(?:common stock|ordinary shares) )?will be outstanding immediately (?:after|following) (?:this|the) offering", text, re.I)
    if not post:
        post = re.search(r"shares outstanding immediately (?:after|following) (?:this|the) offering\s*[:\-]?\s*([\d,]+)", text, re.I)
    if post: facts.append(_fact("shares_outstanding_post_ipo", post, 1, "shares", "0.96", "Offering summary"))
    return facts
