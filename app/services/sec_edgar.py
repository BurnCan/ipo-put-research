from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import PurePosixPath
import re
import time
import httpx
from app.config import settings

SEC_ARCHIVES = "https://www.sec.gov/Archives"


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


def _headers() -> dict[str, str]:
    return {
        "User-Agent": settings.sec_user_agent,
        "Accept-Encoding": "gzip, deflate",
        "Host": "www.sec.gov",
    }


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
