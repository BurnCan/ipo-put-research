"""Persistence and conservative canonical promotion for parsed facts."""
from decimal import Decimal
import hashlib
import json
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.models import IPO, IPOFact
from app.services.prospectus_parser import (CANONICAL_PROMOTION_CONFIDENCE, PARSER_NAME,
                                             PARSER_VERSION, ParsedFact)

SUPPORTED_FIELDS = {"ipo_price", "shares_offered", "primary_shares", "secondary_shares", "shares_outstanding_post_ipo"}

def _fact_identity(fact: ParsedFact) -> str:
    """Return a stable identity for the value and the evidence supporting it."""
    payload = {
        "value": format(fact.value.normalize(), "f"),
        "unit": fact.unit,
        "confidence": format(fact.confidence.normalize(), "f"),
        "source_excerpt": fact.source_excerpt,
        "source_locator": fact.source_locator,
        "is_derived": fact.is_derived,
        "derivation": fact.derivation,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

def store_facts(db: Session, ipo: IPO, filing_id: int, facts: list[ParsedFact]) -> int:
    created = 0
    for fact in facts:
        key = _fact_identity(fact)
        exists = db.scalar(select(IPOFact.id).where(
            IPOFact.ipo_id == ipo.id, IPOFact.filing_id == filing_id,
            IPOFact.field_name == fact.field_name, IPOFact.parser_name == PARSER_NAME,
            IPOFact.parser_version == PARSER_VERSION,
            # The attribute comparison also recognizes rows written before value_key
            # was expanded from a numeric value into a provenance identity.
            IPOFact.value_numeric == fact.value, IPOFact.unit == fact.unit,
            IPOFact.confidence == fact.confidence,
            IPOFact.source_excerpt == fact.source_excerpt,
            IPOFact.source_locator == fact.source_locator,
            IPOFact.is_derived == fact.is_derived,
            IPOFact.derivation.is_(None) if fact.derivation is None else IPOFact.derivation == fact.derivation))
        if exists: continue
        db.add(IPOFact(ipo_id=ipo.id, filing_id=filing_id, field_name=fact.field_name,
                       value_numeric=fact.value, value_key=key, unit=fact.unit,
                       confidence=fact.confidence, parser_name=PARSER_NAME, parser_version=PARSER_VERSION,
                       source_excerpt=fact.source_excerpt, source_locator=fact.source_locator,
                       is_derived=fact.is_derived, derivation=fact.derivation))
        db.flush(); created += 1
    return created

def promote_canonical_facts(db: Session, ipo: IPO) -> dict[str, int]:
    if ipo.final_prospectus_filing_id is None: return {"updated": 0, "ambiguities": 0, "facts_created": 0}
    facts = db.scalars(select(IPOFact).where(
        IPOFact.ipo_id == ipo.id, IPOFact.filing_id == ipo.final_prospectus_filing_id,
        # A parser-version change may intentionally correct old semantics. Keep
        # old provenance rows, but canonicalize from the current interpretation.
        IPOFact.parser_name == PARSER_NAME, IPOFact.parser_version == PARSER_VERSION,
        IPOFact.confidence >= CANONICAL_PROMOTION_CONFIDENCE)).all()
    updated = ambiguities = derived_created = 0
    by_field = {}
    for fact in facts:
        if fact.field_name in SUPPORTED_FIELDS: by_field.setdefault(fact.field_name, []).append(fact)
    resolved = {}
    for field, candidates in by_field.items():
        values = {Decimal(str(x.value_numeric)) for x in candidates if x.value_numeric is not None}
        if len(values) != 1:
            ambiguities += 1
            if getattr(ipo, field) is not None:
                setattr(ipo, field, None); updated += 1
            continue
        value = values.pop()
        resolved[field] = (value, candidates)
        if getattr(ipo, field) is None or Decimal(str(getattr(ipo, field))) != value:
            setattr(ipo, field, value); updated += 1
    if "ipo_price" in resolved and "shares_offered" in resolved:
        price, price_facts = resolved["ipo_price"]
        shares, share_facts = resolved["shares_offered"]
        value = price * shares
        confidence = min(Decimal(str(f.confidence)) for f in price_facts + share_facts)
        derived = ParsedFact("deal_size", value, "USD", confidence, "Derived from canonical IPO price and shares offered",
                             "Derived", True, "ipo_price * shares_offered")
        derived_created = store_facts(db, ipo, ipo.final_prospectus_filing_id, [derived])
        if confidence >= CANONICAL_PROMOTION_CONFIDENCE and (ipo.deal_size is None or Decimal(str(ipo.deal_size)) != value):
            ipo.deal_size = value; updated += 1
    elif ipo.deal_size is not None:
        ipo.deal_size = None; updated += 1
    db.flush()
    return {"updated": updated, "ambiguities": ambiguities, "facts_created": derived_created}
