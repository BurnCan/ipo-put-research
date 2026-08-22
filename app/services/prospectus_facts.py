"""Persistence and conservative canonical promotion for parsed facts."""
from decimal import Decimal
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.models import IPO, IPOFact
from app.services.prospectus_parser import (CANONICAL_PROMOTION_CONFIDENCE, PARSER_NAME,
                                             PARSER_VERSION, ParsedFact)

SUPPORTED_FIELDS = {"ipo_price", "shares_offered", "primary_shares", "secondary_shares", "shares_outstanding_post_ipo"}

def store_facts(db: Session, ipo: IPO, filing_id: int, facts: list[ParsedFact]) -> int:
    created = 0
    for fact in facts:
        key = format(fact.value.normalize(), "f")
        exists = db.scalar(select(IPOFact.id).where(
            IPOFact.ipo_id == ipo.id, IPOFact.filing_id == filing_id,
            IPOFact.field_name == fact.field_name, IPOFact.parser_name == PARSER_NAME,
            IPOFact.parser_version == PARSER_VERSION, IPOFact.value_key == key))
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
        IPOFact.confidence >= CANONICAL_PROMOTION_CONFIDENCE)).all()
    updated = ambiguities = derived_created = 0
    by_field = {}
    for fact in facts:
        if fact.field_name in SUPPORTED_FIELDS: by_field.setdefault(fact.field_name, []).append(fact)
    for field, candidates in by_field.items():
        values = {Decimal(str(x.value_numeric)) for x in candidates if x.value_numeric is not None}
        if len(values) != 1: ambiguities += 1; continue
        value = values.pop()
        if getattr(ipo, field) is None or Decimal(str(getattr(ipo, field))) != value:
            setattr(ipo, field, value); updated += 1
    if ipo.ipo_price is not None and ipo.shares_offered is not None:
        value = Decimal(str(ipo.ipo_price)) * Decimal(str(ipo.shares_offered))
        confidence = min((Decimal(str(f.confidence)) for f in facts if f.field_name in {"ipo_price", "shares_offered"}), default=Decimal("0"))
        derived = ParsedFact("deal_size", value, "USD", confidence, "Derived from canonical IPO price and shares offered",
                             "Derived", True, "ipo_price * shares_offered")
        derived_created = store_facts(db, ipo, ipo.final_prospectus_filing_id, [derived])
        if confidence >= CANONICAL_PROMOTION_CONFIDENCE and (ipo.deal_size is None or Decimal(str(ipo.deal_size)) != value):
            ipo.deal_size = value; updated += 1
    db.flush()
    return {"updated": updated, "ambiguities": ambiguities, "facts_created": derived_created}
