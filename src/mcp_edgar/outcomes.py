"""Structured tool outcomes: one envelope per status, and the exception → outcome mapping.

Every anticipated result of a tool call is returned to the client as a JSON object whose first
key is ``status`` (gotchas #8, execution_plan.md Q5); the tools never raise for these. Anything
``outcome_for_exception`` does not recognise returns ``None`` so the tool re-raises it and the SDK
reports a crash. This module is MCP-free and does no I/O, so both tools share it and it is tested
without a server.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from mcp_edgar.edgar_urls import FilingRecord
from mcp_edgar.filings import (
    FilingsPage,
    InvalidForms,
    InvalidPagination,
    InvalidYear,
    SubmissionsError,
)
from mcp_edgar.form_types import FormType, FormTypeCatalogue, InvalidQuery
from mcp_edgar.sec_client import SecClientError, SecNotFound
from mcp_edgar.tickers import (
    AmbiguousTicker,
    InvalidCik,
    InvalidTicker,
    Issuer,
    TickerMapError,
    UnknownTicker,
)

STATUS_OK = "ok"
STATUS_NOT_FOUND = "not_found"
STATUS_AMBIGUOUS = "ambiguous"
STATUS_INVALID_INPUT = "invalid_input"
STATUS_UPSTREAM_ERROR = "upstream_error"

Outcome = dict[str, Any]


def latest_filing_ok(
    issuer: Issuer, forms: tuple[str, ...] | None, record: FilingRecord | None
) -> Outcome:
    """``ok`` envelope for ``get_latest_filing``; ``record`` is ``None`` when nothing matched."""
    label = ", ".join(forms) if forms else None
    who = f"{issuer.name} ({issuer.ticker})"
    if record is None:
        message = (
            f"{who} has no filings of form {label} on EDGAR."
            if label
            else f"{who} has no filings on EDGAR."
        )
    elif label:
        message = f"Newest {label} by {who}: filed {record['filing_date']}."
    else:
        message = f"Newest filing by {who}: {record['form']} filed {record['filing_date']}."
    return {
        "status": STATUS_OK,
        "company": issuer.as_dict(),
        "forms": list(forms) if forms else None,
        "filing": record,
        "message": message,
    }


def list_filings_ok(page: FilingsPage, records: list[FilingRecord]) -> Outcome:
    """``ok`` envelope for ``list_filings_by_year``: one page of records plus paging fields."""
    issuer = page.issuer
    who = f"{issuer.name} ({issuer.ticker})"
    label = f" of form {', '.join(page.forms)}" if page.forms else ""
    total, returned = page.total_count, len(records)
    if total == 0:
        message = f"{who} has no filings{label} with a filing date in {page.year}."
    else:
        noun = "filing" if total == 1 else "filings"
        head = f"{who}: {total} {noun}{label} with a filing date in {page.year}; "
        if returned == 0:
            message = f"{head}offset {page.offset} is past the end."
        else:
            tail = f", next offset {page.next_offset}." if page.next_offset is not None else "."
            message = f"{head}showing {page.offset + 1}-{page.offset + returned}{tail}"
    return {
        "status": STATUS_OK,
        "company": issuer.as_dict(),
        "year": page.year,
        "forms": list(page.forms) if page.forms else None,
        "limit": page.limit,
        "offset": page.offset,
        "total_count": total,
        "returned_count": returned,
        "next_offset": page.next_offset,
        "filings": records,
        "message": message,
    }


def list_form_types_ok(
    catalogue: FormTypeCatalogue,
    query: str | None,
    limit: int,
    offset: int,
    total: int,
    page: Sequence[FormType],
    next_offset: int | None,
) -> Outcome:
    """``ok`` envelope for ``list_form_types``: one page of the bundled catalogue."""
    span = "-".join(dict.fromkeys((catalogue.quarters[0], catalogue.quarters[-1])))  # first-last
    returned = len(page)
    noun = "form type" if total == 1 else "form types"
    verb = "matches" if total == 1 else "match"
    if query is None:
        head = f"EDGAR lists {total or 'no'} {noun} filed in {span}"
    else:
        head = f"{total or 'No'} {noun} {verb} {query!r} in {span}"
    if total == 0:
        message = f"{head}."
    elif returned == 0:
        message = f"{head}; offset {offset} is past the end."
    else:
        tail = f", next offset {next_offset}." if next_offset is not None else "."
        message = f"{head}; showing {offset + 1}-{offset + returned}{tail}"
    return {
        "status": STATUS_OK,
        "query": query,
        "limit": limit,
        "offset": offset,
        "total_count": total,
        "returned_count": returned,
        "next_offset": next_offset,
        "catalogue": catalogue.metadata(),
        "form_types": [entry.as_dict() for entry in page],
        "message": message,
    }


def not_found(ticker: str, message: str) -> Outcome:
    """No SEC issuer is listed under ``ticker`` (the caller's spelling, stripped)."""
    return {"status": STATUS_NOT_FOUND, "ticker": ticker, "message": message}


def ambiguous(ticker: str, candidates: Sequence[Issuer], message: str) -> Outcome:
    """Several issuers share ``ticker``; ``candidates`` keep the SEC file order. None is chosen."""
    return {
        "status": STATUS_AMBIGUOUS,
        "ticker": ticker,
        "candidates": [issuer.as_dict() for issuer in candidates],
        "message": message,
    }


def invalid_input(field: str, message: str) -> Outcome:
    """An argument was rejected before any request; ``field`` names it."""
    return {"status": STATUS_INVALID_INPUT, "field": field, "message": message}


def upstream_error(
    message: str,
    *,
    retryable: bool,
    status_code: int | None = None,
    retry_after_seconds: float | None = None,
) -> Outcome:
    """SEC EDGAR did not answer usefully; ``retryable`` preserves the client's retry semantics."""
    return {
        "status": STATUS_UPSTREAM_ERROR,
        "retryable": retryable,
        "status_code": status_code,
        "retry_after_seconds": retry_after_seconds,
        "message": message,
    }


def outcome_for_exception(exc: Exception) -> Outcome | None:
    """Envelope for an anticipated data-layer exception, or ``None`` for anything unexpected.

    Subclasses are tested before bases: ``InvalidTicker``, ``InvalidForms`` and
    ``SubmissionsError`` are all ``ValueError``s, so ``ValueError`` itself is never mapped.
    """
    message = str(exc)
    if isinstance(exc, InvalidTicker):
        return invalid_input("ticker", message)
    if isinstance(exc, InvalidCik):
        return invalid_input("cik", message)
    if isinstance(exc, InvalidForms):
        return invalid_input("forms", message)
    if isinstance(exc, InvalidYear):
        return invalid_input("year", message)
    if isinstance(exc, InvalidPagination):
        return invalid_input("limit" if message.startswith("limit") else "offset", message)
    if isinstance(exc, InvalidQuery):
        return invalid_input("query", message)
    if isinstance(exc, UnknownTicker):
        return not_found(exc.ticker, message)
    if isinstance(exc, AmbiguousTicker):
        hint = "; call again with the chosen candidate's cik"
        return ambiguous(exc.ticker, exc.candidates, message + hint)
    if isinstance(exc, SecClientError):
        status_code = 404 if isinstance(exc, SecNotFound) else getattr(exc, "status_code", None)
        return upstream_error(
            message,
            retryable=exc.retryable,
            status_code=status_code,
            retry_after_seconds=getattr(exc, "retry_after", None),
        )
    if isinstance(exc, TickerMapError | SubmissionsError):
        return upstream_error(f"SEC data had an unexpected shape: {message}", retryable=False)
    return None
