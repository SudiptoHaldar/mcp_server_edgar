"""Tests for mcp_edgar.edgar_urls: CIK/accession spelling, safe primary documents, records."""

import json
from datetime import date
from typing import Any

import httpx
import pytest

from mcp_edgar.edgar_urls import (
    ARCHIVES_BASE_URL,
    FilingRecord,
    accession_compact,
    archive_cik,
    build_filing_url,
    build_primary_document_url,
    safe_primary_document,
    to_record,
    to_records,
)
from mcp_edgar.filings import Filing, FilingsService, parse_filings
from mcp_edgar.sec_client import SecClient, SecUnavailable
from mcp_edgar.settings import Settings
from mcp_edgar.tickers import Issuer, TickerResolver
from tests.conftest import ClientFactory, fixture_json

SUBS_URL = "https://data.sec.gov/submissions/CIK0000320193.json"
HIST_URL = "https://data.sec.gov/submissions/CIK0000320193-submissions-001.json"
AAPL = Issuer(cik="0000320193", name="Apple Inc.", ticker="AAPL", exchange="Nasdaq")
SUBMISSIONS = fixture_json("submissions_CIK0000320193.json")
HISTORY = fixture_json("CIK0000320193-submissions-001.json")
RECENT_FILINGS = parse_filings(AAPL.cik, SUBMISSIONS["filings"]["recent"], source="recent")
BY_ACCESSION = {f.accession_number: f for f in RECENT_FILINGS}

AAPL_DIR = f"{ARCHIVES_BASE_URL}320193/"
RECORD_KEYS = [
    "form",
    "filing_date",
    "report_date",
    "accession_number",
    "filing_url",
    "primary_document_url",
]


def ok(payload: Any) -> httpx.Response:
    return httpx.Response(200, json=payload)


# --- archive_cik / accession_compact --------------------------------------------------------


@pytest.mark.parametrize(
    ("cik", "expected"),
    [("0000320193", "320193"), ("0000001234", "1234"), ("320193", "320193"), (320193, "320193")],
)
def test_archive_cik_strips_leading_zeros(cik: Any, expected: str) -> None:
    assert archive_cik(cik) == expected


@pytest.mark.parametrize("cik", ["abc", "", "12345678901", None, 3.5])
def test_archive_cik_rejects_non_cik(cik: Any) -> None:
    with pytest.raises(ValueError):
        archive_cik(cik)


@pytest.mark.parametrize(
    ("accession", "expected"),
    [
        ("0000320193-26-000008", "000032019326000008"),
        ("0001104659-05-058421", "000110465905058421"),
    ],
)
def test_accession_compact(accession: str, expected: str) -> None:
    assert accession_compact(accession) == expected


@pytest.mark.parametrize(
    "accession",
    [
        "",
        "0000320193-26-8",
        "0000320193_26_000008",
        " 0000320193-26-000008",
        "000032019326000008",
        "0000320193-26-0000081",
        "0000320193-26-000008\n",
        None,
        12345,
    ],
)
def test_accession_compact_rejects_malformed(accession: Any) -> None:
    with pytest.raises(ValueError, match="accession number must look like"):
        accession_compact(accession)


# --- build_filing_url -----------------------------------------------------------------------


def test_build_filing_url_for_aapl_ten_k() -> None:
    assert build_filing_url("0000320193", "0000320193-26-000008") == (
        "https://www.sec.gov/Archives/edgar/data/320193/000032019326000008/"
        "0000320193-26-000008-index.htm"
    )


def test_build_filing_url_short_cik_and_suffix() -> None:
    url = build_filing_url("0000001234", "0001104659-05-058421")
    assert url.startswith(f"{ARCHIVES_BASE_URL}1234/000110465905058421/")
    assert url.endswith("-index.htm")


def test_build_filing_url_invalid_accession_raises() -> None:
    with pytest.raises(ValueError):
        build_filing_url("0000320193", "bogus")


# --- safe_primary_document ------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "aapl-20250927.htm",
        "xslF345X06/form4.xml",
        "xslF345X03/edgardoc.xml",
        "a/b/c/d.htm",
        "form4.xml",
        "d751788d10q.htm",
        "ny20012345x1_8k.htm",
        "A.B-C_D.HTM",
        "0001.txt",
    ],
)
def test_safe_primary_document_accepts(value: str) -> None:
    assert safe_primary_document(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "",
        None,
        7,
        b"form4.xml",
        "../evil.htm",
        "a/../b.htm",
        "/abs.htm",
        "abs.htm/",
        "a//b.htm",
        "a\\b.htm",
        "%2e%2e/x.htm",
        "..%2fx.htm",
        ".hidden",
        "a/.hidden",
        "a b.htm",
        "a\tb.htm",
        "form4.xml\n",
        "x.htm?x=1",
        "x.htm#f",
        "http://evil/x.htm",
        ".",
        "..",
        "a" * 256 + ".htm",
    ],
)
def test_safe_primary_document_rejects(value: Any) -> None:
    assert safe_primary_document(value) is None


def test_safe_primary_document_max_length_boundary() -> None:
    assert safe_primary_document("a" * 255) == "a" * 255
    assert safe_primary_document("a" * 256) is None


# --- build_primary_document_url -------------------------------------------------------------


def test_primary_url_for_form4_subdirectory() -> None:
    assert build_primary_document_url(
        "0000320193", "0000320193-26-000012", "xslF345X06/form4.xml"
    ) == (f"{AAPL_DIR}000032019326000012/xslF345X06/form4.xml")


@pytest.mark.parametrize("value", ["", None, "../evil.htm", "/abs.htm"])
def test_primary_url_none_for_unsafe(value: Any) -> None:
    assert build_primary_document_url("0000320193", "0000320193-26-000012", value) is None


def test_primary_url_validates_accession_even_when_document_unsafe() -> None:
    with pytest.raises(ValueError):
        build_primary_document_url("0000320193", "bogus", "../evil.htm")


# --- to_record on fixture filings -----------------------------------------------------------


def test_ten_k_record_exact() -> None:
    record = to_record(BY_ACCESSION["0000320193-26-000008"])
    assert record == {
        "form": "10-K",
        "filing_date": "2026-01-30",
        "report_date": "2025-09-27",
        "accession_number": "0000320193-26-000008",
        "filing_url": f"{AAPL_DIR}000032019326000008/0000320193-26-000008-index.htm",
        "primary_document_url": f"{AAPL_DIR}000032019326000008/aapl-20250927.htm",
    }
    assert list(record) == RECORD_KEYS


def test_form4_record_has_subdirectory_primary_url() -> None:
    record = to_record(BY_ACCESSION["0000320193-26-000012"])
    assert record["form"] == "4" and record["filing_date"] == "2026-02-05"
    assert record["primary_document_url"] == f"{AAPL_DIR}000032019326000012/xslF345X06/form4.xml"


def test_empty_primary_document_gives_none_but_filing_url() -> None:
    record = to_record(BY_ACCESSION["0000320193-25-000110"])
    assert record["primary_document_url"] is None
    assert record["filing_url"] == f"{AAPL_DIR}000032019325000110/0000320193-25-000110-index.htm"


def test_traversal_primary_document_gives_none_but_filing_url() -> None:
    record = to_record(BY_ACCESSION["0000320193-25-000066"])
    assert record["primary_document_url"] is None
    assert record["filing_url"].endswith("/0000320193-25-000066-index.htm")
    assert ".." not in record["filing_url"]


def test_missing_report_date_is_none() -> None:
    record = to_record(BY_ACCESSION["0001308179-25-000010"])
    assert record["form"] == "DEF 14A" and record["report_date"] is None


def test_records_are_json_serialisable_with_str_or_none_values() -> None:
    for filing in RECENT_FILINGS:
        record = to_record(filing)
        assert list(record) == RECORD_KEYS
        assert all(isinstance(v, str) or v is None for v in record.values())
        json.dumps(record)


def test_to_record_raises_on_malformed_accession() -> None:
    bad = Filing(AAPL.cik, "8-K", date(2026, 1, 1), None, "not-an-accession", None)
    with pytest.raises(ValueError):
        to_record(bad)


# --- to_records over service pages ----------------------------------------------------------


def make_service(make_client: ClientFactory) -> FilingsService:
    client, _, _ = make_client({SUBS_URL: [ok(SUBMISSIONS)], HIST_URL: [ok(HISTORY)]})
    return FilingsService(client, Settings.from_env({}))


def test_records_for_a_year_page_keep_order_and_shape(make_client: ClientFactory) -> None:
    page = make_service(make_client).list_filings_by_year(AAPL, 2025)
    records = to_records(page.filings)
    assert len(records) == 8
    assert [r["filing_date"] for r in records] == [str(f.filing_date) for f in page.filings]
    assert all(
        r["filing_url"].startswith(AAPL_DIR) and r["filing_url"].endswith("-index.htm")
        for r in records
    )
    assert len({r["filing_url"] for r in records}) == 8


def test_records_for_history_year(make_client: ClientFactory) -> None:
    page = make_service(make_client).list_filings_by_year(AAPL, 2005)
    records = to_records(page.filings)
    assert (
        records[0]["filing_url"] == f"{AAPL_DIR}000110465905058421/0001104659-05-058421-index.htm"
    )
    assert records[0]["primary_document_url"] == f"{AAPL_DIR}000110465905058421/a05-20674_110k.htm"


def test_latest_filing_record(make_client: ClientFactory) -> None:
    latest = make_service(make_client).latest_filing(AAPL)
    assert latest is not None
    record: FilingRecord = to_record(latest)
    assert record["form"] == "4" and record["filing_date"] == "2026-02-05"


def test_to_records_empty() -> None:
    assert to_records([]) == []


# --- live -----------------------------------------------------------------------------------


@pytest.mark.live
def test_live_filing_url_opens_on_sec_gov() -> None:
    settings = Settings.from_env({})
    try:
        with SecClient(settings) as client:
            issuer = TickerResolver(client).resolve("AAPL")
            latest = FilingsService(client, settings).latest_filing(issuer)
            assert latest is not None
            record = to_record(latest)
            response = httpx.head(
                record["filing_url"],
                headers={"User-Agent": settings.user_agent},
                follow_redirects=True,
                timeout=30,
            )
    except (SecUnavailable, httpx.TransportError) as exc:
        pytest.skip(f"SEC unreachable: {exc}")
    assert response.status_code < 400
