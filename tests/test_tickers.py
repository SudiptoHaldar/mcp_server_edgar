"""Tests for mcp_edgar.tickers: normalisation, CIK formatting, the ticker index, the resolver."""

import copy
from typing import Any

import httpx
import pytest

from mcp_edgar import tickers
from mcp_edgar.sec_client import TICKER_MAP_URL, SecUnavailable
from mcp_edgar.tickers import (
    AmbiguousTicker,
    InvalidCik,
    InvalidTicker,
    Issuer,
    TickerError,
    TickerMap,
    TickerMapError,
    TickerResolver,
    UnknownTicker,
    format_cik,
    normalize_ticker,
)
from tests.conftest import ClientFactory, fixture_json

TICKER_MAP = fixture_json("company_tickers_exchange.json")
AAPL = Issuer(cik="0000320193", name="Apple Inc.", ticker="AAPL", exchange="Nasdaq")


def ok(payload: Any, **headers: str) -> httpx.Response:
    return httpx.Response(200, json=payload, headers=headers or None)


def status(code: int, **headers: str) -> httpx.Response:
    return httpx.Response(code, headers=headers or None)


def ticker_map() -> TickerMap:
    return TickerMap.from_payload(TICKER_MAP)


# --- normalize_ticker -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("aapl", "AAPL"),
        ("  msft ", "MSFT"),
        ("brk-b", "BRK-B"),
        ("BRK.B", "BRK.B"),
        ("NONE.", "NONE."),
        ("AAPL\n", "AAPL"),
        ("a1", "A1"),
        ("0", "0"),
    ],
)
def test_normalize_accepts_and_upper_cases(raw: str, expected: str) -> None:
    assert normalize_ticker(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["", "   ", "A" * 11, "AA PL", "AA\tPL", "AAPL;", "aapl$", "-AAPL", ".AAPL", "ÄPL", "A/B"],
)
def test_normalize_rejects_invalid_input(raw: str) -> None:
    with pytest.raises(InvalidTicker) as excinfo:
        normalize_ticker(raw)
    exc = excinfo.value
    assert isinstance(exc, ValueError)
    assert isinstance(exc, TickerError)
    assert exc.ticker == raw.strip()


def test_normalize_messages_name_the_problem() -> None:
    with pytest.raises(InvalidTicker, match="empty"):
        normalize_ticker("  ")
    with pytest.raises(InvalidTicker, match="longer than 10"):
        normalize_ticker("ABCDEFGHIJK")
    with pytest.raises(InvalidTicker, match="unsupported characters ' ;'"):
        normalize_ticker("AA PL;")
    with pytest.raises(InvalidTicker, match="must start with a letter or digit"):
        normalize_ticker("-AAPL")


# --- format_cik -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cik", "expected"),
    [
        (320193, "0000320193"),
        ("320193", "0000320193"),
        ("0000320193", "0000320193"),
        (1234, "0000001234"),
        (" 42 ", "0000000042"),
        (2160519, "0002160519"),
    ],
)
def test_format_cik_pads_to_ten_digits(cik: int | str, expected: str) -> None:
    assert format_cik(cik) == expected


@pytest.mark.parametrize("cik", ["", "abc", -1, 12345678901, "12 3", "²", True, 3.0])
def test_format_cik_rejects_non_cik_values(cik: Any) -> None:
    with pytest.raises(ValueError):
        format_cik(cik)


# --- TickerMap ------------------------------------------------------------------------------


def test_map_counts_distinct_issuers_after_collapsing() -> None:
    assert len(TICKER_MAP["data"]) == 12
    assert len(ticker_map()) == 11  # BOTH appears twice with one CIK


def test_lookup_apple() -> None:
    assert ticker_map().lookup("AAPL") == (AAPL,)


def test_one_cik_under_two_tickers_is_not_ambiguous() -> None:
    tm = ticker_map()
    (googl,) = tm.lookup("GOOGL")
    (goog,) = tm.lookup("GOOG")
    assert googl.cik == goog.cik == "0001652044"
    assert googl.ticker == "GOOGL" and goog.ticker == "GOOG"


def test_two_ciks_under_one_ticker_stay_separate_in_file_order() -> None:
    dual = ticker_map().lookup("DUAL")
    assert [i.cik for i in dual] == ["0001000001", "0001000002"]
    assert [i.exchange for i in dual] == ["NYSE", "CBOE"]
    assert [i.name for i in dual] == ["Dual Listed Holdings Inc.", "Dual Energy Trust"]


def test_same_ticker_same_cik_rows_collapse_keeping_first_exchange() -> None:
    (both,) = ticker_map().lookup("BOTH")
    assert both == Issuer("0001000003", "Both Boards Corp", "BOTH", "NYSE")


def test_null_exchange_becomes_none() -> None:
    (nobd,) = ticker_map().lookup("NOBD")
    assert nobd.exchange is None


def test_short_cik_is_zero_padded() -> None:
    (tiny,) = ticker_map().lookup("TINY")
    assert tiny.cik == "0000001234"


def test_lookup_miss_returns_empty_tuple() -> None:
    assert ticker_map().lookup("ZZZZ") == ()


def test_lookup_alias_dot_to_hyphen_and_back() -> None:
    tm = ticker_map()
    (direct,) = tm.lookup("BRK-B")
    assert direct.cik == "0001067983"
    assert tm.lookup("BRK.B") == (direct,)
    # a hyphen key that only exists with a dot resolves too
    payload = {
        "fields": ["cik", "name", "ticker", "exchange"],
        "data": [[1, "Dot Co", "X.Y", "OTC"]],
    }
    assert TickerMap.from_payload(payload).lookup("X-Y")[0].ticker == "X.Y"
    assert tm.lookup("BRK_B") == ()


def test_empty_exchange_string_is_none_and_later_row_fills_it() -> None:
    payload = {
        "fields": ["cik", "name", "ticker", "exchange"],
        "data": [[7, "Late Listing", "LATE", ""], [7, "Late Listing", "LATE", "NYSE"]],
    }
    (late,) = TickerMap.from_payload(payload).lookup("LATE")
    assert late.exchange == "NYSE"


def test_fields_order_is_read_by_name() -> None:
    payload = {
        "fields": ["ticker", "cik", "exchange", "name"],
        "data": [["AAPL", 320193, "Nasdaq", "Apple Inc."]],
    }
    assert TickerMap.from_payload(payload).lookup("AAPL") == (AAPL,)


def test_lower_case_ticker_in_payload_is_indexed_upper() -> None:
    payload = {"fields": ["cik", "name", "ticker", "exchange"], "data": [[5, "Lower", "low", None]]}
    assert TickerMap.from_payload(payload).lookup("LOW")[0].ticker == "LOW"


# --- TickerMap.issuers_for_cik (req_006 v6.01) ----------------------------------------------


def test_issuers_for_cik_returns_the_rows_in_file_order() -> None:
    tm = ticker_map()
    assert tm.issuers_for_cik("0000320193") == (AAPL,)
    alphabet = tm.issuers_for_cik("0001652044")
    assert [issuer.ticker for issuer in alphabet] == ["GOOGL", "GOOG"]
    assert alphabet[0] is tm.lookup("GOOGL")[0]  # shares the ticker index's objects


def test_issuers_for_cik_collapses_exchanges_and_misses_cleanly() -> None:
    tm = ticker_map()
    (both,) = tm.issuers_for_cik("0001000003")
    assert (both.ticker, both.exchange) == ("BOTH", "NYSE")
    assert tm.issuers_for_cik("0099999999") == ()
    assert tm.issuers_for_cik("320193") == ()  # the key is the 10-digit form
    assert TickerMap({}).issuers_for_cik("0000320193") == ()  # by_cik defaults to empty


def test_issuers_for_cik_follows_first_appearance_across_interleaved_rows() -> None:
    payload = {
        "fields": ["cik", "name", "ticker", "exchange"],
        "data": [
            [9, "Nine", "NINE-B", "NYSE"],
            [8, "Eight", "EIGHT", None],
            [9, "Nine", "NINE-A", "NYSE"],
            [9, "Nine", "NINE-B", "Nasdaq"],  # same ticker and CIK: collapsed, order kept
        ],
    }
    tm = TickerMap.from_payload(payload)
    assert [i.ticker for i in tm.issuers_for_cik("0000000009")] == ["NINE-B", "NINE-A"]
    assert tm.issuers_for_cik("0000000008")[0].exchange is None


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        ([], "not a JSON object"),
        ({"fields": ["cik", "name", "ticker", "exchange"]}, "'fields' and 'data'"),
        ({"fields": ["cik", "name", "ticker"], "data": []}, "missing \\['exchange'\\]"),
        (
            {"fields": ["cik", "name", "ticker", "exchange"], "data": [[1, "Short"]]},
            "row 0 is malformed",
        ),
        (
            {"fields": ["cik", "name", "ticker", "exchange"], "data": [["x", "Bad", "BAD", None]]},
            "row 0 has an invalid CIK",
        ),
        (
            {"fields": ["cik", "name", "ticker", "exchange"], "data": [[1, "Blank", "  ", None]]},
            "row 0 has an empty ticker",
        ),
        (
            {
                "fields": ["cik", "name", "ticker", "exchange"],
                "data": [[1, "A", "A", None], "nope"],
            },
            "row 1 is malformed",
        ),
    ],
)
def test_malformed_payloads_raise_ticker_map_error(payload: Any, match: str) -> None:
    with pytest.raises(TickerMapError, match=match):
        TickerMap.from_payload(payload)


# --- Issuer ---------------------------------------------------------------------------------


def test_issuer_is_frozen() -> None:
    with pytest.raises(AttributeError):
        AAPL.cik = "1"  # type: ignore[misc]


def test_issuer_as_dict_shape() -> None:
    assert AAPL.as_dict() == {
        "cik": "0000320193",
        "name": "Apple Inc.",
        "ticker": "AAPL",
        "exchange": "Nasdaq",
    }
    assert Issuer("0000000001", "N", "N", None).as_dict()["exchange"] is None


# --- TickerResolver -------------------------------------------------------------------------


def test_resolve_lower_case_ticker(make_client: ClientFactory) -> None:
    client, recorded, _ = make_client({TICKER_MAP_URL: [ok(TICKER_MAP)]})
    assert TickerResolver(client).resolve("aapl") == AAPL
    assert [str(r.url) for r in recorded.requests] == [TICKER_MAP_URL]


def test_unknown_ticker_makes_no_submissions_request(make_client: ClientFactory) -> None:
    client, recorded, _ = make_client({TICKER_MAP_URL: [ok(TICKER_MAP)]})
    with pytest.raises(UnknownTicker) as excinfo:
        TickerResolver(client).resolve(" zzzz ")
    assert excinfo.value.ticker == "zzzz"
    assert isinstance(excinfo.value, LookupError)
    assert "zzzz" in str(excinfo.value)
    urls = [str(r.url) for r in recorded.requests]
    assert urls == [TICKER_MAP_URL]
    assert not any("data.sec.gov" in u for u in urls)


def test_ambiguous_ticker_returns_candidates(make_client: ClientFactory) -> None:
    client, _, _ = make_client({TICKER_MAP_URL: [ok(TICKER_MAP)]})
    with pytest.raises(AmbiguousTicker) as excinfo:
        TickerResolver(client).resolve("dual")
    exc = excinfo.value
    assert exc.ticker == "dual"
    assert len(exc.candidates) == 2
    dicts = [c.as_dict() for c in exc.candidates]
    assert all(set(d) == {"cik", "name", "ticker", "exchange"} for d in dicts)
    assert [d["exchange"] for d in dicts] == ["NYSE", "CBOE"]
    assert "2 issuers" in str(exc)


def test_invalid_ticker_makes_no_request(make_client: ClientFactory) -> None:
    client, recorded, _ = make_client({TICKER_MAP_URL: [ok(TICKER_MAP)]})
    with pytest.raises(InvalidTicker):
        TickerResolver(client).resolve("aa pl")
    assert recorded.requests == []


def test_alias_resolves_through_resolver(make_client: ClientFactory) -> None:
    client, _, _ = make_client({TICKER_MAP_URL: [ok(TICKER_MAP)]})
    assert TickerResolver(client).resolve("brk.b").ticker == "BRK-B"


# --- TickerResolver: cik selection (req_002 v2.03) -----------------------------------------


@pytest.mark.parametrize("cik", ["320193", "0000320193", " 320193 ", 320193])
def test_matching_cik_on_unambiguous_ticker_is_accepted(
    make_client: ClientFactory, cik: str | int
) -> None:
    client, _, _ = make_client({TICKER_MAP_URL: [ok(TICKER_MAP)]})
    assert TickerResolver(client).resolve("aapl", cik=cik) == AAPL


@pytest.mark.parametrize("cik", [None, "", "   "])
def test_blank_cik_means_no_selection(make_client: ClientFactory, cik: str | None) -> None:
    client, _, _ = make_client({TICKER_MAP_URL: [ok(TICKER_MAP)]})
    resolver = TickerResolver(client)
    assert resolver.resolve("aapl", cik=cik) == AAPL
    with pytest.raises(AmbiguousTicker):
        resolver.resolve("dual", cik=cik)


@pytest.mark.parametrize(
    ("cik", "name"),
    [("0001000001", "Dual Listed Holdings Inc."), ("1000002", "Dual Energy Trust")],
)
def test_cik_selects_one_ambiguous_candidate(
    make_client: ClientFactory, cik: str, name: str
) -> None:
    client, _, _ = make_client({TICKER_MAP_URL: [ok(TICKER_MAP)]})
    issuer = TickerResolver(client).resolve("dual", cik=cik)
    assert (issuer.name, issuer.ticker) == (name, "DUAL")


def test_unlisted_cik_names_the_listed_ones(make_client: ClientFactory) -> None:
    client, recorded, _ = make_client({TICKER_MAP_URL: [ok(TICKER_MAP)]})
    with pytest.raises(InvalidCik) as excinfo:
        TickerResolver(client).resolve("dual", cik="0000999999")
    exc = excinfo.value
    assert isinstance(exc, TickerError) and isinstance(exc, ValueError)
    assert (exc.cik, exc.ticker) == ("0000999999", "dual")
    assert "0001000001" in str(exc) and "0001000002" in str(exc)
    assert [str(r.url) for r in recorded.requests] == [TICKER_MAP_URL]


def test_unlisted_cik_on_unambiguous_ticker(make_client: ClientFactory) -> None:
    client, _, _ = make_client({TICKER_MAP_URL: [ok(TICKER_MAP)]})
    with pytest.raises(InvalidCik, match="not listed under ticker AAPL; listed: 0000320193"):
        TickerResolver(client).resolve("AAPL", cik="1")


@pytest.mark.parametrize("cik", ["abc", "12345678901", "12 34", "-1", "1.0"])
def test_malformed_cik_costs_no_request(make_client: ClientFactory, cik: str) -> None:
    client, recorded, _ = make_client({TICKER_MAP_URL: [ok(TICKER_MAP)]})
    with pytest.raises(InvalidCik, match="1-10 digits") as excinfo:
        TickerResolver(client).resolve("AAPL", cik=cik)
    assert excinfo.value.cik == cik
    assert recorded.requests == []


def test_unknown_ticker_wins_over_cik(make_client: ClientFactory) -> None:
    client, recorded, _ = make_client({TICKER_MAP_URL: [ok(TICKER_MAP)]})
    with pytest.raises(UnknownTicker):
        TickerResolver(client).resolve("zzzz", cik="320193")
    assert len(recorded.requests) == 1


def test_resolver_issuers_for_cik_accepts_any_spelling_and_shares_the_map(
    make_client: ClientFactory,
) -> None:
    client, recorded, _ = make_client({TICKER_MAP_URL: [ok(TICKER_MAP)]})
    resolver = TickerResolver(client)
    assert resolver.issuers_for_cik(320193) == (AAPL,)
    assert resolver.issuers_for_cik("320193") == (AAPL,)
    assert resolver.issuers_for_cik("0000320193") == (AAPL,)
    assert resolver.issuers_for_cik("99999999") == ()
    assert resolver.resolve("aapl") == AAPL
    assert len(recorded.requests) == 1  # one ticker-map fetch serves every call


@pytest.mark.parametrize("cik", ["abc", "12345678901", "", True, 1.5])
def test_resolver_issuers_for_cik_rejects_malformed_input_before_io(
    make_client: ClientFactory, cik: Any
) -> None:
    client, recorded, _ = make_client({TICKER_MAP_URL: [ok(TICKER_MAP)]})
    with pytest.raises(ValueError):
        TickerResolver(client).issuers_for_cik(cik)
    assert recorded.requests == []


def _spy_index_builds(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls: list[int] = []
    original = TickerMap.from_payload

    def spy(payload: Any) -> TickerMap:
        calls.append(1)
        return original(payload)

    monkeypatch.setattr(tickers.TickerMap, "from_payload", staticmethod(spy))
    return calls


def test_index_is_reused_while_payload_is_cached(
    make_client: ClientFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    builds = _spy_index_builds(monkeypatch)
    client, recorded, _ = make_client({TICKER_MAP_URL: [ok(TICKER_MAP)]})
    resolver = TickerResolver(client)
    resolver.resolve("aapl")
    resolver.resolve("msft")
    assert len(recorded.requests) == 1
    assert len(builds) == 1


def test_index_rebuilds_after_ttl_expiry(
    make_client: ClientFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    builds = _spy_index_builds(monkeypatch)
    client, recorded, clock = make_client({TICKER_MAP_URL: [ok(TICKER_MAP)]})
    resolver = TickerResolver(client)
    resolver.resolve("aapl")
    clock.advance(86_401)
    resolver.resolve("aapl")
    assert len(recorded.requests) == 2
    assert len(builds) == 2


def test_304_revalidation_does_not_rebuild_index(
    make_client: ClientFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    builds = _spy_index_builds(monkeypatch)
    client, recorded, clock = make_client(
        {TICKER_MAP_URL: [ok(TICKER_MAP, ETag='"v1"'), status(304)]}
    )
    resolver = TickerResolver(client)
    resolver.resolve("aapl")
    clock.advance(86_401)
    assert resolver.resolve("nvda").ticker == "NVDA"
    assert len(recorded.requests) == 2
    assert len(builds) == 1


def test_changed_payload_after_expiry_is_picked_up(make_client: ClientFactory) -> None:
    updated = copy.deepcopy(TICKER_MAP)
    updated["data"].append([999, "New Listing", "NEWL", "NYSE"])
    client, _, clock = make_client({TICKER_MAP_URL: [ok(TICKER_MAP), ok(updated)]})
    resolver = TickerResolver(client)
    with pytest.raises(UnknownTicker):
        resolver.resolve("newl")
    clock.advance(86_401)
    assert resolver.resolve("newl").cik == "0000000999"


def test_sec_unavailable_propagates_not_unknown(make_client: ClientFactory) -> None:
    client, _, _ = make_client({TICKER_MAP_URL: [status(429)]})
    with pytest.raises(SecUnavailable):
        TickerResolver(client).resolve("aapl")


def test_malformed_payload_propagates_ticker_map_error(make_client: ClientFactory) -> None:
    client, _, _ = make_client({TICKER_MAP_URL: [ok({"unexpected": True})]})
    with pytest.raises(TickerMapError):
        TickerResolver(client).resolve("aapl")


# --- fixture integrity ----------------------------------------------------------------------


def test_fixture_rows_for_v102() -> None:
    rows = TICKER_MAP["data"]
    assert len(rows) == 12
    both = [r for r in rows if r[2] == "BOTH"]
    assert len(both) == 2 and both[0][0] == both[1][0]
    assert {r[3] for r in both} == {"NYSE", "Nasdaq"}
    (nobd,) = [r for r in rows if r[2] == "NOBD"]
    assert nobd[3] is None
    assert any(r[2] == "BRK-B" and r[0] == 1067983 for r in rows)
