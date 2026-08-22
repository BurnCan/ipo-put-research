"""Deterministic, deliberately narrow prospectus lockup extraction."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
import re

PARSER_NAME = "deterministic_lockup_parser"
PARSER_VERSION = "1.0"
CONFIDENCE_VERY_HIGH = Decimal("0.97")
CONFIDENCE_HIGH = Decimal("0.92")
CONFIDENCE_PLAUSIBLE = Decimal("0.82")

HEADINGS = re.compile(
    r"^(?:underwriting|shares eligible for (?:future )?sale|lock[ -]?up (?:agreements?|arrangements?)|"
    r"restrictions? on sale)\s*$", re.I
)
CONCEPT = re.compile(r"lock[ -]?up|agree(?:d)?(?:.{0,100})?(?:not to|will not)|(?:may|will) not (?:offer|sell)|restricted from", re.I)
EARLY = re.compile(r"waiv(?:e|er)|early release|release .*shares|blackout|staggered release|partial release", re.I)
ANCHOR = re.compile(r"(?:after|following) (?:the )?date of (?:this|the) prospectus", re.I)


@dataclass(frozen=True)
class LockupSection:
    heading: str
    text: str
    start_line: int


@dataclass(frozen=True)
class ParsedLockup:
    holder_group: str
    holder_group_text: str | None
    lockup_type: str
    duration_days: int | None
    stated_expiration_date: date | None
    calculated_expiration_date: date | None
    shares_locked: Decimal | None
    percentage_locked: Decimal | None
    early_release_exists: bool
    early_release_terms: str | None
    confidence: Decimal
    source_excerpt: str
    source_locator: str


def find_lockup_sections(text: str) -> list[LockupSection]:
    """Return only recognized heading regions containing a lockup concept."""
    lines = text.splitlines()
    starts = [(i, line.strip()) for i, line in enumerate(lines) if HEADINGS.match(line.strip())]
    sections = []
    for n, (start, heading) in enumerate(starts):
        end = starts[n + 1][0] if n + 1 < len(starts) else min(len(lines), start + 350)
        region = "\n".join(lines[start + 1:end])[:30000]
        if CONCEPT.search(region):
            sections.append(LockupSection(heading, region, start + 1))
    return sections


def parse_duration(text: str) -> int | None:
    numeric = re.search(r"\b(90|120|180)\s*(?:calendar\s+)?days\b", text, re.I)
    if numeric:
        return int(numeric.group(1))
    written = re.search(r"\b(one hundred (?:and )?eighty) days\b", text, re.I)
    if written:
        return 180
    # Exact six months is represented by the conventional 180-day research estimate;
    # approximate phrasing is intentionally rejected.
    if re.search(r"\bsix months\b", text, re.I) and not re.search(r"approximately|about", text, re.I):
        return 180
    return None


def parse_expiration_date(text: str) -> date | None:
    match = re.search(r"(?:terminate|expire|expiration)(?:s|d)?(?:\s+on|\s+is)?\s+"
                      r"(January|February|March|April|May|June|July|August|September|October|November|December)"
                      r"\s+(\d{1,2}),\s+(20\d{2})", text, re.I)
    if not match:
        return None
    try:
        return datetime.strptime(" ".join(match.groups()), "%B %d %Y").date()
    except ValueError:
        return None


def parse_holder_group(text: str) -> tuple[str, str | None]:
    patterns = [
        ("company", r"\b(?:we|our company|the company)\b"),
        ("selling_stockholders", r"\b(?:our )?selling stockholders?\b"),
        ("existing_stockholders", r"\b(?:substantially all |certain |our )?(?:holders|stockholders) of (?:our )?(?:common )?stock\b"),
        ("pre_ipo_investors", r"\b(?:pre-IPO investors?|principal stockholders?)\b"),
        ("directors_officers", r"\b(?:our )?directors?(?:,? (?:and )?(?:executive )?officers?)?\b"),
        ("employees", r"\b(?:our )?employees?\b"),
        ("sponsor", r"\b(?:our |the )?sponsor\b"),
    ]
    # Company is only selected for issuance language, avoiding "we expect" boilerplate.
    if re.search(r"(?:we|the company) (?:have |has )?agree(?:d)? not to (?:issue|sell)", text, re.I):
        return "company", re.search(r"(?:we|the company)", text, re.I).group(0)
    for group, pattern in patterns[1:]:
        match = re.search(pattern, text, re.I)
        if match:
            start = max(0, text.rfind(".", 0, match.start()) + 1)
            end = text.find(", have", match.end())
            if end < 0: end = text.find(" agreed", match.end())
            phrase = text[start:(end if end >= 0 else min(len(text), match.end() + 80))].strip(" ,;\n")
            return group, phrase[:300]
    return "unknown", None


def detect_early_release(text: str) -> tuple[bool, str | None]:
    match = EARLY.search(text)
    if not match:
        return False, None
    start, end = max(0, match.start() - 100), min(len(text), match.end() + 220)
    return True, " ".join(text[start:end].split())


def _shares(text: str) -> Decimal | None:
    # Require grammatical linkage to locked/subject shares, not merely proximity.
    match = re.search(r"(?:an aggregate of )?([\d,]+) shares (?:are |is |will be )?(?:subject to|covered by) (?:the )?lock[ -]?up", text, re.I)
    return Decimal(match.group(1).replace(",", "")) if match else None


def extract_lockup_agreements(text: str, prospectus_date: date | None = None) -> list[ParsedLockup]:
    agreements: list[ParsedLockup] = []
    for section in find_lockup_sections(text):
        # Normalized SEC text has inconsistent wrapping; sentence-sized evidence windows
        # retain enough neighboring language for holder, term and exception provenance.
        flat = " ".join(section.text.split())
        sentences = re.split(r"(?<=[.!?])\s+", flat)
        for i, sentence in enumerate(sentences):
            if not CONCEPT.search(sentence):
                continue
            window = " ".join(sentences[i:min(len(sentences), i + 3)])
            duration, stated = parse_duration(sentence), parse_expiration_date(sentence)
            if duration is None and stated is None:
                continue
            group, group_text = parse_holder_group(sentence)
            lockup_type = ("company_lockup" if group == "company" else
                           "market_standoff" if "market standoff" in window.lower() else
                           "underwriter_lockup" if re.search(r"underwriter|representative", window, re.I) else "unknown")
            calculated = prospectus_date + timedelta(days=duration) if duration and prospectus_date and ANCHOR.search(window) else None
            early, terms = detect_early_release(window)
            confidence = CONFIDENCE_HIGH if lockup_type == "underwriter_lockup" and group != "unknown" else CONFIDENCE_PLAUSIBLE
            if stated and group != "unknown": confidence = CONFIDENCE_VERY_HIGH
            excerpt = window[:1200]
            agreements.append(ParsedLockup(group, group_text, lockup_type, duration, stated, calculated,
                                            _shares(window), None, early, terms, confidence, excerpt,
                                            f"{section.heading}, line {section.start_line}"))
    return agreements
