"""Tests for mcp_edgar.sec_client: headers, throttle, cache, retries, typed errors, fixtures.

Network-free (httpx.MockTransport); the single live check is behind ``@pytest.mark.live``.
"""

import logging
from typing import Any

import httpx
import pytest

from mcp_edgar.sec_client import (
    BACKOFF_MAX_SECONDS,
    RETRY_ATTEMPTS,
    TICKER_MAP_URL,
    RateLimiter,
    SecClient,
    SecClientError,
    SecNotFound,
    SecRequestError,
    SecUnavailable,
    submissions_file_url,
    submissions_url,
)
from mcp_edgar.settings import Settings, default_user_agent
from tests.conftest import ClientFactory, FakeClock, fixture_json

TICKER_URL = TICKER_MAP_URL
SUBS_URL = "https://data.sec.gov/submissions/CIK0000320193.json"
HIST_URL = "https://data.sec.gov/submissions/CIK0000320193-submissions-001.json"
CIK = "0000320193"
HIST_NAME = "CIK0000320193-submissions-001.json"

TICKER_MAP = fixture_json("company_tickers_exchange.json")
SUBMISSIONS = fixture_json("submissions_CIK0000320193.json")
HISTORY = fixture_json(HIST_NAME)

ARRAYS = {
    "accessionNumber",
    "filingDate",
    "reportDate",
    "acceptanceDateTime",
    "act",
    "form",
    "fileNumber",
    "filmNumber",
    "items",
    "core_type",
    "size",
    "isXBRL",
    "isInlineXBRL",
    "isXBRLNumeric",
    "primaryDocument",
    "primaryDocDescription",
}


def ok(payload: Any, **headers: str) -> httpx.Response:
    return httpx.Response(200, json=payload, headers=headers or None)


def status(code: int, **headers: str) -> httpx.Response:
    return httpx.Response(code, headers=headers or None)


# --- headers --------------------------------------------------------------------------------


def test_user_agent_and_accept_on_every_request(make_client: ClientFactory) -> None:
    client, recorded, _ = make_client({TICKER_URL: [ok(TICKER_MAP)], SUBS_URL: [ok(SUBMISSIONS)]})
    client.fetch_ticker_map()
    client.fetch_submissions(CIK)
    assert len(recorded.requests) == 2
    for request in recorded.requests:
        assert request.headers["User-Agent"] == default_user_agent()
        assert request.headers["Accept"] == "application/json"


def test_user_agent_override_is_sent(make_client: ClientFactory) -> None:
    ua = "acme-research/1.0 (ops@acme.example)"
    client, recorded, _ = make_client(
        {TICKER_URL: [ok(TICKER_MAP)]}, env={"MCP_EDGAR_USER_AGENT": ua}
    )
    client.fetch_ticker_map()
    assert recorded.requests[0].headers["User-Agent"] == ua


# --- RateLimiter unit -----------------------------------------------------------------------


def test_limiter_allows_burst_then_sleeps() -> None:
    clock = FakeClock()
    limiter = RateLimiter(10, clock=clock, sleep=clock.sleep)
    for _ in range(10):
        limiter.acquire()
    assert clock.sleeps == []
    limiter.acquire()
    assert clock.sleeps == [pytest.approx(1.0)]


def test_limiter_does_not_sleep_after_window_passes() -> None:
    clock = FakeClock()
    limiter = RateLimiter(10, clock=clock, sleep=clock.sleep)
    for _ in range(10):
        limiter.acquire()
    clock.advance(1.01)
    limiter.acquire()
    assert clock.sleeps == []


def test_limiter_one_per_second() -> None:
    clock = FakeClock()
    limiter = RateLimiter(1, clock=clock, sleep=clock.sleep)
    limiter.acquire()
    limiter.acquire()
    limiter.acquire()
    assert clock.sleeps == [pytest.approx(1.0), pytest.approx(1.0)]


def test_limiter_rejects_zero() -> None:
    with pytest.raises(ValueError):
        RateLimiter(0)


# --- throttle through the client ------------------------------------------------------------


def test_burst_never_exceeds_ten_per_second(make_client: ClientFactory) -> None:
    client, recorded, _ = make_client(
        {TICKER_URL: [ok(TICKER_MAP)]}, env={"MCP_EDGAR_TICKER_MAP_TTL_SECONDS": "0"}
    )
    for _ in range(25):
        client.fetch_ticker_map()
    assert len(recorded.requests) == 25
    times = recorded.times
    for i in range(len(times) - 10):
        assert times[i + 10] - times[i] >= 1.0 - 1e-9


def test_retries_acquire_the_limiter(
    make_client: ClientFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, recorded, _ = make_client({SUBS_URL: [status(503), status(503), ok(SUBMISSIONS)]})
    acquires: list[int] = []
    original = client._limiter.acquire

    def spy() -> None:
        acquires.append(1)
        original()

    monkeypatch.setattr(client._limiter, "acquire", spy)
    client.fetch_submissions(CIK)
    assert len(recorded.requests) == 3
    assert len(acquires) == 3


# --- backoff --------------------------------------------------------------------------------


def test_429_exhausts_into_sec_unavailable(make_client: ClientFactory) -> None:
    client, recorded, clock = make_client({SUBS_URL: [status(429)]})
    with pytest.raises(SecUnavailable) as excinfo:
        client.fetch_submissions(CIK)
    exc = excinfo.value
    assert exc.retryable is True
    assert isinstance(exc, SecClientError)
    assert exc.status_code == 429
    assert exc.attempts == RETRY_ATTEMPTS == 4
    assert exc.url == SUBS_URL
    assert SUBS_URL in str(exc)
    assert "4 attempts" in str(exc)
    assert len(recorded.requests) == 4
    assert clock.sleeps == [0.5, 1.0, 2.0]


def test_retry_after_seconds_is_honoured(make_client: ClientFactory) -> None:
    client, recorded, clock = make_client(
        {SUBS_URL: [status(429, **{"Retry-After": "3"}), ok(SUBMISSIONS)]}
    )
    assert client.fetch_submissions(CIK) == SUBMISSIONS
    assert clock.sleeps == [3.0]
    assert len(recorded.requests) == 2


def test_retry_after_is_capped(make_client: ClientFactory) -> None:
    client, _, clock = make_client({SUBS_URL: [status(429, **{"Retry-After": "60"}), ok({})]})
    client.fetch_submissions(CIK)
    assert clock.sleeps == [BACKOFF_MAX_SECONDS] == [8.0]


def test_retry_after_http_date_falls_back_to_computed_delay(make_client: ClientFactory) -> None:
    client, _, clock = make_client(
        {SUBS_URL: [status(429, **{"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}), ok({})]}
    )
    client.fetch_submissions(CIK)
    assert clock.sleeps == [0.5]


def test_503_then_200_returns_payload(make_client: ClientFactory) -> None:
    client, recorded, clock = make_client({SUBS_URL: [status(503), ok(SUBMISSIONS)]})
    assert client.fetch_submissions(CIK) == SUBMISSIONS
    assert len(recorded.requests) == 2
    assert clock.sleeps == [0.5]


def test_5xx_sequence_succeeds_on_fourth_attempt(make_client: ClientFactory) -> None:
    client, recorded, clock = make_client(
        {SUBS_URL: [status(500), status(502), status(504), ok(SUBMISSIONS)]}
    )
    assert client.fetch_submissions(CIK) == SUBMISSIONS
    assert len(recorded.requests) == 4
    assert clock.sleeps == [0.5, 1.0, 2.0]


def test_unavailable_reports_last_retry_after(make_client: ClientFactory) -> None:
    client, _, _ = make_client({SUBS_URL: [status(503), status(429, **{"Retry-After": "2"})]})
    with pytest.raises(SecUnavailable) as excinfo:
        client.fetch_submissions(CIK)
    assert excinfo.value.retry_after == 2.0
    assert excinfo.value.status_code == 429


# --- non-retryable --------------------------------------------------------------------------


def test_404_raises_not_found_without_retry(make_client: ClientFactory) -> None:
    client, recorded, clock = make_client({SUBS_URL: [status(404)]})
    with pytest.raises(SecNotFound) as excinfo:
        client.fetch_submissions(CIK)
    assert excinfo.value.retryable is False
    assert excinfo.value.url == SUBS_URL
    assert len(recorded.requests) == 1
    assert clock.sleeps == []


def test_403_raises_request_error_without_retry(make_client: ClientFactory) -> None:
    client, recorded, clock = make_client({SUBS_URL: [status(403)]})
    with pytest.raises(SecRequestError) as excinfo:
        client.fetch_submissions(CIK)
    assert excinfo.value.status_code == 403
    assert excinfo.value.retryable is False
    assert len(recorded.requests) == 1
    assert clock.sleeps == []


def test_non_json_body_raises_request_error_and_caches_nothing(
    make_client: ClientFactory,
) -> None:
    client, recorded, _ = make_client({TICKER_URL: [httpx.Response(200, text="<html>oops</html>")]})
    with pytest.raises(SecRequestError) as excinfo:
        client.fetch_ticker_map()
    assert excinfo.value.status_code == 200
    assert "not JSON" in str(excinfo.value)
    with pytest.raises(SecRequestError):
        client.fetch_ticker_map()
    assert len(recorded.requests) == 2


# --- transport errors -----------------------------------------------------------------------


def test_connect_timeout_then_success(make_client: ClientFactory) -> None:
    client, recorded, clock = make_client(
        {TICKER_URL: [httpx.ConnectTimeout("boom"), ok(TICKER_MAP)]}
    )
    assert client.fetch_ticker_map() == TICKER_MAP
    assert len(recorded.requests) == 2
    assert clock.sleeps == [0.5]


def test_repeated_read_timeouts_exhaust(make_client: ClientFactory) -> None:
    client, recorded, _ = make_client({TICKER_URL: [httpx.ReadTimeout("slow")]})
    with pytest.raises(SecUnavailable) as excinfo:
        client.fetch_ticker_map()
    assert excinfo.value.status_code is None
    assert "network error" in str(excinfo.value)
    assert len(recorded.requests) == 4


# --- cache ----------------------------------------------------------------------------------


def test_cache_hit_avoids_second_request(make_client: ClientFactory) -> None:
    client, recorded, _ = make_client({TICKER_URL: [ok(TICKER_MAP)]})
    first = client.fetch_ticker_map()
    second = client.fetch_ticker_map()
    assert first is second
    assert len(recorded.requests) == 1


def test_cache_expires_after_ttl(make_client: ClientFactory) -> None:
    client, recorded, clock = make_client({TICKER_URL: [ok(TICKER_MAP)]})
    client.fetch_ticker_map()
    clock.advance(86_400 + 1)
    client.fetch_ticker_map()
    assert len(recorded.requests) == 2


def test_ttl_zero_disables_cache_and_validators(make_client: ClientFactory) -> None:
    client, recorded, _ = make_client(
        {TICKER_URL: [ok(TICKER_MAP, ETag='"abc"')]},
        env={"MCP_EDGAR_TICKER_MAP_TTL_SECONDS": "0"},
    )
    client.fetch_ticker_map()
    client.fetch_ticker_map()
    assert len(recorded.requests) == 2
    assert "If-None-Match" not in recorded.requests[1].headers


def test_ttl_categories_are_independent(make_client: ClientFactory) -> None:
    client, recorded, clock = make_client(
        {TICKER_URL: [ok(TICKER_MAP)], SUBS_URL: [ok(SUBMISSIONS)]},
        env={
            "MCP_EDGAR_TICKER_MAP_TTL_SECONDS": "10",
            "MCP_EDGAR_SUBMISSIONS_TTL_SECONDS": "1000",
        },
    )
    client.fetch_ticker_map()
    client.fetch_submissions(CIK)
    clock.advance(11)
    client.fetch_ticker_map()
    client.fetch_submissions(CIK)
    urls = [str(r.url) for r in recorded.requests]
    assert urls.count(TICKER_URL) == 2
    assert urls.count(SUBS_URL) == 1


def test_clear_cache_forces_refetch(make_client: ClientFactory) -> None:
    client, recorded, _ = make_client({TICKER_URL: [ok(TICKER_MAP)]})
    client.fetch_ticker_map()
    client.clear_cache()
    client.fetch_ticker_map()
    assert len(recorded.requests) == 2


# --- validators -----------------------------------------------------------------------------


def test_304_renews_cached_value(make_client: ClientFactory) -> None:
    last_modified = "Mon, 26 Oct 2026 21:10:44 GMT"
    client, recorded, clock = make_client(
        {
            TICKER_URL: [
                ok(TICKER_MAP, ETag='"abc"', **{"Last-Modified": last_modified}),
                status(304),
            ]
        }
    )
    first = client.fetch_ticker_map()
    clock.advance(86_400 + 1)
    second = client.fetch_ticker_map()
    assert second is first
    assert len(recorded.requests) == 2
    revalidation = recorded.requests[1].headers
    assert revalidation["If-None-Match"] == '"abc"'
    assert revalidation["If-Modified-Since"] == last_modified
    client.fetch_ticker_map()  # renewed TTL: no third request
    assert len(recorded.requests) == 2


def test_200_after_expiry_replaces_value_and_validators(make_client: ClientFactory) -> None:
    client, recorded, clock = make_client(
        {TICKER_URL: [ok({"v": 1}, ETag='"a"'), ok({"v": 2}, ETag='"b"'), status(304)]}
    )
    assert client.fetch_ticker_map() == {"v": 1}
    clock.advance(86_400 + 1)
    assert client.fetch_ticker_map() == {"v": 2}
    clock.advance(86_400 + 1)
    assert client.fetch_ticker_map() == {"v": 2}
    assert recorded.requests[2].headers["If-None-Match"] == '"b"'
    assert len(recorded.requests) == 3


# --- URL helpers ----------------------------------------------------------------------------


def test_submissions_url_for_padded_cik() -> None:
    assert submissions_url(CIK) == SUBS_URL


@pytest.mark.parametrize("bad", ["320193", "00003201930", "000032019a", "", "CIK0000320193"])
def test_submissions_url_rejects_unpadded_or_non_digit(bad: str) -> None:
    with pytest.raises(ValueError):
        submissions_url(bad)


def test_submissions_file_url_for_history_name() -> None:
    assert submissions_file_url(HIST_NAME) == HIST_URL


@pytest.mark.parametrize(
    "bad",
    ["../x.json", "a/b.json", "a\\b.json", "a%2e%2e.json", "", "x.txt", "..json", "x.json\n"],
)
def test_submissions_file_url_rejects_unsafe_names(bad: str) -> None:
    with pytest.raises(ValueError):
        submissions_file_url(bad)


def test_fetch_submissions_rejects_unpadded_cik_before_any_request(
    make_client: ClientFactory,
) -> None:
    client, recorded, _ = make_client({})
    with pytest.raises(ValueError):
        client.fetch_submissions("320193")
    assert recorded.requests == []


def test_fetch_submissions_file_rejects_traversal_before_any_request(
    make_client: ClientFactory,
) -> None:
    client, recorded, _ = make_client({})
    with pytest.raises(ValueError):
        client.fetch_submissions_file("../CIK0000320193.json")
    assert recorded.requests == []


# --- fetch methods --------------------------------------------------------------------------


def test_fetch_submissions_requests_padded_url(make_client: ClientFactory) -> None:
    client, recorded, _ = make_client({SUBS_URL: [ok(SUBMISSIONS)]})
    assert client.fetch_submissions(CIK) == SUBMISSIONS
    assert str(recorded.requests[0].url) == SUBS_URL


def test_fetch_submissions_file_requests_history_url(make_client: ClientFactory) -> None:
    client, recorded, _ = make_client({HIST_URL: [ok(HISTORY)]})
    value = client.fetch_submissions_file(HIST_NAME)
    assert value["filingDate"][0] == "2015-10-02"
    assert str(recorded.requests[0].url) == HIST_URL


def test_fetch_submissions_uses_submissions_ttl(make_client: ClientFactory) -> None:
    client, recorded, clock = make_client(
        {SUBS_URL: [ok(SUBMISSIONS)]}, env={"MCP_EDGAR_SUBMISSIONS_TTL_SECONDS": "5"}
    )
    client.fetch_submissions(CIK)
    client.fetch_submissions(CIK)
    assert len(recorded.requests) == 1
    clock.advance(6)
    client.fetch_submissions(CIK)
    assert len(recorded.requests) == 2


def test_unknown_url_answers_not_found(make_client: ClientFactory) -> None:
    client, _, _ = make_client({})
    with pytest.raises(SecNotFound):
        client.get_json("https://data.sec.gov/submissions/CIK0000000001.json", ttl_seconds=0)


# --- fixture integrity ----------------------------------------------------------------------


def test_submissions_fixture_arrays_are_parallel() -> None:
    recent = SUBMISSIONS["filings"]["recent"]
    assert set(recent) == ARRAYS
    lengths = {len(values) for values in recent.values()}
    assert lengths == {12}
    assert SUBMISSIONS["cik"] == CIK  # SEC serves the top-level cik zero-padded (verified live)
    assert SUBMISSIONS["filings"]["files"] == [
        {
            "name": HIST_NAME,
            "filingCount": 6,
            "filingFrom": "2004-02-10",
            "filingTo": "2015-10-02",
        }
    ]
    index = recent["form"].index("10-K")
    assert recent["filingDate"][index] == "2026-01-30"
    assert recent["reportDate"][index] == "2025-09-27"


def test_history_fixture_arrays_are_top_level_and_parallel() -> None:
    assert "filings" not in HISTORY
    assert set(HISTORY) == ARRAYS
    assert {len(values) for values in HISTORY.values()} == {6}
    assert HISTORY["filingDate"] == sorted(HISTORY["filingDate"], reverse=True)
    assert HISTORY["filingDate"].count("2005-12-01") == 1
    assert sum(d.startswith("2005") for d in HISTORY["filingDate"]) == 2


def test_ticker_fixture_shape_and_ambiguity() -> None:
    assert TICKER_MAP["fields"] == ["cik", "name", "ticker", "exchange"]
    dual = [row for row in TICKER_MAP["data"] if row[2] == "DUAL"]
    assert len(dual) == 2
    assert dual[0][0] != dual[1][0]
    assert all(isinstance(row[0], int) for row in TICKER_MAP["data"])


# --- logging --------------------------------------------------------------------------------


def test_retries_log_warnings_then_error_to_stderr_only(
    make_client: ClientFactory,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    caplog.set_level(logging.DEBUG, logger="mcp_edgar.sec_client")
    client, _, _ = make_client({SUBS_URL: [status(429)]})
    with pytest.raises(SecUnavailable):
        client.fetch_submissions(CIK)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(warnings) == 3
    assert all(SUBS_URL in r.getMessage() for r in warnings)
    assert len(errors) == 1
    assert capsys.readouterr().out == ""


# --- lifecycle ------------------------------------------------------------------------------


def test_context_manager_closes_http_client(make_client: ClientFactory) -> None:
    client, _, _ = make_client({})
    with client as entered:
        assert entered is client
        assert not client._http.is_closed
    assert client._http.is_closed


# --- live -----------------------------------------------------------------------------------


@pytest.mark.live
def test_live_sec_endpoints_answer() -> None:
    try:
        with SecClient(Settings.from_env({})) as client:
            ticker_map = client.fetch_ticker_map()
            submissions = client.fetch_submissions(CIK)
    except (SecUnavailable, httpx.TransportError) as exc:
        pytest.skip(f"SEC unreachable: {exc}")
    assert ticker_map["fields"] == ["cik", "name", "ticker", "exchange"]
    assert submissions["cik"] == CIK
    assert "AAPL" in submissions["tickers"]
