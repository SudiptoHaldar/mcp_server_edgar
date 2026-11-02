"""Official EDGAR links for a filing and the client-facing filing record.

The filing-detail page (``...-index.htm``) is always linked; it lists every document of the
filing. The primary-document link is added only when the SEC-supplied ``primaryDocument`` passes
a strict path check, because that value is interpolated into a URL and comes from an external
response (gotchas #6). CIKs are zero-stripped for ``/Archives/`` paths (gotchas #4).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from typing import TypedDict

from mcp_edgar.filings import Filing
from mcp_edgar.tickers import format_cik

logger = logging.getLogger(__name__)

ARCHIVES_BASE_URL = "https://www.sec.gov/Archives/edgar/data/"
ACCESSION_RE = re.compile(r"\d{10}-\d{2}-\d{6}")  # used with fullmatch
SEGMENT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")  # one path segment; used with fullmatch
MAX_PRIMARY_DOCUMENT_LENGTH = 255


class FilingRecord(TypedDict):
    """The guide's filing record; all six keys are always present."""

    form: str
    filing_date: str
    report_date: str | None
    accession_number: str
    filing_url: str
    primary_document_url: str | None


def archive_cik(cik: int | str) -> str:
    """CIK spelling for ``/Archives/`` paths: leading zeros stripped (0000320193 → 320193)."""
    return str(int(format_cik(cik)))


def accession_compact(accession_number: str) -> str:
    """``0000320193-26-000008`` → ``000032019326000008``; anything else raises ``ValueError``."""
    if not isinstance(accession_number, str) or not ACCESSION_RE.fullmatch(accession_number):
        raise ValueError(
            f"accession number must look like 0000000000-00-000000, got {accession_number!r}"
        )
    return accession_number.replace("-", "")


def _filing_directory(cik: int | str, accession_number: str) -> str:
    return f"{ARCHIVES_BASE_URL}{archive_cik(cik)}/{accession_compact(accession_number)}/"


def build_filing_url(cik: int | str, accession_number: str) -> str:
    """The filing-detail page URL; always available for a well-formed accession number."""
    return f"{_filing_directory(cik, accession_number)}{accession_number}-index.htm"


def safe_primary_document(value: object) -> str | None:
    """Return ``value`` when it is a safe relative document path, else ``None``. Never raises.

    Allowed: one or more ``/``-separated segments, each starting with a letter or digit and
    containing only letters, digits, ``.``, ``_``, ``-`` (Form 4 uses ``xslF345X06/form4.xml``).
    Rejected: empty values, ``.``/``..``/hidden segments, empty segments, leading or trailing
    ``/``, ``\\``, ``%`` (so no percent-encoded traversal), whitespace, ``?``, ``#``, ``:``.
    """
    if not isinstance(value, str) or not 1 <= len(value) <= MAX_PRIMARY_DOCUMENT_LENGTH:
        return None
    if value.startswith("/") or value.endswith("/"):
        return None
    if not all(SEGMENT_RE.fullmatch(segment) for segment in value.split("/")):
        return None
    return value


def build_primary_document_url(
    cik: int | str, accession_number: str, primary_document: object
) -> str | None:
    """Primary-document URL, or ``None`` when ``primary_document`` is missing or unsafe."""
    directory = _filing_directory(cik, accession_number)  # validates CIK and accession first
    safe = safe_primary_document(primary_document)
    if safe is None:
        if primary_document not in (None, ""):
            logger.debug(
                "dropping unsafe primaryDocument %r for %s", primary_document, accession_number
            )
        return None
    return directory + safe


def to_record(filing: Filing) -> FilingRecord:
    """Client-facing record for one filing (dates as ISO strings, URLs built defensively)."""
    return {
        "form": filing.form,
        "filing_date": filing.filing_date.isoformat(),
        "report_date": filing.report_date.isoformat() if filing.report_date else None,
        "accession_number": filing.accession_number,
        "filing_url": build_filing_url(filing.cik, filing.accession_number),
        "primary_document_url": build_primary_document_url(
            filing.cik, filing.accession_number, filing.primary_document
        ),
    }


def to_records(filings: Iterable[Filing]) -> list[FilingRecord]:
    """Records for a sequence of filings, preserving order."""
    return [to_record(filing) for filing in filings]
