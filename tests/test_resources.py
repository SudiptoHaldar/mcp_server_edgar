"""Tests for mcp_edgar.resources: listing, reads (parity with the tools), CIK routes, errors.

The ``fake_services`` fixture is imported by name from ``tests.test_server`` (pytest registers
imported fixtures); ruff reads the fixture parameters as redefinitions, hence the F811 waiver.
"""

# ruff: noqa: F811

import asyncio
import copy
import json
import logging
from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import (
    ResourceError,
    ResourceNotFoundError,
    UnexpectedResourceError,
)

from mcp_edgar import server
from mcp_edgar.filings import RECENT_ARRAYS
from mcp_edgar.form_types import get_catalogue
from mcp_edgar.resources import (
    COMPANY_LATEST,
    COMPANY_PROFILE,
    FORM_TYPES,
    ISSUER_LATEST,
    ISSUER_PROFILE,
    JSON,
    TEMPLATE_URIS,
    latest_payload,
    profile_payload,
    resource_error_for,
)
from mcp_edgar.sec_client import TICKER_MAP_URL, SecNotFound, SecUnavailable
from mcp_edgar.server import get_latest_filing, list_form_types
from mcp_edgar.tickers import (
    AmbiguousTicker,
    InvalidTicker,
    Issuer,
    TickerMapError,
    UnknownTicker,
)
from tests.test_server import (  # explicit names only: `import *` would collect its tests twice
    AAPL_COMPANY,
    AAPL_DIR,
    FORM4_RECORD,
    SUBMISSIONS,
    SUBS_URL,
    TICKER_MAP,
    ServicesFactory,
    fake_services,  # noqa: F401  (pytest fixture, registered by import)
    ok,
    status,
    urls,
)

PROFILE_KEYS = ["cik", "name", "ticker", "exchange", "tickers"]
TITLES = [  # distinct from the tool titles (v6.03 surface review S1)
    "Issuer profile by ticker",
    "Latest SEC filing by ticker",
    "Issuer profile by CIK",
    "Latest SEC filing by CIK",
]
AAPL_CIK = "0000320193"


def read_raw(uri: str) -> Any:
    contents = asyncio.run(server.mcp.read_resource(uri))
    assert len(contents) == 1
    return contents[0]


def read(uri: str) -> dict[str, Any]:
    item = read_raw(uri)
    assert item.mime_type == JSON
    return json.loads(item.content)


def no_filings_submissions() -> Any:
    payload = copy.deepcopy(SUBMISSIONS)
    for name in RECENT_ARRAYS:
        payload["filings"]["recent"][name] = []
    return payload


# --- listing --------------------------------------------------------------------------------


def test_one_static_resource_is_listed() -> None:
    (resource,) = asyncio.run(server.mcp.list_resources())
    assert str(resource.uri) == FORM_TYPES
    assert resource.mime_type == JSON
    assert resource.title == "EDGAR form-type catalogue"  # the tool is titled "EDGAR form types"
    assert resource.name == "form_types_catalogue"
    description = resource.description or ""
    assert description and "\n" not in description and not description.startswith(" ")


def test_four_templates_are_listed_in_order() -> None:
    templates = asyncio.run(server.mcp.list_resource_templates())
    assert [t.uri_template for t in templates] == list(TEMPLATE_URIS)
    assert [t.title for t in templates] == TITLES
    assert [t.name for t in templates] == [
        "company_profile",
        "company_latest_filing",
        "issuer_profile",
        "issuer_latest_filing",
    ]
    for template in templates:
        assert template.mime_type == JSON
        description = template.description or ""
        assert description.endswith(".") and "\n" not in description
        assert not description.startswith(" ") and len(description) <= 100


# --- profile by ticker ----------------------------------------------------------------------


def test_profile_is_the_issuer_plus_tickers(fake_services: ServicesFactory) -> None:
    _, recorded, _ = fake_services()
    payload = read(COMPANY_PROFILE.format(ticker="AAPL"))
    assert list(payload) == PROFILE_KEYS
    assert payload == {**AAPL_COMPANY, "tickers": ["AAPL"]}
    assert urls(recorded) == [TICKER_MAP_URL]


@pytest.mark.parametrize("segment", ["aapl", "%20aapl%20", "AAPL"])
def test_profile_normalises_the_segment_like_the_tools(
    fake_services: ServicesFactory, segment: str
) -> None:
    fake_services()
    assert read(f"edgar://companies/{segment}/profile") == read(
        COMPANY_PROFILE.format(ticker="AAPL")
    )


def test_profile_resolves_the_dot_hyphen_alias(fake_services: ServicesFactory) -> None:
    fake_services()
    payload = read(COMPANY_PROFILE.format(ticker="brk.b"))
    assert payload["ticker"] == "BRK-B"
    assert payload["tickers"] == ["BRK-B"]
    assert payload["cik"] == "0001067983"


def test_profile_lists_every_ticker_of_the_cik(fake_services: ServicesFactory) -> None:
    fake_services()
    googl = read(COMPANY_PROFILE.format(ticker="GOOGL"))
    goog = read(COMPANY_PROFILE.format(ticker="GOOG"))
    assert googl["tickers"] == goog["tickers"] == ["GOOGL", "GOOG"]
    assert (googl["ticker"], goog["ticker"]) == ("GOOGL", "GOOG")
    assert googl["cik"] == goog["cik"] == "0001652044"


def test_profile_collapses_a_ticker_listed_on_two_exchanges(
    fake_services: ServicesFactory,
) -> None:
    fake_services()
    payload = read(COMPANY_PROFILE.format(ticker="BOTH"))
    assert payload["tickers"] == ["BOTH"]
    assert payload["exchange"] == "NYSE"


def test_profile_keeps_a_null_exchange(fake_services: ServicesFactory) -> None:
    fake_services()
    assert read(COMPANY_PROFILE.format(ticker="NOBD"))["exchange"] is None


def test_profile_minus_tickers_equals_the_tool_company(fake_services: ServicesFactory) -> None:
    fake_services()
    payload = read(COMPANY_PROFILE.format(ticker="AAPL"))
    del payload["tickers"]
    assert payload == get_latest_filing("AAPL")["company"]


# --- latest by ticker -----------------------------------------------------------------------


def test_latest_is_company_and_filing_only(fake_services: ServicesFactory) -> None:
    _, recorded, _ = fake_services()
    payload = read(COMPANY_LATEST.format(ticker="AAPL"))
    assert list(payload) == ["company", "filing"]
    assert payload["company"] == AAPL_COMPANY
    assert payload["filing"] == FORM4_RECORD
    assert urls(recorded) == [TICKER_MAP_URL, SUBS_URL]


def test_latest_equals_the_tool_values(fake_services: ServicesFactory) -> None:
    fake_services()
    outcome = get_latest_filing("aapl")
    assert read(COMPANY_LATEST.format(ticker="aapl")) == {
        key: outcome[key] for key in ("company", "filing")
    }


def test_second_read_is_served_from_the_client_cache(fake_services: ServicesFactory) -> None:
    _, recorded, _ = fake_services()
    read(COMPANY_LATEST.format(ticker="AAPL"))
    read(COMPANY_LATEST.format(ticker="AAPL"))
    read(COMPANY_PROFILE.format(ticker="AAPL"))
    assert urls(recorded) == [TICKER_MAP_URL, SUBS_URL]


def test_latest_is_null_when_the_issuer_has_no_filings(fake_services: ServicesFactory) -> None:
    fake_services({TICKER_MAP_URL: [ok(TICKER_MAP)], SUBS_URL: [ok(no_filings_submissions())]})
    payload = read(COMPANY_LATEST.format(ticker="AAPL"))
    assert payload == {"company": AAPL_COMPANY, "filing": None}


# --- CIK routes -----------------------------------------------------------------------------


@pytest.mark.parametrize("cik", ["320193", "0000320193", "00320193"])
def test_issuer_profile_accepts_any_cik_spelling(fake_services: ServicesFactory, cik: str) -> None:
    fake_services()
    assert read(ISSUER_PROFILE.format(cik=cik)) == read(COMPANY_PROFILE.format(ticker="AAPL"))


def test_issuer_profile_uses_the_first_listed_ticker(fake_services: ServicesFactory) -> None:
    fake_services()
    payload = read(ISSUER_PROFILE.format(cik="1652044"))
    assert payload["ticker"] == "GOOGL"
    assert payload["tickers"] == ["GOOGL", "GOOG"]
    assert read(ISSUER_PROFILE.format(cik="1234"))["ticker"] == "TINY"


def test_issuer_latest_equals_the_ticker_route(fake_services: ServicesFactory) -> None:
    fake_services()
    assert read(ISSUER_LATEST.format(cik="320193")) == read(COMPANY_LATEST.format(ticker="AAPL"))


# --- form types -----------------------------------------------------------------------------


def test_form_types_is_the_whole_catalogue(fake_services: ServicesFactory) -> None:
    _, recorded, _ = fake_services()
    item = read_raw(FORM_TYPES)
    payload = json.loads(item.content)
    assert list(payload) == ["catalogue", "form_types"]
    assert payload["catalogue"] == list_form_types()["catalogue"]
    assert payload["form_types"] == [entry.as_dict() for entry in get_catalogue().entries]
    assert len(payload["form_types"]) >= 250
    assert len(item.content) < 200_000
    assert recorded.requests == []


# --- errors ---------------------------------------------------------------------------------


def test_unknown_ticker_is_not_found(fake_services: ServicesFactory) -> None:
    fake_services()
    with pytest.raises(
        ResourceNotFoundError, match="no SEC issuer is listed under ticker 'ZZZZZZ'"
    ):
        read(COMPANY_PROFILE.format(ticker="ZZZZZZ"))


@pytest.mark.parametrize("uri", ["edgar://companies//profile", "edgar://companies/A%20B/latest"])
def test_invalid_ticker_segment_is_not_found_without_a_request(
    fake_services: ServicesFactory, uri: str
) -> None:
    _, recorded, _ = fake_services()
    with pytest.raises(ResourceNotFoundError, match="ticker"):
        read(uri)
    assert recorded.requests == []


@pytest.mark.parametrize("kind", ["profile", "latest"])
def test_ambiguous_ticker_is_a_resource_error_naming_the_cik_uris(
    fake_services: ServicesFactory, kind: str
) -> None:
    fake_services()
    with pytest.raises(ResourceError) as excinfo:
        read(f"edgar://companies/DUAL/{kind}")
    assert not isinstance(excinfo.value, ResourceNotFoundError)
    message = str(excinfo.value)
    assert message.startswith("ticker 'DUAL' is listed for 2 issuers")
    assert f"edgar://issuers/<cik>/{kind}" in message
    assert "0001000001 Dual Listed Holdings Inc. (NYSE)" in message
    assert "0001000002 Dual Energy Trust (CBOE)" in message


def test_ambiguity_text_handles_a_null_exchange() -> None:
    candidates = (
        Issuer("0000000001", "A Co", "X", None),
        Issuer("0000000002", "B Co", "X", "NYSE"),
    )
    error = resource_error_for(AmbiguousTicker("X", candidates=candidates), kind="profile")
    assert isinstance(error, ResourceError) and not isinstance(error, ResourceNotFoundError)
    assert "0000000001 A Co (no exchange)" in str(error)


@pytest.mark.parametrize(
    ("cik", "match"),
    [
        ("abc", "cik must be 1-10 digits"),
        ("12345678901", "cik must be 1-10 digits"),
        ("", "cik must be 1-10 digits"),  # edgar://issuers//profile
    ],
)
def test_malformed_cik_segment_is_not_found_without_a_request(
    fake_services: ServicesFactory, cik: str, match: str
) -> None:
    _, recorded, _ = fake_services()
    with pytest.raises(ResourceNotFoundError, match=match):
        read(ISSUER_PROFILE.format(cik=cik))
    assert recorded.requests == []


def test_unlisted_cik_is_not_found_and_never_hits_data_sec_gov(
    fake_services: ServicesFactory,
) -> None:
    _, recorded, _ = fake_services()
    with pytest.raises(
        ResourceNotFoundError, match="CIK 0099999999 is listed in the SEC ticker map"
    ):
        read(ISSUER_LATEST.format(cik="99999999"))
    assert urls(recorded) == [TICKER_MAP_URL]


def test_sec_outage_is_a_resource_error_keeping_the_retry_wording(
    fake_services: ServicesFactory,
) -> None:
    fake_services({TICKER_MAP_URL: [ok(TICKER_MAP)], SUBS_URL: [status(503)]})
    with pytest.raises(ResourceError) as excinfo:
        read(COMPANY_LATEST.format(ticker="AAPL"))
    assert not isinstance(excinfo.value, ResourceNotFoundError)
    assert "retry" in str(excinfo.value) and "not retryable" not in str(excinfo.value)


def test_missing_submissions_is_a_non_retryable_resource_error(
    fake_services: ServicesFactory,
) -> None:
    fake_services()  # BRK-B resolves to CIK 0001067983, which has no route -> 404
    with pytest.raises(ResourceError) as excinfo:
        read(COMPANY_LATEST.format(ticker="brk.b"))
    assert str(excinfo.value).endswith("(HTTP 404); not retryable")


def test_malformed_ticker_map_is_a_resource_error(fake_services: ServicesFactory) -> None:
    fake_services({TICKER_MAP_URL: [ok({"fields": ["cik"], "data": []})]})
    with pytest.raises(ResourceError) as excinfo:
        read(COMPANY_PROFILE.format(ticker="AAPL"))
    assert str(excinfo.value).startswith("SEC data had an unexpected shape")
    assert str(excinfo.value).endswith("; not retryable")


def test_unmatched_uri_is_not_found() -> None:
    with pytest.raises(ResourceNotFoundError, match="Unknown resource"):
        read("edgar://companies/AAPL/other")


def test_unknown_exceptions_are_not_translated() -> None:
    assert resource_error_for(RuntimeError("boom"), kind="profile") is None


@pytest.mark.parametrize(
    ("exc", "expected_type", "text"),
    [
        (UnknownTicker("ZZZ"), ResourceNotFoundError, "no SEC issuer is listed under ticker 'ZZZ'"),
        (InvalidTicker("ticker is empty", ticker=""), ResourceNotFoundError, "ticker is empty"),
        (SecNotFound(SUBS_URL), ResourceError, f"{SUBS_URL} (HTTP 404); not retryable"),
        (
            SecUnavailable(SUBS_URL, status_code=503, retry_after=None, attempts=3),
            ResourceError,
            "after 3 attempts; retry in a few seconds",
        ),
        (
            TickerMapError("bad"),
            ResourceError,
            "SEC data had an unexpected shape: bad; not retryable",
        ),
    ],
)
def test_resource_error_for_maps_the_tool_outcomes(
    exc: Exception, expected_type: type, text: str
) -> None:
    error = resource_error_for(exc, kind="latest")
    assert type(error) is expected_type
    assert str(error).endswith(text)


def test_issuer_profile_needs_only_the_ticker_map(fake_services: ServicesFactory) -> None:
    _, recorded, _ = fake_services()
    read(ISSUER_PROFILE.format(cik="320193"))
    read(ISSUER_PROFILE.format(cik="1652044"))
    assert urls(recorded) == [TICKER_MAP_URL]


def test_form_types_catalogue_metadata_keys(fake_services: ServicesFactory) -> None:
    fake_services()
    assert list(read(FORM_TYPES)["catalogue"]) == ["generated_on", "quarters", "sources"]


def test_crash_surfaces_as_unexpected_resource_error(fake_services: ServicesFactory) -> None:
    payload = copy.deepcopy(SUBMISSIONS)
    payload["filings"]["recent"]["accessionNumber"][0] = "bad"  # the newest filing
    fake_services({TICKER_MAP_URL: [ok(TICKER_MAP)], SUBS_URL: [ok(payload)]})
    with pytest.raises(UnexpectedResourceError):
        read(COMPANY_LATEST.format(ticker="AAPL"))


# --- builders -------------------------------------------------------------------------------


def test_profile_payload_keeps_file_order_and_deduplicates() -> None:
    goog = Issuer("0001652044", "Alphabet Inc.", "GOOG", "Nasdaq")
    googl = Issuer("0001652044", "Alphabet Inc.", "GOOGL", "Nasdaq")
    assert profile_payload(goog, (googl, goog))["tickers"] == ["GOOGL", "GOOG"]
    assert profile_payload(goog, (googl,))["tickers"] == ["GOOGL", "GOOG"]  # always present
    assert profile_payload(goog, ())["tickers"] == ["GOOG"]


def test_latest_payload_without_a_filing() -> None:
    issuer = Issuer(AAPL_CIK, "Apple Inc.", "AAPL", "Nasdaq")
    assert latest_payload(issuer, None) == {"company": AAPL_COMPANY, "filing": None}


# --- logging and stdout ---------------------------------------------------------------------


def test_each_read_logs_one_line(
    fake_services: ServicesFactory, caplog: pytest.LogCaptureFixture
) -> None:
    fake_services()
    caplog.set_level(logging.INFO, logger="mcp_edgar.resources")
    read(COMPANY_PROFILE.format(ticker="AAPL"))
    with pytest.raises(ResourceNotFoundError):
        read(COMPANY_PROFILE.format(ticker="ZZZZZZ"))
    messages = [r.getMessage() for r in caplog.records if r.name == "mcp_edgar.resources"]
    assert messages == [
        "resource company_profile 'AAPL' -> ok",
        "resource company_profile 'ZZZZZZ' -> ResourceNotFoundError",
    ]


def test_reads_write_nothing_to_stdout(
    fake_services: ServicesFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_services()
    read(COMPANY_LATEST.format(ticker="AAPL"))
    read(FORM_TYPES)
    with pytest.raises(ResourceError):
        read(COMPANY_PROFILE.format(ticker="DUAL"))
    assert capsys.readouterr().out == ""


# --- live -----------------------------------------------------------------------------------


@pytest.mark.live
def test_live_latest_resource_through_real_services() -> None:
    server.reset_services()
    try:
        try:
            payload = read(COMPANY_LATEST.format(ticker="AAPL"))
        except ResourceError as exc:
            if isinstance(exc, ResourceNotFoundError):
                raise
            pytest.skip(str(exc))
    finally:
        server.reset_services()
    assert payload["company"]["cik"] == AAPL_CIK
    assert payload["filing"]["filing_url"].startswith(AAPL_DIR)
