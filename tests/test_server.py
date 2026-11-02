"""Tests for mcp_edgar.server: tool registration, get_latest_filing outcomes, SDK path, main()."""

import asyncio
import copy
import inspect
import json
import logging
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx
import pytest
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
from mcp.types import CallToolResult, Tool

from mcp_edgar import server
from mcp_edgar.edgar_urls import ARCHIVES_BASE_URL
from mcp_edgar.sec_client import TICKER_MAP_URL, SecClient
from mcp_edgar.server import Services, get_latest_filing, list_filings_by_year, list_form_types
from mcp_edgar.settings import Settings, reset_settings_cache
from tests.conftest import ClientFactory, FakeClock, Recorded, fixture_json

SUBS_URL = "https://data.sec.gov/submissions/CIK0000320193.json"
HIST_URL = "https://data.sec.gov/submissions/CIK0000320193-submissions-001.json"
TICKER_MAP = fixture_json("company_tickers_exchange.json")
SUBMISSIONS = fixture_json("submissions_CIK0000320193.json")
HISTORY = fixture_json("CIK0000320193-submissions-001.json")

AAPL_COMPANY = {"cik": "0000320193", "name": "Apple Inc.", "ticker": "AAPL", "exchange": "Nasdaq"}
AAPL_DIR = f"{ARCHIVES_BASE_URL}320193/"
FORM4_RECORD = {
    "form": "4",
    "filing_date": "2026-02-05",
    "report_date": "2026-02-03",
    "accession_number": "0000320193-26-000012",
    "filing_url": f"{AAPL_DIR}000032019326000012/0000320193-26-000012-index.htm",
    "primary_document_url": f"{AAPL_DIR}000032019326000012/xslF345X06/form4.xml",
}
TEN_K_RECORD = {
    "form": "10-K",
    "filing_date": "2026-01-30",
    "report_date": "2025-09-27",
    "accession_number": "0000320193-26-000008",
    "filing_url": f"{AAPL_DIR}000032019326000008/0000320193-26-000008-index.htm",
    "primary_document_url": f"{AAPL_DIR}000032019326000008/aapl-20250927.htm",
}
DUAL_CANDIDATES = [
    {
        "cik": "0001000001",
        "name": "Dual Listed Holdings Inc.",
        "ticker": "DUAL",
        "exchange": "NYSE",
    },
    {"cik": "0001000002", "name": "Dual Energy Trust", "ticker": "DUAL", "exchange": "CBOE"},
]
FORM4_2025_RECORD = {
    "form": "4",
    "filing_date": "2025-11-20",
    "report_date": "2025-11-18",
    "accession_number": "0000320193-25-000120",
    "filing_url": f"{AAPL_DIR}000032019325000120/0000320193-25-000120-index.htm",
    "primary_document_url": f"{AAPL_DIR}000032019325000120/xslF345X06/form4.xml",
}
DATES_2025 = [
    "2025-11-20",
    "2025-10-15",
    "2025-08-01",
    "2025-06-10",
    "2025-05-05",
    "2025-02-14",
    "2025-01-22",
    "2025-01-10",
]
OK_KEYS = ["status", "company", "forms", "filing", "message"]
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
UPSTREAM_KEYS = ["status", "retryable", "status_code", "retry_after_seconds", "message"]
DOCSTRING_TOKENS = (
    "filing_date",
    "10-K/A",
    "not_found",
    "ambiguous",
    "invalid_input",
    "upstream_error",
    "cik",
)
LIST_DOCSTRING_TOKENS = DOCSTRING_TOKENS + ("report_date", "1993", "total_count", "next_offset")
TOOL_NAMES = ["get_latest_filing", "list_filings_by_year", "list_form_types"]
FILING_TOOLS = TOOL_NAMES[:2]  # the tools that take a ticker (and therefore cik)
FORM_TYPES_TOKENS = ("forms", "generated_on", "/A", "invalid_input")
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
INSTRUCTION_TOKENS = (
    "filing_date",
    "next_offset",
    "cik",
    "get_latest_filing",
    "list_filings_by_year",
    "list_form_types",
    "Resources:",
    "edgar://companies/{ticker}/latest",
    "edgar://issuers/{cik}",
    "edgar://form-types",
    "Prompts:",
    "review_latest_filing",
    "review_filings_by_year",
    "explain_form_types",
)

Routes = dict[str, list[httpx.Response]]
ServicesFactory = Callable[..., tuple[Services, Recorded, FakeClock]]


def ok(payload: Any, **headers: str) -> httpx.Response:
    return httpx.Response(200, json=payload, headers=headers or None)


def status(code: int, headers: Mapping[str, str] | None = None) -> httpx.Response:
    return httpx.Response(code, headers=headers)


def default_routes() -> Routes:
    return {TICKER_MAP_URL: [ok(TICKER_MAP)], SUBS_URL: [ok(SUBMISSIONS)], HIST_URL: [ok(HISTORY)]}


def urls(recorded: Recorded) -> list[str]:
    return [str(r.url) for r in recorded.requests]


def the_tool(name: str) -> Tool:
    tools = asyncio.run(server.mcp.list_tools())
    assert [t.name for t in tools] == TOOL_NAMES  # registration (file) order
    return next(t for t in tools if t.name == name)


def call_tool(arguments: dict[str, Any], name: str = "get_latest_filing") -> CallToolResult:
    result = asyncio.run(server.mcp.call_tool(name, arguments))
    assert isinstance(result, CallToolResult)
    return result


def call_list(arguments: dict[str, Any]) -> CallToolResult:
    return call_tool(arguments, name="list_filings_by_year")


def broken_submissions() -> Any:
    payload = copy.deepcopy(SUBMISSIONS)
    payload["filings"]["recent"]["accessionNumber"][2] = "bad"  # the 10-K entry
    return payload


@pytest.fixture
def fake_services(make_client: ClientFactory, monkeypatch: pytest.MonkeyPatch) -> ServicesFactory:
    """Factory: ``fake_services(routes, env)`` wires fixture-backed services into the tool."""

    def factory(
        routes: Routes | None = None, env: Mapping[str, str] | None = None
    ) -> tuple[Services, Recorded, FakeClock]:
        client, recorded, clock = make_client(
            routes if routes is not None else default_routes(), env=env
        )
        services = Services.build(Settings.from_env(dict(env or {})), client=client)
        monkeypatch.setattr(server, "get_services", lambda: services)
        return services, recorded, clock

    return factory


# --- registration and contract --------------------------------------------------------------


def test_server_instance_and_entry() -> None:
    assert isinstance(server.mcp, MCPServer)
    assert server.mcp.name == "SEC EDGAR Filings"
    assert callable(server.main)


def test_tool_title_and_read_only_annotations() -> None:
    tool = the_tool("get_latest_filing")
    assert tool.title == "Latest SEC filing"
    assert tool.annotations is not None
    assert tool.annotations.read_only_hint is True
    assert tool.annotations.idempotent_hint is True
    assert tool.annotations.open_world_hint is True


def test_input_schema_from_signature() -> None:
    schema = the_tool("get_latest_filing").input_schema
    assert schema["required"] == ["ticker"]
    assert schema["properties"]["ticker"]["type"] == "string"
    forms = schema["properties"]["forms"]
    assert {"items": {"type": "string"}, "type": "array"} in forms["anyOf"]
    assert {"type": "null"} in forms["anyOf"]
    assert forms["default"] is None


def test_output_schema_is_a_permissive_object() -> None:
    schema = the_tool("get_latest_filing").output_schema
    assert schema is not None
    assert schema["type"] == "object"
    assert schema.get("additionalProperties") is True


@pytest.mark.parametrize("token", DOCSTRING_TOKENS)
def test_description_states_the_contract(token: str) -> None:
    assert token in the_tool("get_latest_filing").description


@pytest.mark.parametrize("name", TOOL_NAMES)
def test_description_is_the_dedented_docstring(name: str) -> None:
    description = the_tool(name).description
    fn = getattr(server, name)
    assert description == inspect.getdoc(fn)
    lines = description.splitlines()
    assert lines[0].endswith(".") and not lines[0].startswith(" ")
    assert lines[1] == "" and not lines[2].startswith(" ")  # paragraph at column 0


@pytest.mark.parametrize("name", FILING_TOOLS)
def test_cik_is_the_last_nullable_argument(name: str) -> None:
    schema = the_tool(name).input_schema
    properties = schema["properties"]
    assert list(properties)[-1] == "cik"
    assert {"type": "string"} in properties["cik"]["anyOf"]
    assert {"type": "integer"} in properties["cik"]["anyOf"]  # clients send CIKs as numbers too
    assert {"type": "null"} in properties["cik"]["anyOf"]
    assert properties["cik"]["default"] is None
    assert "cik" not in schema["required"]


def test_server_instructions_guide_clients() -> None:
    assert server.mcp.instructions == server.SERVER_INSTRUCTIONS
    for token in INSTRUCTION_TOKENS:
        assert token in server.SERVER_INSTRUCTIONS
    assert len(server.SERVER_INSTRUCTIONS) < 1000  # v6.03: names resources and prompts too


def test_resources_and_prompts_are_registered_alongside_the_tools() -> None:
    """req_006 v6.01/v6.02: one static resource, four templates, three prompts; tools unchanged."""
    assert len(asyncio.run(server.mcp.list_resources())) == 1
    assert len(asyncio.run(server.mcp.list_resource_templates())) == 4
    assert len(asyncio.run(server.mcp.list_prompts())) == 3
    assert [t.name for t in asyncio.run(server.mcp.list_tools())] == TOOL_NAMES


# --- direct calls: ok outcomes --------------------------------------------------------------


def test_default_call_returns_newest_filing(fake_services: ServicesFactory) -> None:
    _, recorded, _ = fake_services()
    outcome = get_latest_filing("AAPL")
    assert list(outcome) == OK_KEYS
    assert outcome["status"] == "ok"
    assert outcome["company"] == AAPL_COMPANY
    assert outcome["forms"] is None
    assert outcome["filing"] == FORM4_RECORD
    assert outcome["message"] == "Newest filing by Apple Inc. (AAPL): 4 filed 2026-02-05."
    assert urls(recorded) == [TICKER_MAP_URL, SUBS_URL]


def test_forms_filter_returns_newest_ten_k(fake_services: ServicesFactory) -> None:
    fake_services()
    outcome = get_latest_filing("AAPL", ["10-K"])
    assert outcome["status"] == "ok"
    assert outcome["forms"] == ["10-K"]
    assert outcome["filing"] == TEN_K_RECORD
    assert outcome["message"] == "Newest 10-K by Apple Inc. (AAPL): filed 2026-01-30."


def test_forms_are_normalised(fake_services: ServicesFactory) -> None:
    fake_services()
    outcome = get_latest_filing("aapl", ["10-k", " 10-q "])
    assert outcome["forms"] == ["10-K", "10-Q"]
    assert outcome["filing"]["accession_number"] == "0000320193-26-000010"
    assert outcome["filing"]["filing_date"] == "2026-02-02"
    assert outcome["filing"]["primary_document_url"].endswith("/aapl-20251227.htm")


def test_empty_forms_means_no_filter(fake_services: ServicesFactory) -> None:
    fake_services()
    assert get_latest_filing("AAPL", []) == get_latest_filing("AAPL")


def test_record_keeps_null_report_date(fake_services: ServicesFactory) -> None:
    fake_services()
    filing = get_latest_filing("AAPL", ["DEF 14A"])["filing"]
    assert filing["form"] == "DEF 14A"
    assert filing["report_date"] is None
    assert filing["filing_url"].endswith("/0001308179-25-000010-index.htm")


def test_no_matching_form_is_ok_with_null_filing(fake_services: ServicesFactory) -> None:
    _, recorded, _ = fake_services()
    outcome = get_latest_filing("AAPL", ["10-ZZ"])
    assert outcome["status"] == "ok"
    assert outcome["company"] == AAPL_COMPANY
    assert outcome["forms"] == ["10-ZZ"]
    assert outcome["filing"] is None
    assert outcome["message"] == "Apple Inc. (AAPL) has no filings of form 10-ZZ on EDGAR."
    assert urls(recorded).count(HIST_URL) == 1  # the history walk, once


# --- direct calls: structured non-ok outcomes -----------------------------------------------


def test_unknown_ticker_is_not_found(fake_services: ServicesFactory) -> None:
    _, recorded, _ = fake_services()
    outcome = get_latest_filing("zzzzzz")
    assert list(outcome) == ["status", "ticker", "message"]
    assert outcome["status"] == "not_found"
    assert outcome["ticker"] == "zzzzzz"
    assert "zzzzzz" in outcome["message"]
    assert urls(recorded) == [TICKER_MAP_URL]


def test_ambiguous_ticker_lists_candidates(fake_services: ServicesFactory) -> None:
    _, recorded, _ = fake_services()
    outcome = get_latest_filing("dual")
    assert list(outcome) == ["status", "ticker", "candidates", "message"]
    assert outcome["status"] == "ambiguous"
    assert outcome["ticker"] == "dual"
    assert outcome["candidates"] == DUAL_CANDIDATES
    assert "choose one" in outcome["message"]
    assert all("data.sec.gov" not in url for url in urls(recorded))


@pytest.mark.parametrize("ticker", ["AA PL", "", "A" * 11, "-X"])
def test_invalid_ticker_costs_no_request(fake_services: ServicesFactory, ticker: str) -> None:
    _, recorded, _ = fake_services()
    outcome = get_latest_filing(ticker)
    assert outcome["status"] == "invalid_input"
    assert outcome["field"] == "ticker"
    assert list(outcome) == ["status", "field", "message"]
    assert recorded.requests == []


@pytest.mark.parametrize("forms", [["", "10-K"], [" "], "10-K"])
def test_invalid_forms_costs_no_request(fake_services: ServicesFactory, forms: Any) -> None:
    _, recorded, _ = fake_services()
    outcome = get_latest_filing("AAPL", forms)
    assert outcome["status"] == "invalid_input"
    assert outcome["field"] == "forms"
    assert recorded.requests == []


def test_ticker_map_429_is_retryable_upstream_error(fake_services: ServicesFactory) -> None:
    _, _, clock = fake_services({TICKER_MAP_URL: [status(429, {"Retry-After": "2"})]})
    outcome = get_latest_filing("AAPL")
    assert list(outcome) == UPSTREAM_KEYS
    assert outcome["status"] == "upstream_error"
    assert outcome["retryable"] is True
    assert outcome["status_code"] == 429
    assert outcome["retry_after_seconds"] == 2.0
    assert clock.sleeps == [2.0, 2.0, 2.0]


def test_submissions_503_is_retryable(fake_services: ServicesFactory) -> None:
    _, _, clock = fake_services({TICKER_MAP_URL: [ok(TICKER_MAP)], SUBS_URL: [status(503)]})
    outcome = get_latest_filing("AAPL")
    assert outcome["status"] == "upstream_error"
    assert outcome["retryable"] is True
    assert outcome["status_code"] == 503
    assert outcome["retry_after_seconds"] is None
    assert clock.sleeps == [0.5, 1.0, 2.0]


def test_missing_submissions_file_is_not_retryable(fake_services: ServicesFactory) -> None:
    fake_services()  # BRK-B resolves to CIK 0001067983, which has no route -> 404
    outcome = get_latest_filing("brk.b")
    assert outcome["status"] == "upstream_error"
    assert outcome["retryable"] is False
    assert outcome["status_code"] == 404
    assert "CIK0001067983" in outcome["message"]


def test_non_json_submissions_is_not_retryable(fake_services: ServicesFactory) -> None:
    routes = {TICKER_MAP_URL: [ok(TICKER_MAP)], SUBS_URL: [httpx.Response(200, content=b"<html>")]}
    fake_services(routes)
    outcome = get_latest_filing("AAPL")
    assert outcome["status"] == "upstream_error"
    assert outcome["retryable"] is False
    assert outcome["status_code"] == 200


def test_malformed_ticker_map_is_not_retryable(fake_services: ServicesFactory) -> None:
    fake_services({TICKER_MAP_URL: [ok({"fields": ["cik"], "data": []})]})
    outcome = get_latest_filing("AAPL")
    assert outcome["status"] == "upstream_error"
    assert outcome["retryable"] is False
    assert outcome["status_code"] is None
    assert "unexpected shape" in outcome["message"]


# --- crashes propagate ----------------------------------------------------------------------


def test_malformed_accession_propagates_as_value_error(fake_services: ServicesFactory) -> None:
    fake_services({TICKER_MAP_URL: [ok(TICKER_MAP)], SUBS_URL: [ok(broken_submissions())]})
    with pytest.raises(ValueError, match="accession number"):
        get_latest_filing("AAPL", ["10-K"])


def test_malformed_accession_is_unexpected_tool_error(fake_services: ServicesFactory) -> None:
    fake_services({TICKER_MAP_URL: [ok(TICKER_MAP)], SUBS_URL: [ok(broken_submissions())]})
    with pytest.raises(UnexpectedToolError):
        call_tool({"ticker": "AAPL", "forms": ["10-K"]})


# --- the SDK path ---------------------------------------------------------------------------


def test_sdk_call_matches_direct_call(
    fake_services: ServicesFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_services()
    result = call_tool({"ticker": "AAPL"})
    assert result.is_error is False
    assert result.structured_content == get_latest_filing("AAPL")
    assert json.loads(result.content[0].text) == result.structured_content  # type: ignore[union-attr]
    assert capsys.readouterr().out == ""


def test_sdk_parses_json_string_forms(fake_services: ServicesFactory) -> None:
    fake_services()
    result = call_tool({"ticker": "AAPL", "forms": '["10-K"]'})
    assert result.is_error is False
    assert result.structured_content is not None
    assert result.structured_content["filing"] == TEN_K_RECORD


def test_sdk_missing_ticker_is_tool_error(fake_services: ServicesFactory) -> None:
    fake_services()
    with pytest.raises(ToolError) as excinfo:
        call_tool({})
    assert "ticker" in str(excinfo.value)
    assert not isinstance(excinfo.value, UnexpectedToolError)


@pytest.mark.parametrize("forms", ["10-K", [1]])
def test_sdk_rejects_wrong_forms_type(fake_services: ServicesFactory, forms: Any) -> None:
    fake_services()
    with pytest.raises(ToolError) as excinfo:
        call_tool({"ticker": "AAPL", "forms": forms})
    assert not isinstance(excinfo.value, UnexpectedToolError)


def test_direct_calls_write_nothing_to_stdout(
    fake_services: ServicesFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_services()
    get_latest_filing("AAPL")
    get_latest_filing("zzzzzz")
    get_latest_filing("AA PL")
    assert capsys.readouterr().out == ""


def test_concurrent_calls_share_services(fake_services: ServicesFactory) -> None:
    fake_services()
    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(lambda _: get_latest_filing("AAPL"), range(8)))
    assert all(outcome["status"] == "ok" for outcome in outcomes)
    assert all(outcome == outcomes[0] for outcome in outcomes)


# --- list_filings_by_year: contract ---------------------------------------------------------


def test_list_tool_title_schema_and_annotations() -> None:
    tool = the_tool("list_filings_by_year")
    assert tool.title == "Filings by year"
    assert tool.annotations is not None
    assert tool.annotations.read_only_hint is True
    assert tool.annotations.idempotent_hint is True
    assert tool.annotations.open_world_hint is True
    schema = tool.input_schema
    assert schema["required"] == ["ticker", "year"]
    assert schema["properties"]["year"]["type"] == "integer"
    assert schema["properties"]["limit"] == {"default": 100, "title": "Limit", "type": "integer"}
    assert schema["properties"]["offset"]["default"] == 0
    assert {"type": "null"} in schema["properties"]["forms"]["anyOf"]
    assert tool.output_schema is not None and tool.output_schema["type"] == "object"


@pytest.mark.parametrize("token", LIST_DOCSTRING_TOKENS)
def test_list_description_states_the_contract(token: str) -> None:
    assert token in the_tool("list_filings_by_year").description


# --- list_filings_by_year: ok outcomes ------------------------------------------------------


def test_list_year_2025_defaults(fake_services: ServicesFactory) -> None:
    _, recorded, _ = fake_services()
    outcome = list_filings_by_year("AAPL", 2025)
    assert list(outcome) == LIST_KEYS
    assert outcome["status"] == "ok"
    assert outcome["company"] == AAPL_COMPANY
    assert outcome["year"] == 2025
    assert outcome["forms"] is None
    assert (outcome["limit"], outcome["offset"]) == (100, 0)
    assert (outcome["total_count"], outcome["returned_count"], outcome["next_offset"]) == (
        8,
        8,
        None,
    )
    assert [f["filing_date"] for f in outcome["filings"]] == DATES_2025
    assert outcome["filings"][0] == FORM4_2025_RECORD
    assert outcome["filings"][1]["primary_document_url"] is None  # "" primaryDocument
    assert outcome["filings"][3]["primary_document_url"] is None  # ../evil.htm
    assert all(f["filing_url"].endswith("-index.htm") for f in outcome["filings"])
    assert outcome["message"] == (
        "Apple Inc. (AAPL): 8 filings with a filing date in 2025; showing 1-8."
    )
    assert urls(recorded) == [TICKER_MAP_URL, SUBS_URL]


def test_list_year_2026(fake_services: ServicesFactory) -> None:
    fake_services()
    outcome = list_filings_by_year("AAPL", 2026)
    assert outcome["total_count"] == 4
    assert [f["filing_date"] for f in outcome["filings"]] == [
        "2026-02-05",
        "2026-02-02",
        "2026-01-30",
        "2026-01-29",
    ]


def test_list_forms_are_normalised(fake_services: ServicesFactory) -> None:
    fake_services()
    outcome = list_filings_by_year("aapl", 2026, ["10-k", " 10-q "])
    assert outcome["forms"] == ["10-K", "10-Q"]
    assert outcome["total_count"] == 2
    assert [(f["form"], f["filing_date"]) for f in outcome["filings"]] == [
        ("10-Q", "2026-02-02"),
        ("10-K", "2026-01-30"),
    ]


def test_list_fiscal_year_ten_k_belongs_to_its_filing_year(fake_services: ServicesFactory) -> None:
    fake_services()
    in_2025 = list_filings_by_year("AAPL", 2025, ["10-K"])
    in_2026 = list_filings_by_year("AAPL", 2026, ["10-K"])
    assert (in_2025["status"], in_2025["total_count"], in_2025["filings"]) == ("ok", 0, [])
    assert in_2025["message"] == (
        "Apple Inc. (AAPL) has no filings of form 10-K with a filing date in 2025."
    )
    assert in_2026["total_count"] == 1
    assert in_2026["filings"] == [TEN_K_RECORD]
    assert in_2026["message"] == (
        "Apple Inc. (AAPL): 1 filing of form 10-K with a filing date in 2026; showing 1-1."
    )


def test_list_empty_forms_means_no_filter(fake_services: ServicesFactory) -> None:
    fake_services()
    assert list_filings_by_year("AAPL", 2025, []) == list_filings_by_year("AAPL", 2025)


def test_list_pagination_advances_without_duplicates(fake_services: ServicesFactory) -> None:
    fake_services()
    pages = [list_filings_by_year("AAPL", 2025, limit=3, offset=o) for o in (0, 3, 6)]
    assert [p["returned_count"] for p in pages] == [3, 3, 2]
    assert [p["next_offset"] for p in pages] == [3, 6, None]
    assert [p["offset"] for p in pages] == [0, 3, 6]
    assert all(p["limit"] == 3 and p["total_count"] == 8 for p in pages)
    accessions = [f["accession_number"] for p in pages for f in p["filings"]]
    assert len(accessions) == 8 and len(set(accessions)) == 8
    full = [f["accession_number"] for f in list_filings_by_year("AAPL", 2025)["filings"]]
    assert accessions == full
    assert pages[0]["message"] == (
        "Apple Inc. (AAPL): 8 filings with a filing date in 2025; showing 1-3, next offset 3."
    )
    assert pages[2]["message"].endswith("showing 7-8.")


def test_list_limit_is_clamped_and_echoed(fake_services: ServicesFactory) -> None:
    fake_services()
    outcome = list_filings_by_year("AAPL", 2025, limit=1000)
    assert (outcome["limit"], outcome["returned_count"], outcome["next_offset"]) == (100, 8, None)


def test_list_page_cap_setting_clamps_limit(fake_services: ServicesFactory) -> None:
    fake_services(env={"MCP_EDGAR_PAGE_CAP": "5"})
    outcome = list_filings_by_year("AAPL", 2025, limit=10)
    assert (outcome["limit"], outcome["returned_count"], outcome["next_offset"]) == (5, 5, 5)


def test_list_offset_past_the_end_is_an_empty_page(fake_services: ServicesFactory) -> None:
    fake_services()
    outcome = list_filings_by_year("AAPL", 2025, offset=50)
    assert (outcome["total_count"], outcome["returned_count"], outcome["next_offset"]) == (
        8,
        0,
        None,
    )
    assert outcome["filings"] == []
    assert outcome["message"].endswith("offset 50 is past the end.")


def test_list_historical_year_from_history_file(fake_services: ServicesFactory) -> None:
    _, recorded, _ = fake_services()
    outcome = list_filings_by_year("AAPL", 2005)
    assert outcome["total_count"] == 2
    assert [f["filing_date"] for f in outcome["filings"]] == ["2005-12-01", "2005-10-11"]
    assert outcome["filings"][0]["filing_url"] == (
        f"{AAPL_DIR}000110465905058421/0001104659-05-058421-index.htm"
    )
    assert urls(recorded).count(HIST_URL) == 1


def test_list_boundary_year_2015_from_history_file(fake_services: ServicesFactory) -> None:
    fake_services()
    assert list_filings_by_year("AAPL", 2015)["total_count"] == 1


@pytest.mark.parametrize("year", [2020, 2030])
def test_list_empty_year_is_ok_without_history_fetch(
    fake_services: ServicesFactory, year: int
) -> None:
    _, recorded, _ = fake_services()
    outcome = list_filings_by_year("AAPL", year)
    assert outcome["status"] == "ok"
    assert (outcome["total_count"], outcome["returned_count"], outcome["next_offset"]) == (
        0,
        0,
        None,
    )
    assert outcome["filings"] == []
    assert outcome["message"] == f"Apple Inc. (AAPL) has no filings with a filing date in {year}."
    assert HIST_URL not in urls(recorded)


# --- list_filings_by_year: structured non-ok outcomes ---------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "field"),
    [
        ({"year": 1850}, "year"),
        ({"year": 10000}, "year"),
        ({"year": 2025, "limit": 0}, "limit"),
        ({"year": 2025, "offset": -1}, "offset"),
        ({"year": 2025, "forms": "10-K"}, "forms"),
    ],
)
def test_list_invalid_input_costs_no_request(
    fake_services: ServicesFactory, kwargs: dict[str, Any], field: str
) -> None:
    _, recorded, _ = fake_services()
    outcome = list_filings_by_year("AAPL", **kwargs)
    assert outcome["status"] == "invalid_input"
    assert outcome["field"] == field
    assert list(outcome) == ["status", "field", "message"]
    assert recorded.requests == []


def test_list_invalid_ticker_costs_no_request(fake_services: ServicesFactory) -> None:
    _, recorded, _ = fake_services()
    outcome = list_filings_by_year("AA PL", 2025)
    assert (outcome["status"], outcome["field"]) == ("invalid_input", "ticker")
    assert recorded.requests == []


def test_list_not_found_and_ambiguous(fake_services: ServicesFactory) -> None:
    _, recorded, _ = fake_services()
    not_found = list_filings_by_year("zzzzzz", 2025)
    assert (not_found["status"], not_found["ticker"]) == ("not_found", "zzzzzz")
    ambiguous = list_filings_by_year("dual", 2025)
    assert ambiguous["status"] == "ambiguous"
    assert ambiguous["candidates"] == DUAL_CANDIDATES
    assert urls(recorded) == [TICKER_MAP_URL]  # second call served from cache; no data.sec.gov


def test_list_ticker_map_429_is_retryable(fake_services: ServicesFactory) -> None:
    _, _, clock = fake_services({TICKER_MAP_URL: [status(429, {"Retry-After": "2"})]})
    outcome = list_filings_by_year("AAPL", 2025)
    assert (outcome["status"], outcome["retryable"], outcome["status_code"]) == (
        "upstream_error",
        True,
        429,
    )
    assert clock.sleeps == [2.0, 2.0, 2.0]


def test_list_missing_submissions_file_is_not_retryable(fake_services: ServicesFactory) -> None:
    fake_services()
    outcome = list_filings_by_year("brk.b", 2025)
    assert (outcome["status"], outcome["retryable"], outcome["status_code"]) == (
        "upstream_error",
        False,
        404,
    )


def test_list_malformed_accession_propagates(fake_services: ServicesFactory) -> None:
    fake_services({TICKER_MAP_URL: [ok(TICKER_MAP)], SUBS_URL: [ok(broken_submissions())]})
    with pytest.raises(ValueError, match="accession number"):
        list_filings_by_year("AAPL", 2026)
    with pytest.raises(UnexpectedToolError):
        call_list({"ticker": "AAPL", "year": 2026})


# --- list_filings_by_year: the SDK path -----------------------------------------------------


def test_list_sdk_call_matches_direct_call(
    fake_services: ServicesFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_services()
    result = call_list({"ticker": "AAPL", "year": 2025})
    assert result.is_error is False
    assert result.structured_content == list_filings_by_year("AAPL", 2025)
    assert json.loads(result.content[0].text) == result.structured_content  # type: ignore[union-attr]
    assert capsys.readouterr().out == ""


def test_list_sdk_coerces_numeric_strings(fake_services: ServicesFactory) -> None:
    fake_services()
    content = call_list({"ticker": "AAPL", "year": "2025", "limit": "3"}).structured_content
    assert content is not None
    assert (content["year"], content["limit"], content["returned_count"]) == (2025, 3, 3)


def test_list_sdk_parses_json_string_forms(fake_services: ServicesFactory) -> None:
    fake_services()
    content = call_list({"ticker": "AAPL", "year": 2025, "forms": '["10-K"]'}).structured_content
    assert content is not None
    assert (content["forms"], content["total_count"]) == (["10-K"], 0)


def test_list_sdk_bool_year_is_invalid_input(fake_services: ServicesFactory) -> None:
    fake_services()  # pydantic coerces true -> 1; validate_year rejects 1
    content = call_list({"ticker": "AAPL", "year": True}).structured_content
    assert content is not None
    assert (content["status"], content["field"]) == ("invalid_input", "year")


@pytest.mark.parametrize(
    "arguments",
    [{"ticker": "AAPL"}, {"ticker": "AAPL", "year": "abc"}, {"ticker": "AAPL", "year": 2025.5}],
)
def test_list_sdk_rejects_bad_argument_types(
    fake_services: ServicesFactory, arguments: dict[str, Any]
) -> None:
    fake_services()
    with pytest.raises(ToolError) as excinfo:
        call_list(arguments)
    assert "year" in str(excinfo.value)
    assert not isinstance(excinfo.value, UnexpectedToolError)


# --- cik selection (req_002 v2.03) ---------------------------------------------------------


def test_ambiguous_message_tells_the_caller_what_to_do(fake_services: ServicesFactory) -> None:
    fake_services()
    outcome = get_latest_filing("dual")
    assert outcome["status"] == "ambiguous"
    assert outcome["message"].endswith("; call again with the chosen candidate's cik")


@pytest.mark.parametrize(
    ("cik", "expected"), [("0001000001", "CIK0001000001"), ("1000002", "CIK0001000002")]
)
def test_cik_selects_a_candidate_and_reaches_the_data_layer(
    fake_services: ServicesFactory, cik: str, expected: str
) -> None:
    fake_services()  # no submissions route for the DUAL CIKs: the 404 names the CIK that was used
    latest = get_latest_filing("dual", cik=cik)
    assert (latest["status"], latest["status_code"]) == ("upstream_error", 404)
    assert expected in latest["message"]
    listed = list_filings_by_year("dual", 2025, cik=cik)
    assert listed["status"] == "upstream_error" and expected in listed["message"]


def test_matching_cik_on_unambiguous_ticker_is_a_no_op(fake_services: ServicesFactory) -> None:
    fake_services()
    assert get_latest_filing("AAPL", cik="320193") == get_latest_filing("AAPL")
    assert list_filings_by_year("AAPL", 2025, cik="0000320193") == list_filings_by_year(
        "AAPL", 2025
    )


def test_unlisted_cik_is_invalid_input_after_one_request(fake_services: ServicesFactory) -> None:
    _, recorded, _ = fake_services()
    outcome = get_latest_filing("AAPL", cik="0000000001")
    assert (outcome["status"], outcome["field"]) == ("invalid_input", "cik")
    assert "0000320193" in outcome["message"]
    assert urls(recorded) == [TICKER_MAP_URL]


@pytest.mark.parametrize("cik", ["abc", "12345678901"])
def test_malformed_cik_costs_no_request(fake_services: ServicesFactory, cik: str) -> None:
    _, recorded, _ = fake_services()
    for outcome in (
        get_latest_filing("AAPL", cik=cik),
        list_filings_by_year("AAPL", 2025, cik=cik),
    ):
        assert (outcome["status"], outcome["field"]) == ("invalid_input", "cik")
    assert recorded.requests == []


@pytest.mark.parametrize("cik", ["320193", 320193])
def test_sdk_path_accepts_cik_as_string_or_integer(
    fake_services: ServicesFactory, cik: str | int
) -> None:
    fake_services()  # the Inspector CLI and similar clients send numeric-looking values as numbers
    result = call_tool({"ticker": "AAPL", "cik": cik})
    assert result.is_error is False
    assert result.structured_content == get_latest_filing("AAPL")


def test_sdk_path_rejects_non_scalar_cik(fake_services: ServicesFactory) -> None:
    fake_services()
    with pytest.raises(ToolError) as excinfo:
        call_tool({"ticker": "AAPL", "cik": ["320193"]})
    assert "cik" in str(excinfo.value)
    assert not isinstance(excinfo.value, UnexpectedToolError)


# --- list_form_types (req_002 v2.04) -------------------------------------------------------


def test_form_types_tool_contract() -> None:
    tool = the_tool("list_form_types")
    assert tool.title == "EDGAR form types"
    assert tool.annotations is not None and tool.annotations.read_only_hint is True
    schema = tool.input_schema
    assert schema.get("required", []) == []
    assert {"type": "string"} in schema["properties"]["query"]["anyOf"]
    assert {"type": "null"} in schema["properties"]["query"]["anyOf"]
    assert schema["properties"]["limit"]["default"] == 100
    assert schema["properties"]["offset"]["default"] == 0
    for token in FORM_TYPES_TOKENS:
        assert token in tool.description


def test_form_types_default_page_costs_no_request(fake_services: ServicesFactory) -> None:
    _, recorded, _ = fake_services()
    outcome = list_form_types()
    assert list(outcome) == FORM_KEYS
    assert outcome["status"] == "ok"
    assert outcome["query"] is None
    assert (outcome["limit"], outcome["offset"]) == (100, 0)
    assert outcome["total_count"] >= 250
    assert (outcome["returned_count"], outcome["next_offset"]) == (100, 100)
    first, second = outcome["form_types"][:2]
    assert first["filings"] >= second["filings"]
    assert set(first) == {"form", "description", "amendment", "filings", "topics"}
    assert outcome["catalogue"]["generated_on"] and len(outcome["catalogue"]["quarters"]) == 4
    assert outcome["message"].startswith("EDGAR lists ")
    assert recorded.requests == []


def test_form_types_query_filters_code_and_description(fake_services: ServicesFactory) -> None:
    fake_services()
    outcome = list_form_types("10-K")
    forms = [entry["form"] for entry in outcome["form_types"]]
    assert "10-K" in forms and "10-K/A" in forms
    assert all(
        "10-k" in entry["form"].lower() or "10-k" in (entry["description"] or "").lower()
        for entry in outcome["form_types"]
    )
    assert outcome["query"] == "10-K"
    assert "match '10-K'" in outcome["message"]


def test_form_types_no_match_is_ok_and_empty(fake_services: ServicesFactory) -> None:
    fake_services()
    outcome = list_form_types("zzzqqq")
    assert outcome["status"] == "ok"
    assert (outcome["total_count"], outcome["form_types"], outcome["next_offset"]) == (0, [], None)
    assert outcome["message"].startswith("No form types match")


def test_form_types_paging(fake_services: ServicesFactory) -> None:
    fake_services()
    outcome = list_form_types(limit=5, offset=5)
    assert (outcome["returned_count"], outcome["offset"], outcome["next_offset"]) == (5, 5, 10)
    assert outcome["limit"] == 5
    assert outcome["form_types"] == list_form_types()["form_types"][5:10]


@pytest.mark.parametrize(
    ("kwargs", "field"),
    [({"limit": 0}, "limit"), ({"offset": -1}, "offset"), ({"query": "x" * 101}, "query")],
)
def test_form_types_invalid_input_costs_no_request(
    fake_services: ServicesFactory, kwargs: dict[str, Any], field: str
) -> None:
    _, recorded, _ = fake_services()
    outcome = list_form_types(**kwargs)
    assert (outcome["status"], outcome["field"]) == ("invalid_input", field)
    assert recorded.requests == []


def test_form_types_sdk_path(
    fake_services: ServicesFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_services()
    result = call_tool({}, name="list_form_types")
    assert result.is_error is False
    assert result.structured_content == list_form_types()
    assert json.loads(result.content[0].text) == result.structured_content  # type: ignore[union-attr]
    with pytest.raises(ToolError):
        call_tool({"query": 7}, name="list_form_types")
    assert capsys.readouterr().out == ""


# --- services wiring ------------------------------------------------------------------------


def test_get_services_is_cached_and_resettable() -> None:
    server.reset_services()
    first = server.get_services()
    try:
        assert first is server.get_services()
        assert isinstance(first.client, SecClient)
        server.reset_services()
        second = server.get_services()
        assert second is not first
        second.client.close()
    finally:
        first.client.close()
        server.reset_services()


def test_services_build_uses_the_given_client(make_client: ClientFactory) -> None:
    client, _, _ = make_client(default_routes())
    settings = Settings.from_env({})
    services = Services.build(settings, client=client)
    assert services.client is client
    assert services.settings is settings
    assert services.filings is not None and services.resolver is not None


# --- main() ---------------------------------------------------------------------------------


def test_main_configures_stderr_logging(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("MCP_EDGAR_LOG_LEVEL", "DEBUG")
    reset_settings_cache()
    calls: list[str] = []
    monkeypatch.setattr(server.mcp, "run", lambda *a, **k: calls.append("run"))

    server.main()

    root = logging.getLogger()
    assert root.level == logging.DEBUG
    assert len(root.handlers) == 1
    assert isinstance(root.handlers[0], logging.StreamHandler)
    out, err = capsys.readouterr()
    assert out == ""
    assert "starting over stdio" in err
    assert calls == ["run"]


def test_main_bad_setting_exits_with_message(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("MCP_EDGAR_PAGE_CAP", "abc")
    reset_settings_cache()
    monkeypatch.setattr(server.mcp, "run", lambda *a, **k: pytest.fail("run must not be called"))

    with pytest.raises(SystemExit) as excinfo:
        server.main()

    assert "configuration error: MCP_EDGAR_PAGE_CAP:" in str(excinfo.value)
    assert capsys.readouterr().out == ""


# --- live -----------------------------------------------------------------------------------


@pytest.mark.live
def test_live_latest_filing_through_real_services() -> None:
    server.reset_services()
    try:
        outcome = get_latest_filing("AAPL")
    finally:
        server.reset_services()
    if outcome["status"] == "upstream_error":
        pytest.skip(outcome["message"])
    assert outcome["status"] == "ok"
    assert outcome["company"]["cik"] == "0000320193"
    assert outcome["filing"]["filing_url"].startswith(AAPL_DIR)


@pytest.mark.live
def test_live_list_filings_pages_through_2005() -> None:
    """AAPL 2005 (117 filings on 2026-11-01, from one history file) pages at the default cap.

    2025 is not used: live it has only 80 filings, so it fits on one page.
    """
    server.reset_services()
    try:
        first = list_filings_by_year("AAPL", 2005)
        if first["status"] == "upstream_error":
            pytest.skip(first["message"])
        second = list_filings_by_year("AAPL", 2005, offset=100)
    finally:
        server.reset_services()
    assert first["status"] == "ok" and second["status"] == "ok"
    assert first["total_count"] > 100
    assert (first["returned_count"], first["next_offset"]) == (100, 100)
    assert (second["offset"], second["next_offset"]) == (100, None)
    assert 0 < second["returned_count"] == first["total_count"] - 100
    page_one = {f["accession_number"] for f in first["filings"]}
    page_two = {f["accession_number"] for f in second["filings"]}
    assert page_one.isdisjoint(page_two)
