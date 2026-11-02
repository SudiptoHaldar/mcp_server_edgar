"""Submissions parsing, history-file selection, filingDate filtering, sorting, pagination.

``FilingsService`` answers the two data questions behind the MCP tools: filings an issuer
submitted in a calendar year (paged) and its newest filing, each optionally restricted to exact
form types. Membership and order are decided by ``filingDate`` only; ``reportDate`` is carried as
context (gotchas #2). Older years are served from the history files listed in ``filings.files``,
fetching only those whose date range overlaps the year (gotchas #5). URL building and the
client-facing record dict belong to v1.04; tool signatures belong to req_002.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from mcp_edgar.sec_client import SecClient
from mcp_edgar.settings import Settings
from mcp_edgar.tickers import Issuer

logger = logging.getLogger(__name__)

MIN_YEAR = 1993  # EDGAR electronic filing begins (execution_plan.md Q6)
MAX_YEAR = 9999
RECENT_ARRAYS = ("accessionNumber", "filingDate", "reportDate", "form", "primaryDocument")


class FilingsError(Exception):
    """Base for this module's errors."""


class InvalidYear(FilingsError, ValueError):
    def __init__(self, year: object) -> None:
        super().__init__(f"year must be between {MIN_YEAR} and {MAX_YEAR}, got {year!r}")
        self.year = year


class InvalidForms(FilingsError, ValueError):
    def __init__(self, forms: object, detail: str) -> None:
        super().__init__(f"forms {detail}: {forms!r}")
        self.forms = forms


class InvalidPagination(FilingsError, ValueError):
    """``limit`` below 1, ``offset`` below 0, or a non-integer."""


class SubmissionsError(FilingsError, ValueError):
    """An SEC payload is not the expected shape; the message names the source and the problem."""


@dataclass(frozen=True, slots=True)
class Filing:
    """One filing as listed by the SEC; ``primary_document`` is raw and untrusted until v1.04."""

    cik: str
    form: str
    filing_date: date
    report_date: date | None
    accession_number: str
    primary_document: str | None


@dataclass(frozen=True, slots=True)
class FilingsPage:
    """One page of an issuer's filings for a calendar year, newest first."""

    issuer: Issuer
    year: int
    forms: tuple[str, ...] | None
    total_count: int
    offset: int
    limit: int
    filings: tuple[Filing, ...]

    @property
    def next_offset(self) -> int | None:
        """Offset of the next page, or ``None`` on the last page; an empty page never points on."""
        if not self.filings:
            return None
        following = self.offset + len(self.filings)
        return following if following < self.total_count else None


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def validate_year(year: object) -> int:
    """An ``int`` within ``[MIN_YEAR, MAX_YEAR]``; future years are valid and simply empty."""
    if not _is_int(year) or not MIN_YEAR <= year <= MAX_YEAR:  # type: ignore[operator]
        raise InvalidYear(year)
    return year  # type: ignore[return-value]


def normalize_forms(forms: object) -> tuple[str, ...] | None:
    """``None``/empty → no filter; otherwise upper-cased, stripped, de-duplicated form types."""
    if forms is None:
        return None
    if isinstance(forms, str | bytes):
        raise InvalidForms(forms, "must be a list of form types, not a single string")
    try:
        items = list(forms)  # type: ignore[call-overload]
    except TypeError:
        raise InvalidForms(forms, "must be a list of form types") from None
    if not items:
        return None
    out: list[str] = []
    for item in items:
        if not isinstance(item, str) or not item.strip():
            raise InvalidForms(forms, "entries must be non-empty strings")
        value = item.strip().upper()
        if value not in out:
            out.append(value)
    return tuple(out)


def _parse_date(value: object, *, source: str, where: str, field: str) -> date | None:
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise SubmissionsError(f"{source}: {where} has a malformed {field} {value!r}") from None


def parse_filings(cik: str, block: Mapping[str, Any], *, source: str) -> list[Filing]:
    """Turn the SEC parallel arrays (recent block or a whole history file) into ``Filing``s."""
    if not isinstance(block, Mapping):
        raise SubmissionsError(f"{source}: filings block is not an object")
    arrays: dict[str, list[Any]] = {}
    for name in RECENT_ARRAYS:
        values = block.get(name)
        if not isinstance(values, list):
            raise SubmissionsError(f"{source}: missing or non-list array {name!r}")
        arrays[name] = values
    lengths = {name: len(values) for name, values in arrays.items()}
    if len(set(lengths.values())) != 1:
        detail = ", ".join(f"{name}={count}" for name, count in lengths.items())
        raise SubmissionsError(f"{source}: array lengths differ: {detail}")
    filings: list[Filing] = []
    for i in range(lengths["form"]):
        where = f"entry {i}"
        accession = str(arrays["accessionNumber"][i] or "").strip()
        if not accession:
            raise SubmissionsError(f"{source}: {where} has an empty accessionNumber")
        filing_date = _parse_date(
            arrays["filingDate"][i], source=source, where=where, field="filingDate"
        )
        if filing_date is None:
            raise SubmissionsError(f"{source}: {where} has an empty filingDate")
        report_date = _parse_date(
            arrays["reportDate"][i], source=source, where=where, field="reportDate"
        )
        primary = arrays["primaryDocument"][i]
        primary_document = None if primary in (None, "") else str(primary)
        form = str(arrays["form"][i]).strip()
        filings.append(Filing(cik, form, filing_date, report_date, accession, primary_document))
    return filings


def _history_entries(files: object, *, source: str) -> list[tuple[date, date, str]]:
    """``(filingTo, filingFrom, name)`` for every ``files[]`` entry, newest first."""
    if files is None:
        return []
    if not isinstance(files, list):
        raise SubmissionsError(f"{source}: 'files' is not a list")
    entries: list[tuple[date, date, str]] = []
    for i, entry in enumerate(files):
        where = f"files[{i}]"
        if not isinstance(entry, Mapping):
            raise SubmissionsError(f"{source}: {where} is not an object")
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            raise SubmissionsError(f"{source}: {where} has no name")
        start = _parse_date(entry.get("filingFrom"), source=source, where=where, field="filingFrom")
        end = _parse_date(entry.get("filingTo"), source=source, where=where, field="filingTo")
        if start is None or end is None:
            raise SubmissionsError(f"{source}: {where} lacks filingFrom/filingTo")
        entries.append((end, start, name.strip()))
    entries.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return entries


def history_files_for_year(files: object, year: int, *, source: str) -> list[str]:
    """Names of history files whose ``[filingFrom, filingTo]`` overlaps the calendar year."""
    start, end = date(year, 1, 1), date(year, 12, 31)
    return [
        name
        for to, frm, name in _history_entries(files, source=source)
        if frm <= end and to >= start
    ]


def filter_filings(
    filings: Iterable[Filing], *, year: int | None, forms: tuple[str, ...] | None
) -> list[Filing]:
    """Keep filings received in ``year`` (by ``filing_date``) whose form is in ``forms``."""
    return [
        f
        for f in filings
        if (year is None or f.filing_date.year == year)
        and (forms is None or f.form.upper() in forms)
    ]


def sort_filings(filings: Iterable[Filing]) -> list[Filing]:
    """De-duplicate by accession number (first wins), then newest first with a stable tie-break."""
    unique: dict[str, Filing] = {}
    for filing in filings:
        unique.setdefault(filing.accession_number, filing)
    return sorted(unique.values(), key=lambda f: (f.filing_date, f.accession_number), reverse=True)


def paginate(
    filings: Sequence[Filing], *, offset: int, limit: int
) -> tuple[tuple[Filing, ...], int | None]:
    """Slice one page; ``next_offset`` is ``None`` when nothing follows."""
    page = tuple(filings[offset : offset + limit])
    following = offset + len(page)
    return page, (following if following < len(filings) else None)


def validate_limit(limit: object, *, page_cap: int) -> int:
    """``None`` → ``page_cap``; an int >= 1 is clamped to the cap; else ``InvalidPagination``."""
    if limit is None:
        return page_cap
    if not _is_int(limit) or limit < 1:  # type: ignore[operator]
        raise InvalidPagination(f"limit must be an integer >= 1, got {limit!r}")
    return min(limit, page_cap)  # type: ignore[type-var]


def validate_offset(offset: object) -> int:
    """An int ``>= 0``; else ``InvalidPagination``."""
    if not _is_int(offset) or offset < 0:  # type: ignore[operator]
        raise InvalidPagination(f"offset must be an integer >= 0, got {offset!r}")
    return offset  # type: ignore[return-value]


class FilingsService:
    """Stateless filing queries for one ``SecClient``; the client's cache does the memoising."""

    def __init__(self, client: SecClient, settings: Settings) -> None:
        self._client = client
        self._settings = settings

    def list_filings_by_year(
        self,
        issuer: Issuer,
        year: int,
        *,
        forms: object = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> FilingsPage:
        """Filings the SEC received from ``issuer`` during calendar ``year``, newest first."""
        year = validate_year(year)
        normalized = normalize_forms(forms)
        limit = validate_limit(limit, page_cap=self._settings.page_cap)
        offset = validate_offset(offset)
        source = f"CIK{issuer.cik}.json"
        block = self._filings_block(self._client.fetch_submissions(issuer.cik), source)
        records = parse_filings(issuer.cik, block["recent"], source=source)
        names = history_files_for_year(block.get("files"), year, source=source)
        logger.debug("year %d for CIK %s: %d history file(s) overlap", year, issuer.cik, len(names))
        for name in names:
            records.extend(self._history_records(issuer.cik, name, source))
        matches = sort_filings(filter_filings(records, year=year, forms=normalized))
        page, _ = paginate(matches, offset=offset, limit=limit)
        return FilingsPage(issuer, year, normalized, len(matches), offset, limit, page)

    def latest_filing(self, issuer: Issuer, *, forms: object = None) -> Filing | None:
        """Newest filing by ``filing_date``; with ``forms``, walks history files on a miss."""
        normalized = normalize_forms(forms)
        source = f"CIK{issuer.cik}.json"
        block = self._filings_block(self._client.fetch_submissions(issuer.cik), source)
        recent = parse_filings(issuer.cik, block["recent"], source=source)
        matches = sort_filings(filter_filings(recent, year=None, forms=normalized))
        if matches:
            return matches[0]
        if normalized is None:
            return None  # no filings at all: the newest one would be in the recent block
        for _to, _frm, name in _history_entries(block.get("files"), source=source):
            logger.debug("latest %s for CIK %s: searching %s", normalized, issuer.cik, name)
            records = self._history_records(issuer.cik, name, source)
            matches = sort_filings(filter_filings(records, year=None, forms=normalized))
            if matches:
                return matches[0]
        return None

    @staticmethod
    def _filings_block(payload: Any, source: str) -> Mapping[str, Any]:
        if not isinstance(payload, Mapping):
            raise SubmissionsError(f"{source}: payload is not an object")
        block = payload.get("filings")
        if not isinstance(block, Mapping) or not isinstance(block.get("recent"), Mapping):
            raise SubmissionsError(f"{source}: payload lacks filings.recent")
        return block

    def _history_records(self, cik: str, name: str, source: str) -> list[Filing]:
        try:
            payload = self._client.fetch_submissions_file(name)
        except ValueError as exc:
            raise SubmissionsError(
                f"{source}: files[] entry {name!r} is not a safe file name"
            ) from exc
        return parse_filings(cik, payload, source=name)
