"""SEC document retrieval and deterministic HTML normalization."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import time
import httpx
from bs4 import BeautifulSoup
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Filing, FilingDocument, utc_now
from app.services.sec_edgar import _headers

TRANSIENT_STATUSES = {408, 429, 500, 502, 503, 504}


def document_paths(filing: Filing, cache_dir: str | Path | None = None) -> tuple[Path, Path]:
    root = Path(cache_dir or settings.filing_cache_dir)
    folder = root / filing.company.cik.zfill(10) / filing.accession_number
    return folder / "raw.html", folder / "text.txt"


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(content)
    os.replace(temporary, path)


def fetch_filing_document(db: Session, filing: Filing, *, force: bool = False,
                          cache_dir: str | Path | None = None, client=None,
                          retries: int = 3, timeout: float = 30.0) -> tuple[FilingDocument, bool]:
    record = db.query(FilingDocument).filter_by(filing_id=filing.id).one_or_none()
    raw_path, _ = document_paths(filing, cache_dir)
    if record and not force and record.fetch_status == "success" and record.raw_path and Path(record.raw_path).is_file():
        return record, False
    if record is None:
        record = FilingDocument(filing_id=filing.id, source_url=filing.sec_url, fetch_status="pending")
        db.add(record)
    record.source_url = filing.sec_url
    owned = client is None
    http = client or httpx.Client(headers=_headers(), timeout=timeout, follow_redirects=True)
    try:
        for attempt in range(retries):
            try:
                response = http.get(filing.sec_url)
                record.http_status = response.status_code
                if response.status_code in TRANSIENT_STATUSES and attempt + 1 < retries:
                    time.sleep(0.5 * (2 ** attempt)); continue
                response.raise_for_status()
                content = response.content
                _atomic_write(raw_path, content)
                record.fetch_status, record.raw_path = "success", str(raw_path)
                record.sha256 = hashlib.sha256(content).hexdigest()
                record.byte_size = len(content)
                record.content_type = response.headers.get("content-type")
                record.fetched_at, record.updated_at, record.error = utc_now(), utc_now(), None
                db.flush()
                return record, True
            except (httpx.RequestError, httpx.HTTPStatusError) as exc:
                transient = isinstance(exc, httpx.RequestError) or getattr(exc.response, "status_code", None) in TRANSIENT_STATUSES
                if transient and attempt + 1 < retries:
                    time.sleep(0.5 * (2 ** attempt)); continue
                raise
    except Exception as exc:
        record.fetch_status, record.error, record.updated_at = "failed", str(exc)[:2000], utc_now()
        db.flush()
        return record, False
    finally:
        if owned:
            http.close()


def html_to_text(raw: bytes) -> str:
    soup = BeautifulSoup(raw, "html.parser")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    text = soup.get_text("\n")
    lines = [re.sub(r"[ \t\xa0]+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line) + "\n"


def normalize_filing_document(db: Session, document: FilingDocument, *, force: bool = False,
                              cache_dir: str | Path | None = None) -> bool:
    if document.fetch_status != "success" or not document.raw_path:
        return False
    _, text_path = document_paths(document.filing, cache_dir)
    if not force and document.text_path and Path(document.text_path).is_file():
        return False
    normalized = html_to_text(Path(document.raw_path).read_bytes()).encode("utf-8")
    _atomic_write(text_path, normalized)
    document.text_path, document.updated_at = str(text_path), utc_now()
    db.flush()
    return True
