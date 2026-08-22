"""Persistence, conservative primary selection, and cached-document orchestration."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.models import Filing, FilingDocument, IPO, IPOLockup
from app.services.lockup_parser import PARSER_NAME, PARSER_VERSION, ParsedLockup, extract_lockup_agreements

PRINCIPAL_GROUP_PRIORITY = (
    "existing_stockholders",
    "pre_ipo_investors",
    "selling_stockholders",
    "directors_officers",
)
PRIMARY_MIN_CONFIDENCE = 0.90


def _identity(ipo_id: int, filing_id: int, item: ParsedLockup) -> str:
    values = [ipo_id, filing_id, item.lockup_type, item.holder_group, item.holder_group_text,
              item.duration_days, item.stated_expiration_date, item.calculated_expiration_date,
              item.shares_locked, PARSER_NAME, PARSER_VERSION, item.source_excerpt, item.source_locator]
    return hashlib.sha256(json.dumps(values, default=str, separators=(",", ":")).encode()).hexdigest()


def store_lockups(db: Session, ipo: IPO, filing_id: int, items: list[ParsedLockup]) -> int:
    created = 0
    for item in items:
        key = _identity(ipo.id, filing_id, item)
        if db.scalar(select(IPOLockup.id).where(IPOLockup.evidence_key == key)):
            continue
        percentage, derived = item.percentage_locked, False
        if percentage is None and item.shares_locked is not None and ipo.shares_outstanding_post_ipo:
            percentage = item.shares_locked / ipo.shares_outstanding_post_ipo * 100
            derived = True
        db.add(IPOLockup(
            ipo_id=ipo.id, filing_id=filing_id, holder_group=item.holder_group,
            holder_group_text=item.holder_group_text, lockup_type=item.lockup_type,
            duration_days=item.duration_days, stated_expiration_date=item.stated_expiration_date,
            calculated_expiration_date=item.calculated_expiration_date, shares_locked=item.shares_locked,
            percentage_locked=percentage, percentage_is_derived=derived,
            early_release_exists=item.early_release_exists, early_release_terms=item.early_release_terms,
            confidence=item.confidence, parser_name=PARSER_NAME, parser_version=PARSER_VERSION,
            source_excerpt=item.source_excerpt, source_locator=item.source_locator, evidence_key=key))
        created += 1
    db.flush()
    return created


def select_primary_lockup(db: Session, ipo: IPO) -> dict[str, int]:
    """Promote only dated, high-confidence shareholder underwriter agreements.

    Evidence from the most research-relevant holder group wins. Multiple rows in
    that tier agreeing on a date are compatible; distinct dates in that tier are
    materially conflicting.
    """
    candidates = [row for row in db.scalars(select(IPOLockup).where(
        IPOLockup.ipo_id == ipo.id,
        IPOLockup.lockup_type == "underwriter_lockup",
        IPOLockup.holder_group.in_(PRINCIPAL_GROUP_PRIORITY),
        IPOLockup.confidence >= PRIMARY_MIN_CONFIDENCE,
    )).all() if row.stated_expiration_date or row.calculated_expiration_date]
    selected_tier = next(
        (group for group in PRINCIPAL_GROUP_PRIORITY
         if any(row.holder_group == group for row in candidates)),
        None,
    )
    tier_candidates = [row for row in candidates if row.holder_group == selected_tier]
    by_date: dict = {}
    for row in tier_candidates:
        expiration = row.stated_expiration_date or row.calculated_expiration_date
        by_date.setdefault(expiration, []).append(row)
    if len(by_date) != 1:
        changed = int(ipo.primary_lockup_id is not None or ipo.primary_lockup_expiration_date is not None)
        ipo.primary_lockup_id = ipo.primary_lockup_expiration_date = None
        return {"selected": 0, "ambiguity": int(len(by_date) > 1), "cleared": changed}
    expiration, rows = next(iter(by_date.items()))
    chosen = sorted(rows, key=lambda x: (-float(x.confidence), x.id))[0]
    ipo.primary_lockup_id, ipo.primary_lockup_expiration_date = chosen.id, expiration
    return {"selected": 1, "ambiguity": 0, "cleared": 0}


def process_cached_lockups(db: Session, *, limit: int | None = None, ipo_id: int | None = None,
                           reparse: bool = False) -> dict[str, int]:
    summary = {key: 0 for key in ("ipos_seen", "documents_available", "documents_skipped",
                                   "lockups_created", "ipos_with_lockups", "primary_lockups_selected",
                                   "ambiguities", "errors")}
    stmt = select(IPO).options(joinedload(IPO.final_prospectus).joinedload(Filing.document)).where(
        IPO.final_prospectus_filing_id.is_not(None)).order_by(IPO.id)
    if ipo_id is not None: stmt = stmt.where(IPO.id == ipo_id)
    if limit is not None: stmt = stmt.limit(limit)
    for ipo in db.scalars(stmt).unique():
        summary["ipos_seen"] += 1
        try:
            document: FilingDocument | None = ipo.final_prospectus.document
            if not document or document.fetch_status != "success" or not document.text_path or not Path(document.text_path).is_file():
                summary["documents_skipped"] += 1
                continue
            summary["documents_available"] += 1
            already = db.scalar(select(IPOLockup.id).where(IPOLockup.ipo_id == ipo.id,
                IPOLockup.filing_id == document.filing_id, IPOLockup.parser_name == PARSER_NAME,
                IPOLockup.parser_version == PARSER_VERSION))
            if reparse or not already:
                parsed = extract_lockup_agreements(Path(document.text_path).read_text(encoding="utf-8"),
                                                   ipo.final_prospectus.filed_at)
                summary["lockups_created"] += store_lockups(db, ipo, document.filing_id, parsed)
            if db.scalar(select(IPOLockup.id).where(IPOLockup.ipo_id == ipo.id)):
                summary["ipos_with_lockups"] += 1
            result = select_primary_lockup(db, ipo)
            summary["primary_lockups_selected"] += result["selected"]
            summary["ambiguities"] += result["ambiguity"]
            db.commit()
        except Exception:
            db.rollback(); summary["errors"] += 1
    return summary
