"""SEC filings: edgartools 5.62.1 as a library for lookups, our own fetcher for bytes.

edgartools resolves a company (ticker, CIK or name) and lists its filings with
their filing dates and primary documents. Every byte an agent reasons about (a
filing's document, the XBRL concept data) is fetched through the evidence
service's own fetcher, so it is stored exactly with a receipt like any capture.
Tests replace `EdgarToolsBackend` with a fake, so they never reach SEC.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any, Protocol

from stemma.evidence.errors import EvidenceError

ACCESSION_RE = re.compile(r"^\d{10}-\d{2}-\d{6}$")
_CONCEPT_RE = re.compile(r"^(?:([A-Za-z][A-Za-z0-9-]{0,30}):)?([A-Za-z][A-Za-z0-9_]{0,200})$")
_DOCUMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,200}$")


@dataclass(frozen=True)
class CompanyRef:
    cik: int
    name: str


@dataclass(frozen=True)
class FilingRef:
    accession: str
    cik: int
    company: str
    form: str
    filing_date: str  # YYYY-MM-DD
    report_date: str | None
    primary_document: str | None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def document_url(self, document: str | None = None) -> str:
        name = document or self.primary_document
        if not name:
            raise EvidenceError("no_primary_document", f"{self.accession} names no document")
        return (f"https://www.sec.gov/Archives/edgar/data/{self.cik}/"
                f"{self.accession.replace('-', '')}/{name}")


class SecBackend(Protocol):
    def resolve(self, company: str) -> CompanyRef: ...

    def filings(self, cik: int, form: str | None) -> list[FilingRef]: ...

    def lookup(self, accession: str) -> FilingRef: ...


def check_accession(accession: str) -> str:
    if not isinstance(accession, str) or not ACCESSION_RE.match(accession):
        raise EvidenceError("bad_accession", "expected an accession like 0000320193-25-000079")
    return accession


def check_document(document: str) -> str:
    if not _DOCUMENT_RE.match(document) or ".." in document:
        raise EvidenceError("bad_document", "a document is a file name inside the filing")
    return document


def parse_concept(concept: str) -> tuple[str, str]:
    match = _CONCEPT_RE.match(concept or "")
    if match is None:
        raise EvidenceError("bad_concept", "expected a concept like us-gaap:Revenues")
    return (match.group(1) or "us-gaap"), match.group(2)


def concept_url(cik: int, taxonomy: str, tag: str) -> str:
    return f"https://data.sec.gov/api/xbrl/companyconcept/CIK{cik:010d}/{taxonomy}/{tag}.json"


def facts_from_concept_json(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Every fact as filed: one row per (filing, period, unit), never collapsed."""
    facts: list[dict[str, Any]] = []
    units = data.get("units")
    if not isinstance(units, dict):
        return facts
    for unit, rows in units.items():
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict) or "accn" not in row or "filed" not in row:
                continue
            facts.append({
                "value": row.get("val"),
                "unit": unit,
                "start": row.get("start"),
                "end": row.get("end"),
                "fiscal_year": row.get("fy"),
                "fiscal_period": row.get("fp"),
                "form": row.get("form"),
                "accession": row.get("accn"),
                "filed": row.get("filed"),
                "frame": row.get("frame"),
            })
    facts.sort(key=lambda f: (str(f["end"]), str(f["filed"]), str(f["accession"])))
    return facts


class EdgarToolsBackend:
    """The real backend. Needs `STEMMA_SEC_IDENTITY` ("Name email"), which SEC
    requires in every request's User-Agent."""

    def __init__(self, identity: str) -> None:
        if not identity or "@" not in identity:
            raise EvidenceError(
                "sec_identity_missing",
                "SEC requires a contact identity; set STEMMA_SEC_IDENTITY",
            )
        import edgar

        edgar.set_identity(identity)
        self._edgar = edgar

    def resolve(self, company: str) -> CompanyRef:
        try:
            entity = self._edgar.Company(company)
        except Exception as exc:
            raise EvidenceError("sec_company_not_found", f"{company!r}: {exc}") from exc
        cik = getattr(entity, "cik", None)
        if not cik or int(cik) <= 0:
            raise EvidenceError("sec_company_not_found", f"{company!r} has no CIK")
        return CompanyRef(int(cik), str(getattr(entity, "name", company)))

    def filings(self, cik: int, form: str | None) -> list[FilingRef]:
        try:
            entity = self._edgar.Company(cik)
            found = entity.get_filings(form=form) if form else entity.get_filings()
        except Exception as exc:
            raise EvidenceError("sec_unavailable", f"listing filings failed: {exc}") from exc
        refs: list[FilingRef] = []
        for item in found:
            refs.append(FilingRef(
                accession=str(item.accession_no),
                cik=int(item.cik),
                company=str(item.company),
                form=str(item.form),
                filing_date=str(item.filing_date)[:10],
                report_date=(str(item.report_date)[:10] if getattr(item, "report_date", None)
                             else None),
                primary_document=getattr(item, "primary_document", None) or None,
            ))
        return refs

    def lookup(self, accession: str) -> FilingRef:
        try:
            filing = self._edgar.get_by_accession_number(accession)
        except Exception as exc:
            raise EvidenceError("sec_unavailable", f"lookup failed: {exc}") from exc
        if filing is None:
            raise EvidenceError("sec_filing_not_found", f"no filing {accession}")
        try:
            primary = filing.document.document
        except Exception:  # noqa: BLE001 - no primary document is reported by the caller
            primary = None
        return FilingRef(str(filing.accession_no), int(filing.cik), str(filing.company),
                         str(filing.form), str(filing.filing_date)[:10], None, primary)
