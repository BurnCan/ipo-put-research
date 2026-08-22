"""Batch orchestration while keeping fetch, parse, persistence and promotion separate."""
from pathlib import Path
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload
from app.models import Filing, IPO, IPOFact
from app.services.prospectus_documents import fetch_filing_document, normalize_filing_document
from app.services.prospectus_facts import promote_canonical_facts, store_facts
from app.services.prospectus_parser import PARSER_NAME, PARSER_VERSION, extract_ipo_facts

def process_final_prospectuses(db: Session, *, limit: int | None = None, ipo_id: int | None = None,
                               reparse: bool = False, refetch: bool = False, cache_dir=None,
                               client=None, classification_status: str | None = None,
                               candidate_type: str | None = None,
                               offering_status: str | None = None) -> dict[str, int]:
    summary = {k: 0 for k in ("ipos_seen", "documents_fetched", "documents_cached", "documents_failed",
                               "documents_normalized", "facts_created", "canonical_fields_updated", "ambiguities", "errors")}
    stmt = select(IPO).options(joinedload(IPO.company), joinedload(IPO.final_prospectus).joinedload(Filing.company)).where(IPO.final_prospectus_filing_id.is_not(None)).order_by(IPO.id)
    if ipo_id is not None: stmt = stmt.where(IPO.id == ipo_id)
    if classification_status is not None: stmt = stmt.where(IPO.classification_status == classification_status)
    if candidate_type is not None: stmt = stmt.where(IPO.candidate_type == candidate_type)
    if offering_status is not None: stmt = stmt.where(IPO.offering_status == offering_status)
    # SQL ordering matters here: compose the research-universe predicates before
    # limiting the selected IPOs.
    if limit is not None: stmt = stmt.limit(limit)
    for ipo in db.scalars(stmt).unique():
        summary["ipos_seen"] += 1
        try:
            document, fetched = fetch_filing_document(db, ipo.final_prospectus, force=refetch, cache_dir=cache_dir, client=client)
            if document.fetch_status != "success": summary["documents_failed"] += 1; db.commit(); continue
            summary["documents_fetched" if fetched else "documents_cached"] += 1
            if normalize_filing_document(db, document, force=refetch, cache_dir=cache_dir): summary["documents_normalized"] += 1
            already = db.scalar(select(IPOFact.id).where(IPOFact.ipo_id == ipo.id, IPOFact.filing_id == document.filing_id,
                                IPOFact.parser_name == PARSER_NAME, IPOFact.parser_version == PARSER_VERSION))
            if reparse or not already:
                facts = extract_ipo_facts(Path(document.text_path).read_text(encoding="utf-8"))
                summary["facts_created"] += store_facts(db, ipo, document.filing_id, facts)
            result = promote_canonical_facts(db, ipo)
            summary["facts_created"] += result["facts_created"]
            summary["canonical_fields_updated"] += result["updated"]
            summary["ambiguities"] += result["ambiguities"]
            db.commit()
        except Exception:
            db.rollback(); summary["errors"] += 1
    return summary
