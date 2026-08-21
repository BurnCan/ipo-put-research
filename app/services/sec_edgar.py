from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import PurePosixPath
import re
import time
import httpx
from app.config import settings

SEC_ARCHIVES = "https://www.sec.gov/Archives"
SEC_SUBMISSIONS = "https://data.sec.gov/submissions"
RELEVANT_SUBMISSION_FORMS = {
    "424B4", "EFFECT", "8-A12B", "8-A12G", "10-Q", "10-K", "8-K", "20-F", "6-K"
}


@dataclass(frozen=True)
class IndexFiling:
    cik: str
    company_name: str
    form_type: str
    filed_at: date
    filing_path: str

    @property
    def accession_number(self) -> str:
        name = PurePosixPath(self.filing_path).name
        stem = name.removesuffix(".txt")
        return stem

    @property
    def sec_url(self) -> str:
        return f"{SEC_ARCHIVES}/{self.filing_path.lstrip('/')}"


@dataclass(frozen=True)
class SubmissionFiling:
    accession_number: str
    form_type: str
    filed_at: date
    filing_path: str
    sec_url: str


@dataclass(frozen=True)
class SubmissionData:
    company_name: str | None
    ticker: str | None
    exchange: str | None
    filings: list[SubmissionFiling]


def _headers() -> dict[str, str]:
    return {
        "User-Agent": settings.sec_user_agent,
        "Accept-Encoding": "gzip, deflate",
        "Host": "www.sec.gov",
    }


def fetch_company_submissions(cik: str, retries: int = 3, timeout: float = 30.0) -> dict:
    """Fetch an issuer submissions document, retrying transient HTTP/network failures."""
    padded_cik = str(cik).strip().zfill(10)
    url = f"{SEC_SUBMISSIONS}/CIK{padded_cik}.json"
    headers = _headers()
    headers["Host"] = "data.sec.gov"
    with httpx.Client(headers=headers, timeout=timeout, follow_redirects=True) as client:
        for attempt in range(retries):
            try:
                response = client.get(url)
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise ValueError("SEC submissions response is not a JSON object")
                return payload
            except (httpx.RequestError, httpx.HTTPStatusError) as exc:
                transient = isinstance(exc, httpx.RequestError) or exc.response.status_code in {408, 429, 500, 502, 503, 504}
                if not transient or attempt == retries - 1:
                    raise
                time.sleep(0.5 * (2**attempt))
    raise RuntimeError("unreachable")


def parse_company_submissions(payload: dict, cik: str) -> SubmissionData:
    """Extract issuer metadata and relevant rows from ``filings.recent``."""
    if not isinstance(payload, dict):
        raise ValueError("SEC submissions payload must be an object")
    name = payload.get("name")
    name = name.strip() if isinstance(name, str) and name.strip() else None
    tickers = payload.get("tickers") or []
    exchanges = payload.get("exchanges") or []
    # SEC arrays are aligned by security. Milestone 1 conservatively uses only
    # the first security rather than attempting to model multiple instruments.
    ticker = tickers[0].strip() if tickers and isinstance(tickers[0], str) and tickers[0].strip() else None
    exchange = exchanges[0].strip() if exchanges and isinstance(exchanges[0], str) and exchanges[0].strip() else None

    recent = payload.get("filings", {}).get("recent")
    if not isinstance(recent, dict):
        raise ValueError("SEC submissions payload is missing filings.recent")
    required = ("accessionNumber", "filingDate", "form")
    if any(not isinstance(recent.get(field), list) for field in required):
        raise ValueError("SEC submissions filings.recent has malformed arrays")

    documents = recent.get("primaryDocument") or []
    filings: list[SubmissionFiling] = []
    numeric_cik = str(cik).strip().lstrip("0") or "0"
    for index, (accession, filed, form) in enumerate(zip(*(recent[field] for field in required))):
        if form not in RELEVANT_SUBMISSION_FORMS or not isinstance(accession, str):
            continue
        accession = accession.strip()
        if not re.fullmatch(r"\d{10}-\d{2}-\d{6}", accession):
            continue
        try:
            filed_at = date.fromisoformat(filed)
        except (TypeError, ValueError):
            continue
        accession_compact = accession.replace("-", "")
        document = documents[index].strip() if index < len(documents) and isinstance(documents[index], str) else ""
        filename = document or f"{accession_compact}-index.html"
        filing_path = f"edgar/data/{numeric_cik}/{accession_compact}/{filename}"
        filings.append(SubmissionFiling(accession, form, filed_at, filing_path, f"{SEC_ARCHIVES}/{filing_path}"))
    return SubmissionData(name, ticker, exchange, filings)


def fetch_quarter_master_index(year: int, quarter: int) -> str:
    url = f"{SEC_ARCHIVES}/edgar/full-index/{year}/QTR{quarter}/master.idx"
    with httpx.Client(headers=_headers(), timeout=30.0, follow_redirects=True) as client:
        response = client.get(url)
        response.raise_for_status()
        return response.text


def parse_master_index(text: str, forms: set[str] | None = None) -> list[IndexFiling]:
    forms = forms or {"S-1", "S-1/A", "F-1", "F-1/A"}
    rows: list[IndexFiling] = []
    started = False
    for raw in text.splitlines():
        line = raw.strip()
        if not started:
            if line.startswith("---"):
                started = True
            continue
        if not line or "|" not in line:
            continue
        parts = line.split("|")
        if len(parts) != 5:
            continue
        cik, name, form, filed, path = [p.strip() for p in parts]
        if form not in forms:
            continue
        if not re.fullmatch(r"\d+", cik):
            continue
        rows.append(
            IndexFiling(
                cik=cik.zfill(10),
                company_name=name,
                form_type=form,
                filed_at=date.fromisoformat(filed),
                filing_path=path,
            )
        )
    return rows


def quarter_for_month(month: int) -> int:
    return ((month - 1) // 3) + 1


def iter_quarters(start: date, end: date):
    y, q = start.year, quarter_for_month(start.month)
    end_y, end_q = end.year, quarter_for_month(end.month)
    while (y, q) <= (end_y, end_q):
        yield y, q
        q += 1
        if q == 5:
            y += 1
            q = 1


def fetch_registration_filings(start: date, end: date) -> list[IndexFiling]:
    out: list[IndexFiling] = []
    for year, quarter in iter_quarters(start, end):
        text = fetch_quarter_master_index(year, quarter)
        for row in parse_master_index(text):
            if start <= row.filed_at <= end:
                out.append(row)
        time.sleep(0.12)
    return out
