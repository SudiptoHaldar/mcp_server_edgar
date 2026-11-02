"""Tests for mcp_edgar.filings: parsing, history selection, filingDate filtering, pagination."""

import copy
from collections.abc import Mapping
from datetime import date
from typing import Any

import httpx
import pytest

from mcp_edgar.filings import (
    Filing,
    FilingsPage,
    FilingsService,
    InvalidForms,
    InvalidPagination,
    InvalidYear,
    SubmissionsError,
    filter_filings,
    history_files_for_year,
    normalize_forms,
    paginate,
    parse_filings,
    sort_filings,
    validate_limit,
    validate_offset,
    validate_year,
)
from mcp_edgar.sec_client import SecClient, SecUnavailable
from mcp_edgar.settings import Settings
from mcp_edgar.tickers import Issuer
from tests.conftest import ClientFactory, fixture_json

SUBS_URL = "https://data.sec.gov/submissions/CIK0000320193.json"
HIST_URL = "https://data.sec.gov/submissions/CIK0000320193-submissions-001.json"
HIST_NAME = "CIK0000320193-submissions-001.json"
AAPL = Issuer(cik="0000320193", name="Apple Inc.", ticker="AAPL", exchange="Nasdaq")

SUBMISSIONS = fixture_json("submissions_CIK0000320193.json")
HISTORY = fixture_json(HIST_NAME)
RECENT = SUBMISSIONS["filings"]["recent"]
FILES = SUBMISSIONS["filings"]["files"]


def ok(payload: Any, **headers: str) -> httpx.Response:
    return httpx.Response(200, json=payload, headers=headers or None)


def status(code: int, **headers: str) -> httpx.Response:
    return httpx.Response(code, headers=headers or None)


def default_routes() -> dict[str, list[httpx.Response]]:
    return {SUBS_URL: [ok(SUBMISSIONS)], HIST_URL: [ok(HISTORY)]}


def make_service(
    make_client: ClientFactory,
    routes: Mapping[str, list[httpx.Response]] | None = None,
    env: Mapping[str, str] | None = None,
) -> tuple[FilingsService, Any, Any]:
    client, recorded, clock = make_client(
        routes if routes is not None else default_routes(), env=env
    )
    return FilingsService(client, Settings.from_env(dict(env or {}))), recorded, clock


def urls(recorded: Any) -> list[str]:
    return [str(r.url) for r in recorded.requests]


def dates(page: FilingsPage) -> list[str]:
    return [str(f.filing_date) for f in page.filings]


# --- validate_year --------------------------------------------------------------------------


@pytest.mark.parametrize("year", [1993, 2005, 2026, 9999])
def test_validate_year_accepts_bounds(year: int) -> None:
    assert validate_year(year) == year


@pytest.mark.parametrize("year", [1992, 10000, "2025", True, 2025.0, None])
def test_validate_year_rejects(year: Any) -> None:
    with pytest.raises(InvalidYear) as excinfo:
        validate_year(year)
    assert "1993" in str(excinfo.value) and "9999" in str(excinfo.value)
    assert excinfo.value.year == year
    assert isinstance(excinfo.value, ValueError)


# --- normalize_forms ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("forms", "expected"),
    [
        (None, None),
        ([], None),
        ((), None),
        (["10-k", " 8-K ", "10-K"], ("10-K", "8-K")),
        (("sc 13g/a",), ("SC 13G/A",)),
    ],
)
def test_normalize_forms(forms: Any, expected: Any) -> None:
    assert normalize_forms(forms) == expected


@pytest.mark.parametrize("forms", ["10-K", b"10-K", [""], [" "], [10], ["10-K", None], 7])
def test_normalize_forms_rejects(forms: Any) -> None:
    with pytest.raises(InvalidForms) as excinfo:
        normalize_forms(forms)
    assert excinfo.value.forms == forms


# --- parse_filings --------------------------------------------------------------------------


def test_parse_recent_block() -> None:
    filings = parse_filings(AAPL.cik, RECENT, source="recent")
    assert len(filings) == 12
    first = filings[0]
    assert first == Filing(
        cik="0000320193",
        form="4",
        filing_date=date(2026, 2, 5),
        report_date=date(2026, 2, 3),
        accession_number="0000320193-26-000012",
        primary_document="xslF345X06/form4.xml",
    )
    ten_k = next(f for f in filings if f.form == "10-K")
    assert ten_k.filing_date == date(2026, 1, 30)
    assert ten_k.report_date == date(2025, 9, 27)


def test_parse_empty_report_and_primary_document_become_none() -> None:
    filings = parse_filings(AAPL.cik, RECENT, source="recent")
    def14a = next(f for f in filings if f.form == "DEF 14A")
    assert def14a.report_date is None
    assert [f for f in filings if f.primary_document is None][0].filing_date == date(2025, 10, 15)
    assert any(f.primary_document == "../evil.htm" for f in filings)  # raw until v1.04


def test_parse_history_file_top_level_arrays() -> None:
    filings = parse_filings(AAPL.cik, HISTORY, source=HIST_NAME)
    assert len(filings) == 6
    assert filings[0].filing_date == date(2015, 10, 2)
    assert filings[-1].filing_date == date(2004, 2, 10)


def test_parse_unequal_lengths_names_both_counts() -> None:
    block = copy.deepcopy(RECENT)
    block["form"] = block["form"][:-1]
    with pytest.raises(SubmissionsError, match="recent: array lengths differ") as excinfo:
        parse_filings(AAPL.cik, block, source="recent")
    assert "accessionNumber=12" in str(excinfo.value) and "form=11" in str(excinfo.value)


def test_parse_missing_array() -> None:
    block = {k: v for k, v in RECENT.items() if k != "form"}
    with pytest.raises(SubmissionsError, match="missing or non-list array 'form'"):
        parse_filings(AAPL.cik, block, source="recent")


def test_parse_malformed_filing_date_names_entry() -> None:
    block = copy.deepcopy(RECENT)
    block["filingDate"][3] = "2026-13-01"
    with pytest.raises(SubmissionsError, match="entry 3 has a malformed filingDate"):
        parse_filings(AAPL.cik, block, source="recent")


def test_parse_empty_filing_date_and_accession() -> None:
    block = copy.deepcopy(RECENT)
    block["filingDate"][0] = ""
    with pytest.raises(SubmissionsError, match="entry 0 has an empty filingDate"):
        parse_filings(AAPL.cik, block, source="recent")
    block = copy.deepcopy(RECENT)
    block["accessionNumber"][5] = ""
    with pytest.raises(SubmissionsError, match="entry 5 has an empty accessionNumber"):
        parse_filings(AAPL.cik, block, source="recent")


def test_parse_non_object_block() -> None:
    with pytest.raises(SubmissionsError, match="not an object"):
        parse_filings(AAPL.cik, [], source="x")  # type: ignore[arg-type]


# --- history_files_for_year -----------------------------------------------------------------


@pytest.mark.parametrize("year", [2005, 2015, 2004, 2014])
def test_history_selected_for_overlapping_years(year: int) -> None:
    assert history_files_for_year(FILES, year, source="s") == [HIST_NAME]


@pytest.mark.parametrize("year", [2016, 2003, 2020, 2026, 1993])
def test_history_not_selected_outside_range(year: int) -> None:
    assert history_files_for_year(FILES, year, source="s") == []


def test_history_files_sorted_newest_first_and_only_overlapping() -> None:
    files = [
        {"name": "a.json", "filingFrom": "2000-01-05", "filingTo": "2009-12-30"},
        {"name": "b.json", "filingFrom": "2010-01-02", "filingTo": "2015-06-30"},
        {"name": "c.json", "filingFrom": "2015-07-01", "filingTo": "2019-12-31"},
    ]
    assert history_files_for_year(files, 2010, source="s") == ["b.json"]
    assert history_files_for_year(files, 2009, source="s") == ["a.json"]
    assert history_files_for_year(files, 2015, source="s") == ["c.json", "b.json"]
    assert history_files_for_year(files, 2020, source="s") == []


def test_history_boundary_day_counts_as_overlap() -> None:
    files = [{"name": "edge.json", "filingFrom": "2025-12-31", "filingTo": "2026-03-01"}]
    assert history_files_for_year(files, 2025, source="s") == ["edge.json"]
    files = [{"name": "edge.json", "filingFrom": "2020-01-01", "filingTo": "2025-01-01"}]
    assert history_files_for_year(files, 2025, source="s") == ["edge.json"]


def test_history_none_and_malformed() -> None:
    assert history_files_for_year(None, 2005, source="s") == []
    with pytest.raises(SubmissionsError, match="files\\[0\\] lacks filingFrom/filingTo"):
        history_files_for_year([{"name": "x.json", "filingFrom": "2000-01-01"}], 2005, source="s")
    with pytest.raises(SubmissionsError, match="files\\[0\\] has no name"):
        history_files_for_year(
            [{"filingFrom": "2000-01-01", "filingTo": "2001-01-01"}], 2000, source="s"
        )
    with pytest.raises(SubmissionsError, match="'files' is not a list"):
        history_files_for_year({"name": "x"}, 2000, source="s")


# --- filter / sort / paginate ---------------------------------------------------------------


RECENT_FILINGS = parse_filings(AAPL.cik, RECENT, source="recent")


def test_filter_by_year_and_forms() -> None:
    assert len(filter_filings(RECENT_FILINGS, year=2026, forms=None)) == 4
    assert len(filter_filings(RECENT_FILINGS, year=2025, forms=("4",))) == 5
    assert len(filter_filings(RECENT_FILINGS, year=None, forms=("10-Q",))) == 2


def test_filter_exact_form_excludes_amendments() -> None:
    amended = Filing(AAPL.cik, "10-K/A", date(2026, 3, 1), None, "0000320193-26-000099", None)
    assert filter_filings([amended], year=2026, forms=("10-K",)) == []
    assert filter_filings([amended], year=2026, forms=("10-K", "10-K/A")) == [amended]


def test_sort_newest_first_with_accession_tiebreak_and_dedupe() -> None:
    a = Filing(AAPL.cik, "4", date(2026, 2, 5), None, "0000320193-26-000001", None)
    b = Filing(AAPL.cik, "4", date(2026, 2, 5), None, "0000320193-26-000002", None)
    c = Filing(AAPL.cik, "8-K", date(2026, 1, 1), None, "0000320193-26-000000", None)
    dup = Filing(AAPL.cik, "4 (dup)", date(2026, 2, 5), None, "0000320193-26-000002", None)
    assert sort_filings([c, a, b, dup]) == [b, a, c]


def test_paginate_boundaries() -> None:
    items = sort_filings(filter_filings(RECENT_FILINGS, year=2025, forms=None))
    assert len(items) == 8
    page, nxt = paginate(items, offset=0, limit=3)
    assert len(page) == 3 and nxt == 3
    page, nxt = paginate(items, offset=6, limit=3)
    assert len(page) == 2 and nxt is None
    assert paginate(items, offset=8, limit=3) == ((), None)
    assert paginate(items, offset=50, limit=3) == ((), None)


# --- service: year semantics ----------------------------------------------------------------


def test_year_in_recent_block_costs_one_request(make_client: ClientFactory) -> None:
    service, recorded, _ = make_service(make_client)
    page = service.list_filings_by_year(AAPL, 2026)
    assert page.total_count == 4
    assert dates(page) == ["2026-02-05", "2026-02-02", "2026-01-30", "2026-01-29"]
    assert urls(recorded) == [SUBS_URL]
    assert page.next_offset is None
    assert page.forms is None and page.limit == 100 and page.offset == 0


def test_fiscal_year_ten_k_belongs_to_filing_year(make_client: ClientFactory) -> None:
    service, _, _ = make_service(make_client)
    in_2026 = service.list_filings_by_year(AAPL, 2026, forms=["10-K"])
    in_2025 = service.list_filings_by_year(AAPL, 2025, forms=["10-K"])
    assert [str(f.filing_date) for f in in_2026.filings] == ["2026-01-30"]
    assert in_2026.filings[0].report_date == date(2025, 9, 27)
    assert in_2025.total_count == 0 and in_2025.filings == ()


def test_year_2025_newest_first(make_client: ClientFactory) -> None:
    service, _, _ = make_service(make_client)
    page = service.list_filings_by_year(AAPL, 2025)
    assert page.total_count == 8
    assert dates(page) == sorted(dates(page), reverse=True)
    assert dates(page)[0] == "2025-11-20" and dates(page)[-1] == "2025-01-10"


# --- service: forms -------------------------------------------------------------------------


def test_forms_filter_is_case_insensitive_exact(make_client: ClientFactory) -> None:
    service, _, _ = make_service(make_client)
    assert dates(service.list_filings_by_year(AAPL, 2025, forms=["10-q"])) == ["2025-08-01"]
    assert service.list_filings_by_year(AAPL, 2025, forms=["4"]).total_count == 5
    both = service.list_filings_by_year(AAPL, 2026, forms=["10-K", "10-Q"])
    assert both.total_count == 2 and both.forms == ("10-K", "10-Q")


def test_empty_forms_list_means_no_filter(make_client: ClientFactory) -> None:
    service, _, _ = make_service(make_client)
    assert service.list_filings_by_year(AAPL, 2026, forms=[]).total_count == 4


def test_bare_string_forms_rejected_before_request(make_client: ClientFactory) -> None:
    service, recorded, _ = make_service(make_client)
    with pytest.raises(InvalidForms):
        service.list_filings_by_year(AAPL, 2026, forms="10-K")
    assert recorded.requests == []


# --- service: history -----------------------------------------------------------------------


def test_historical_year_served_from_history_file(make_client: ClientFactory) -> None:
    service, recorded, _ = make_service(make_client)
    page = service.list_filings_by_year(AAPL, 2005)
    assert page.total_count == 2
    assert [(f.form, str(f.filing_date)) for f in page.filings] == [
        ("10-K", "2005-12-01"),
        ("8-K", "2005-10-11"),
    ]
    assert urls(recorded) == [SUBS_URL, HIST_URL]


def test_boundary_year_2015_fetches_history(make_client: ClientFactory) -> None:
    service, recorded, _ = make_service(make_client)
    page = service.list_filings_by_year(AAPL, 2015)
    assert dates(page) == ["2015-10-02"]
    assert HIST_URL in urls(recorded)


@pytest.mark.parametrize("year", [2020, 2030, 1993])
def test_empty_years_make_no_history_request(make_client: ClientFactory, year: int) -> None:
    service, recorded, _ = make_service(make_client)
    page = service.list_filings_by_year(AAPL, year)
    assert page.total_count == 0 and page.filings == () and page.next_offset is None
    assert urls(recorded) == [SUBS_URL]


def test_duplicate_accession_across_sources_counted_once(make_client: ClientFactory) -> None:
    subs = copy.deepcopy(SUBMISSIONS)
    recent = subs["filings"]["recent"]
    recent["filingDate"][11] = "2005-12-01"
    recent["accessionNumber"][11] = "0001104659-05-058421"  # the history 10-K
    recent["form"][11] = "10-K"
    service, _, _ = make_service(make_client, {SUBS_URL: [ok(subs)], HIST_URL: [ok(HISTORY)]})
    page = service.list_filings_by_year(AAPL, 2005)
    assert page.total_count == 2
    assert len({f.accession_number for f in page.filings}) == 2


# --- service: pagination --------------------------------------------------------------------


def test_pages_advance_without_duplicates_and_one_request(make_client: ClientFactory) -> None:
    service, recorded, _ = make_service(make_client)
    pages = [service.list_filings_by_year(AAPL, 2025, limit=3, offset=o) for o in (0, 3, 6)]
    assert [len(p.filings) for p in pages] == [3, 3, 2]
    assert [p.next_offset for p in pages] == [3, 6, None]
    assert all(p.total_count == 8 and p.limit == 3 for p in pages)
    seen = [f.accession_number for p in pages for f in p.filings]
    assert len(seen) == 8 == len(set(seen))
    assert urls(recorded) == [SUBS_URL]


def test_offset_at_or_past_end_is_empty(make_client: ClientFactory) -> None:
    service, _, _ = make_service(make_client)
    for offset in (8, 100):
        page = service.list_filings_by_year(AAPL, 2025, offset=offset)
        assert page.filings == () and page.next_offset is None and page.total_count == 8


@pytest.mark.parametrize(
    ("limit", "offset"), [(0, 0), (-1, 0), (100, -1), ("5", 0), (True, 0), (5, 2.0)]
)
def test_invalid_pagination_rejected_before_request(
    make_client: ClientFactory, limit: Any, offset: Any
) -> None:
    service, recorded, _ = make_service(make_client)
    with pytest.raises(InvalidPagination):
        service.list_filings_by_year(AAPL, 2025, limit=limit, offset=offset)
    assert recorded.requests == []


def test_limit_is_clamped_to_page_cap(make_client: ClientFactory) -> None:
    service, _, _ = make_service(make_client)
    page = service.list_filings_by_year(AAPL, 2025, limit=500)
    assert page.limit == 100 and len(page.filings) == 8


def test_page_cap_setting_drives_default_limit(make_client: ClientFactory) -> None:
    service, _, _ = make_service(make_client, env={"MCP_EDGAR_PAGE_CAP": "5"})
    page = service.list_filings_by_year(AAPL, 2025)
    assert page.limit == 5 and len(page.filings) == 5 and page.next_offset == 5


# --- validators (public since req_002 v2.02 so tools can reject paging before any request) ---


@pytest.mark.parametrize(("limit", "expected"), [(None, 100), (3, 3), (100, 100), (1000, 100)])
def test_validate_limit_defaults_and_clamps(limit: Any, expected: int) -> None:
    assert validate_limit(limit, page_cap=100) == expected


@pytest.mark.parametrize("limit", [0, -1, True, "3", 2.0])
def test_validate_limit_rejects(limit: Any) -> None:
    with pytest.raises(InvalidPagination, match="^limit must be an integer >= 1, got "):
        validate_limit(limit, page_cap=100)


@pytest.mark.parametrize(("offset", "expected"), [(0, 0), (7, 7)])
def test_validate_offset_accepts(offset: int, expected: int) -> None:
    assert validate_offset(offset) == expected


@pytest.mark.parametrize("offset", [-1, True, "0", 1.0])
def test_validate_offset_rejects(offset: Any) -> None:
    with pytest.raises(InvalidPagination, match="^offset must be an integer >= 0, got "):
        validate_offset(offset)


# --- service: errors ------------------------------------------------------------------------


def test_invalid_year_before_request(make_client: ClientFactory) -> None:
    service, recorded, _ = make_service(make_client)
    with pytest.raises(InvalidYear):
        service.list_filings_by_year(AAPL, 1850)
    assert recorded.requests == []


def test_sec_unavailable_propagates(make_client: ClientFactory) -> None:
    service, _, _ = make_service(make_client, {SUBS_URL: [status(429)]})
    with pytest.raises(SecUnavailable):
        service.list_filings_by_year(AAPL, 2026)


def test_history_outage_is_not_an_empty_page(make_client: ClientFactory) -> None:
    service, _, _ = make_service(
        make_client, {SUBS_URL: [ok(SUBMISSIONS)], HIST_URL: [status(503)]}
    )
    with pytest.raises(SecUnavailable):
        service.list_filings_by_year(AAPL, 2005)


def test_payload_without_filings_is_submissions_error(make_client: ClientFactory) -> None:
    service, _, _ = make_service(make_client, {SUBS_URL: [ok({"cik": "0000320193"})]})
    with pytest.raises(SubmissionsError, match="CIK0000320193.json: payload lacks filings.recent"):
        service.list_filings_by_year(AAPL, 2026)


def test_unsafe_history_name_is_submissions_error(make_client: ClientFactory) -> None:
    subs = copy.deepcopy(SUBMISSIONS)
    subs["filings"]["files"][0]["name"] = "../x.json"
    service, recorded, _ = make_service(make_client, {SUBS_URL: [ok(subs)]})
    with pytest.raises(SubmissionsError, match="not a safe file name"):
        service.list_filings_by_year(AAPL, 2005)
    assert urls(recorded) == [SUBS_URL]


# --- latest_filing --------------------------------------------------------------------------


def test_latest_without_forms(make_client: ClientFactory) -> None:
    service, recorded, _ = make_service(make_client)
    latest = service.latest_filing(AAPL)
    assert latest is not None
    assert (latest.form, latest.filing_date) == ("4", date(2026, 2, 5))
    assert urls(recorded) == [SUBS_URL]


def test_latest_with_forms(make_client: ClientFactory) -> None:
    service, _, _ = make_service(make_client)
    assert service.latest_filing(AAPL, forms=["10-K"]).filing_date == date(2026, 1, 30)  # type: ignore[union-attr]
    assert service.latest_filing(AAPL, forms=["10-q"]).filing_date == date(2026, 2, 2)  # type: ignore[union-attr]


def test_latest_walks_history_for_form_missing_from_recent(make_client: ClientFactory) -> None:
    history = copy.deepcopy(HISTORY)
    history["form"][3] = "S-8"  # 2005-10-11
    service, recorded, _ = make_service(
        make_client, {SUBS_URL: [ok(SUBMISSIONS)], HIST_URL: [ok(history)]}
    )
    latest = service.latest_filing(AAPL, forms=["s-8"])
    assert latest is not None
    assert (latest.form, latest.filing_date) == ("S-8", date(2005, 10, 11))
    assert urls(recorded) == [SUBS_URL, HIST_URL]


def test_latest_returns_none_after_history_walk(make_client: ClientFactory) -> None:
    service, recorded, _ = make_service(make_client)
    assert service.latest_filing(AAPL, forms=["XYZ"]) is None
    assert urls(recorded) == [SUBS_URL, HIST_URL]


def test_latest_empty_recent_without_forms_does_not_walk_history(
    make_client: ClientFactory,
) -> None:
    subs = copy.deepcopy(SUBMISSIONS)
    subs["filings"]["recent"] = {name: [] for name in subs["filings"]["recent"]}
    service, recorded, _ = make_service(
        make_client, {SUBS_URL: [ok(subs)], HIST_URL: [ok(HISTORY)]}
    )
    assert service.latest_filing(AAPL) is None
    assert urls(recorded) == [SUBS_URL]


def test_latest_rejects_bare_string_forms(make_client: ClientFactory) -> None:
    service, recorded, _ = make_service(make_client)
    with pytest.raises(InvalidForms):
        service.latest_filing(AAPL, forms="10-K")
    assert recorded.requests == []


# --- FilingsPage ----------------------------------------------------------------------------


def test_filings_page_next_offset_and_frozen() -> None:
    page = FilingsPage(AAPL, 2025, None, 10, 4, 3, ())
    assert page.next_offset is None  # empty page never points further
    page = FilingsPage(AAPL, 2025, ("4",), 10, 4, 3, tuple(RECENT_FILINGS[:3]))
    assert page.next_offset == 7
    with pytest.raises(AttributeError):
        page.year = 2026  # type: ignore[misc]


# --- live -----------------------------------------------------------------------------------


@pytest.mark.live
def test_live_historical_year_from_history_file() -> None:
    settings = Settings.from_env({})
    try:
        with SecClient(settings) as client:
            page = FilingsService(client, settings).list_filings_by_year(AAPL, 2005, limit=5)
    except (SecUnavailable, httpx.TransportError) as exc:
        pytest.skip(f"SEC unreachable: {exc}")
    assert page.total_count > 0
    assert 0 < len(page.filings) <= 5
    assert all(f.filing_date.year == 2005 for f in page.filings)
