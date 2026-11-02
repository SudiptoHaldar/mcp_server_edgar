"""Tests for mcp_edgar.outcomes: envelope shapes and the exception → outcome mapping."""

import json
from datetime import date
from typing import Any

import pytest

from mcp_edgar.edgar_urls import FilingRecord
from mcp_edgar.filings import (
    Filing,
    FilingsPage,
    InvalidForms,
    InvalidPagination,
    InvalidYear,
    SubmissionsError,
)
from mcp_edgar.form_types import FormType, FormTypeCatalogue, InvalidQuery
from mcp_edgar.outcomes import (
    STATUS_AMBIGUOUS,
    STATUS_INVALID_INPUT,
    STATUS_NOT_FOUND,
    STATUS_OK,
    STATUS_UPSTREAM_ERROR,
    ambiguous,
    invalid_input,
    latest_filing_ok,
    list_filings_ok,
    list_form_types_ok,
    not_found,
    outcome_for_exception,
    upstream_error,
)
from mcp_edgar.sec_client import SecNotFound, SecRequestError, SecUnavailable
from mcp_edgar.tickers import (
    AmbiguousTicker,
    InvalidCik,
    InvalidTicker,
    Issuer,
    TickerMapError,
    UnknownTicker,
)

AAPL = Issuer(cik="0000320193", name="Apple Inc.", ticker="AAPL", exchange="Nasdaq")
DUAL_A = Issuer("0001000001", "Dual Listed Holdings Inc.", "DUAL", "NYSE")
DUAL_B = Issuer("0001000002", "Dual Energy Trust", "DUAL", "CBOE")
URL = "https://data.sec.gov/submissions/CIK0000320193.json"
TEN_K_DIR = "https://www.sec.gov/Archives/edgar/data/320193/000032019326000008/"
RECORD: FilingRecord = {
    "form": "10-K",
    "filing_date": "2026-01-30",
    "report_date": "2025-09-27",
    "accession_number": "0000320193-26-000008",
    "filing_url": TEN_K_DIR + "0000320193-26-000008-index.htm",
    "primary_document_url": TEN_K_DIR + "aapl-20250927.htm",
}


# --- builders -------------------------------------------------------------------------------


def test_ok_with_filing_and_no_forms() -> None:
    outcome = latest_filing_ok(AAPL, None, RECORD)
    assert list(outcome) == ["status", "company", "forms", "filing", "message"]
    assert outcome["status"] == STATUS_OK
    assert outcome["company"] == AAPL.as_dict()
    assert outcome["forms"] is None
    assert outcome["filing"] is RECORD
    assert outcome["message"] == "Newest filing by Apple Inc. (AAPL): 10-K filed 2026-01-30."


def test_ok_with_forms_lists_them() -> None:
    outcome = latest_filing_ok(AAPL, ("10-K", "10-Q"), RECORD)
    assert outcome["forms"] == ["10-K", "10-Q"]
    assert outcome["message"] == "Newest 10-K, 10-Q by Apple Inc. (AAPL): filed 2026-01-30."


@pytest.mark.parametrize(
    ("forms", "message"),
    [
        (("10-ZZ",), "Apple Inc. (AAPL) has no filings of form 10-ZZ on EDGAR."),
        (None, "Apple Inc. (AAPL) has no filings on EDGAR."),
    ],
)
def test_ok_without_filing(forms: tuple[str, ...] | None, message: str) -> None:
    outcome = latest_filing_ok(AAPL, forms, None)
    assert outcome["status"] == STATUS_OK
    assert outcome["filing"] is None
    assert outcome["message"] == message


def test_not_found_shape() -> None:
    outcome = not_found("zz", "no SEC issuer is listed under ticker 'zz'")
    assert outcome == {
        "status": STATUS_NOT_FOUND,
        "ticker": "zz",
        "message": "no SEC issuer is listed under ticker 'zz'",
    }
    assert list(outcome) == ["status", "ticker", "message"]


def test_ambiguous_shape_keeps_order() -> None:
    outcome = ambiguous("dual", (DUAL_A, DUAL_B), "choose one")
    assert list(outcome) == ["status", "ticker", "candidates", "message"]
    assert outcome["status"] == STATUS_AMBIGUOUS
    assert outcome["candidates"] == [DUAL_A.as_dict(), DUAL_B.as_dict()]


def test_invalid_input_shape() -> None:
    assert invalid_input("forms", "bad") == {
        "status": STATUS_INVALID_INPUT,
        "field": "forms",
        "message": "bad",
    }


def test_upstream_error_shape_and_defaults() -> None:
    outcome = upstream_error("down", retryable=True)
    assert list(outcome) == ["status", "retryable", "status_code", "retry_after_seconds", "message"]
    assert outcome["status"] == STATUS_UPSTREAM_ERROR
    assert outcome["retryable"] is True
    assert outcome["status_code"] is None
    assert outcome["retry_after_seconds"] is None
    assert outcome["message"] == "down"


def test_every_envelope_is_json_serialisable() -> None:
    envelopes = [
        latest_filing_ok(AAPL, ("10-K",), RECORD),
        latest_filing_ok(AAPL, None, None),
        not_found("zz", "m"),
        ambiguous("dual", (DUAL_A, DUAL_B), "m"),
        invalid_input("ticker", "m"),
        upstream_error("m", retryable=False, status_code=503, retry_after_seconds=1.5),
    ]
    for envelope in envelopes:
        assert json.loads(json.dumps(envelope)) == envelope
        assert next(iter(envelope)) == "status"


# --- list_filings_ok ------------------------------------------------------------------------

LIST_KEYS = [
    "status",
    "company",
    "year",
    "forms",
    "limit",
    "offset",
    "total_count",
    "returned_count",
    "next_offset",
    "filings",
    "message",
]


def make_page(
    total: int,
    offset: int,
    limit: int,
    returned: int,
    *,
    year: int = 2025,
    forms: tuple[str, ...] | None = None,
) -> tuple[FilingsPage, list[FilingRecord]]:
    filings = tuple(
        Filing(AAPL.cik, "4", date(year, 1, 1), None, f"0000320193-25-{i:06d}", None)
        for i in range(returned)
    )
    page = FilingsPage(AAPL, year, forms, total, offset, limit, filings)
    return page, [dict(RECORD) for _ in range(returned)]


def test_list_ok_keys_counts_and_next_offset() -> None:
    page, records = make_page(8, 0, 3, 3)
    outcome = list_filings_ok(page, records)
    assert list(outcome) == LIST_KEYS
    assert outcome["status"] == STATUS_OK
    assert outcome["company"] == AAPL.as_dict()
    assert outcome["year"] == 2025
    assert outcome["forms"] is None
    assert (outcome["limit"], outcome["offset"]) == (3, 0)
    assert (outcome["total_count"], outcome["returned_count"], outcome["next_offset"]) == (8, 3, 3)
    assert outcome["filings"] is records
    assert outcome["message"] == (
        "Apple Inc. (AAPL): 8 filings with a filing date in 2025; showing 1-3, next offset 3."
    )


def test_list_ok_last_page_has_no_next_offset() -> None:
    page, records = make_page(8, 6, 3, 2)
    outcome = list_filings_ok(page, records)
    assert outcome["next_offset"] is None
    assert (
        outcome["message"]
        == "Apple Inc. (AAPL): 8 filings with a filing date in 2025; showing 7-8."
    )


def test_list_ok_singular_with_forms() -> None:
    page, records = make_page(1, 0, 100, 1, year=2026, forms=("10-K",))
    outcome = list_filings_ok(page, records)
    assert outcome["forms"] == ["10-K"]
    assert outcome["message"] == (
        "Apple Inc. (AAPL): 1 filing of form 10-K with a filing date in 2026; showing 1-1."
    )


def test_list_ok_offset_past_the_end() -> None:
    page, records = make_page(8, 50, 100, 0)
    outcome = list_filings_ok(page, records)
    assert (outcome["returned_count"], outcome["next_offset"], outcome["filings"]) == (0, None, [])
    assert outcome["message"] == (
        "Apple Inc. (AAPL): 8 filings with a filing date in 2025; offset 50 is past the end."
    )


@pytest.mark.parametrize(
    ("forms", "year", "message"),
    [
        (
            ("10-ZZ",),
            2025,
            "Apple Inc. (AAPL) has no filings of form 10-ZZ with a filing date in 2025.",
        ),
        (None, 2020, "Apple Inc. (AAPL) has no filings with a filing date in 2020."),
    ],
)
def test_list_ok_empty(forms: tuple[str, ...] | None, year: int, message: str) -> None:
    page, records = make_page(0, 0, 100, 0, year=year, forms=forms)
    outcome = list_filings_ok(page, records)
    assert (outcome["total_count"], outcome["returned_count"], outcome["next_offset"]) == (
        0,
        0,
        None,
    )
    assert outcome["message"] == message
    assert json.loads(json.dumps(outcome)) == outcome


# --- list_form_types_ok ---------------------------------------------------------------------

FORM_TYPES = (
    FormType("4", "Statement of changes", False, 100, ("Exchange Act",)),
    FormType("10-K", "Annual report", False, 50, ()),
    FormType("10-K/A", "Annual report", True, 5, ()),
)
CATALOGUE = FormTypeCatalogue(
    "2026-11-01", ("2025Q4", "2026Q1", "2026Q2", "2026Q3"), ("src",), FORM_TYPES
)
FORM_KEYS = [
    "status",
    "query",
    "limit",
    "offset",
    "total_count",
    "returned_count",
    "next_offset",
    "catalogue",
    "form_types",
    "message",
]


def test_form_types_ok_keys_and_page() -> None:
    outcome = list_form_types_ok(CATALOGUE, None, 2, 0, 3, FORM_TYPES[:2], 2)
    assert list(outcome) == FORM_KEYS
    assert outcome["status"] == STATUS_OK
    assert outcome["query"] is None
    assert (outcome["limit"], outcome["offset"]) == (2, 0)
    assert (outcome["total_count"], outcome["returned_count"], outcome["next_offset"]) == (3, 2, 2)
    assert outcome["catalogue"] == CATALOGUE.metadata()
    assert outcome["form_types"][0] == FORM_TYPES[0].as_dict()
    assert outcome["message"] == (
        "EDGAR lists 3 form types filed in 2025Q4-2026Q3; showing 1-2, next offset 2."
    )
    assert json.loads(json.dumps(outcome)) == outcome


@pytest.mark.parametrize(
    ("query", "total", "page", "offset", "next_offset", "message"),
    [
        (
            "13g",
            2,
            FORM_TYPES[1:],
            0,
            None,
            "2 form types match '13g' in 2025Q4-2026Q3; showing 1-2.",
        ),
        (
            "10-K/A",
            1,
            FORM_TYPES[2:],
            0,
            None,
            "1 form type matches '10-K/A' in 2025Q4-2026Q3; showing 1-1.",
        ),
        ("zzz", 0, (), 0, None, "No form types match 'zzz' in 2025Q4-2026Q3."),
        (
            None,
            3,
            (),
            50,
            None,
            "EDGAR lists 3 form types filed in 2025Q4-2026Q3; offset 50 is past the end.",
        ),
    ],
)
def test_form_types_ok_messages(
    query: str | None,
    total: int,
    page: tuple[FormType, ...],
    offset: int,
    next_offset: int | None,
    message: str,
) -> None:
    outcome = list_form_types_ok(CATALOGUE, query, 100, offset, total, page, next_offset)
    assert outcome["message"] == message
    assert outcome["returned_count"] == len(page)


def test_invalid_query_maps_to_invalid_input() -> None:
    exc = InvalidQuery("query must be at most 100 characters, got 101")
    assert outcome_for_exception(exc) == {
        "status": STATUS_INVALID_INPUT,
        "field": "query",
        "message": str(exc),
    }


# --- outcome_for_exception ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("exc", "field"),
    [
        (InvalidTicker("ticker '' is empty", ticker=""), "ticker"),
        (InvalidCik("cik must be 1-10 digits, got 'x'", cik="x", ticker="AAPL"), "cik"),
        (InvalidForms("10-K", "must be a list of form types, not a single string"), "forms"),
        (InvalidYear(1850), "year"),
        (InvalidPagination("limit must be an integer >= 1, got 0"), "limit"),
        (InvalidPagination("offset must be an integer >= 0, got -1"), "offset"),
    ],
)
def test_validation_errors_map_to_invalid_input(exc: Exception, field: str) -> None:
    assert outcome_for_exception(exc) == {
        "status": STATUS_INVALID_INPUT,
        "field": field,
        "message": str(exc),
    }


def test_unknown_ticker_maps_to_not_found() -> None:
    exc = UnknownTicker("zz")
    assert outcome_for_exception(exc) == {
        "status": STATUS_NOT_FOUND,
        "ticker": "zz",
        "message": str(exc),
    }


def test_ambiguous_ticker_maps_to_candidates() -> None:
    exc = AmbiguousTicker("dual", candidates=(DUAL_A, DUAL_B))
    outcome = outcome_for_exception(exc)
    assert outcome is not None
    assert outcome["status"] == STATUS_AMBIGUOUS
    assert outcome["ticker"] == "dual"
    assert outcome["candidates"] == [DUAL_A.as_dict(), DUAL_B.as_dict()]
    assert outcome["message"] == f"{exc}; call again with the chosen candidate's cik"


def test_sec_unavailable_is_retryable_with_hints() -> None:
    exc = SecUnavailable(URL, status_code=429, retry_after=2.0, attempts=4)
    assert outcome_for_exception(exc) == {
        "status": STATUS_UPSTREAM_ERROR,
        "retryable": True,
        "status_code": 429,
        "retry_after_seconds": 2.0,
        "message": str(exc),
    }


def test_sec_unavailable_network_error_has_null_status() -> None:
    exc = SecUnavailable(URL, status_code=None, retry_after=None, attempts=4)
    outcome = outcome_for_exception(exc)
    assert outcome is not None
    assert outcome["retryable"] is True
    assert outcome["status_code"] is None
    assert outcome["retry_after_seconds"] is None


def test_sec_not_found_is_not_retryable_404() -> None:
    outcome = outcome_for_exception(SecNotFound(URL))
    assert outcome is not None
    assert (outcome["status"], outcome["retryable"], outcome["status_code"]) == (
        STATUS_UPSTREAM_ERROR,
        False,
        404,
    )


@pytest.mark.parametrize(
    ("exc", "status_code"),
    [
        (SecRequestError(URL, status_code=403), 403),
        (SecRequestError(URL, status_code=200, detail="body is not JSON"), 200),
    ],
)
def test_sec_request_error_is_not_retryable(exc: SecRequestError, status_code: int) -> None:
    outcome = outcome_for_exception(exc)
    assert outcome is not None
    assert outcome["retryable"] is False
    assert outcome["status_code"] == status_code
    assert outcome["retry_after_seconds"] is None


@pytest.mark.parametrize("exc", [TickerMapError("fields missing"), SubmissionsError("lengths")])
def test_malformed_sec_data_is_not_retryable(exc: Exception) -> None:
    outcome = outcome_for_exception(exc)
    assert outcome is not None
    assert outcome["status"] == STATUS_UPSTREAM_ERROR
    assert outcome["retryable"] is False
    assert outcome["status_code"] is None
    assert outcome["message"] == f"SEC data had an unexpected shape: {exc}"


@pytest.mark.parametrize(
    "exc", [ValueError("accession"), RuntimeError(), KeyError("k"), TypeError("t")]
)
def test_unexpected_exceptions_are_not_mapped(exc: Any) -> None:
    assert outcome_for_exception(exc) is None
