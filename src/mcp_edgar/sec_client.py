"""SEC HTTP access: User-Agent, rate limiting, caching, retries/backoff.

``SecClient`` is the data layer's only network boundary. It is synchronous (the MCP SDK runs
sync tool functions in worker threads, see gotchas #14) and thread-safe. Callers receive parsed
JSON or one of the typed errors below; nothing here ever returns an empty result on failure.
Parsing of the SEC payloads belongs to ``filings.py``.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

from mcp_edgar.settings import Settings

logger = logging.getLogger(__name__)

TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
SUBMISSIONS_BASE_URL = "https://data.sec.gov/submissions/"

RETRY_ATTEMPTS = 4  # 1 initial + 3 retries
BACKOFF_BASE_SECONDS = 0.5  # delays 0.5, 1.0, 2.0 (doubling per retry)
BACKOFF_MAX_SECONDS = 8.0  # cap for computed delays and for Retry-After
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

_FILE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+\.json$")

Clock = Callable[[], float]
Sleep = Callable[[float], None]


class SecClientError(Exception):
    """Base for every error raised by this module; ``retryable`` tells callers what to do."""

    retryable: bool = False

    def __init__(self, message: str, *, url: str) -> None:
        super().__init__(message)
        self.url = url


class SecUnavailable(SecClientError):
    """SEC did not answer usefully after bounded retries (429, transient 5xx, network error)."""

    retryable = True

    def __init__(
        self, url: str, *, status_code: int | None, retry_after: float | None, attempts: int
    ) -> None:
        cause = f"HTTP {status_code}" if status_code is not None else "a network error"
        advice = "retry in a few seconds"
        super().__init__(
            f"SEC EDGAR returned {cause} for {url} after {attempts} attempts; {advice}", url=url
        )
        self.status_code = status_code
        self.retry_after = retry_after
        self.attempts = attempts


class SecNotFound(SecClientError):
    """HTTP 404: the resource does not exist (e.g. no submissions file for that CIK)."""

    def __init__(self, url: str) -> None:
        super().__init__(f"SEC EDGAR has no resource at {url} (HTTP 404)", url=url)


class SecRequestError(SecClientError):
    """Non-retryable failure: another 4xx, or a response whose body is not JSON."""

    def __init__(self, url: str, *, status_code: int | None, detail: str | None = None) -> None:
        what = detail or f"HTTP {status_code}"
        super().__init__(f"SEC EDGAR request failed for {url}: {what}", url=url)
        self.status_code = status_code


def submissions_url(cik_padded: str) -> str:
    """URL of the submissions JSON for a CIK already zero-padded to 10 digits."""
    if len(cik_padded) != 10 or not cik_padded.isdigit():
        raise ValueError(f"cik_padded must be exactly 10 digits, got {cik_padded!r}")
    return f"{SUBMISSIONS_BASE_URL}CIK{cik_padded}.json"


def submissions_file_url(name: str) -> str:
    """URL of a history file named in ``filings.files[].name``; the name is untrusted."""
    if not _FILE_NAME_RE.fullmatch(name) or ".." in name:
        raise ValueError(f"history file name must be a single *.json path component, got {name!r}")
    return f"{SUBMISSIONS_BASE_URL}{name}"


def _parse_retry_after(value: str | None) -> float | None:
    """Seconds from a ``Retry-After`` header (integer form only), capped; None otherwise."""
    if value is None:
        return None
    value = value.strip()
    if not value.isdigit():
        return None  # HTTP-date form: fall back to the computed delay
    return min(float(value), BACKOFF_MAX_SECONDS)


class RateLimiter:
    """Sliding one-second window: at most ``max_per_second`` acquisitions per second."""

    def __init__(
        self, max_per_second: int, *, clock: Clock = time.monotonic, sleep: Sleep = time.sleep
    ) -> None:
        if max_per_second < 1:
            raise ValueError("max_per_second must be >= 1")
        self._clock = clock
        self._sleep = sleep
        self._stamps: deque[float] = deque(maxlen=max_per_second)
        self._lock = threading.Lock()

    def acquire(self) -> None:
        """Block until a slot is free, then record the acquisition. Thread-safe."""
        with self._lock:
            now = self._clock()
            if len(self._stamps) == self._stamps.maxlen:
                wait = self._stamps[0] + 1.0 - now
                if wait > 0:
                    self._sleep(wait)
                    now = self._clock()
            self._stamps.append(now)


@dataclass(slots=True)
class CacheEntry:
    value: Any
    expires_at: float
    etag: str | None
    last_modified: str | None


class ResponseCache:
    """In-memory per-process cache of parsed JSON keyed by URL. Not locked; the client locks."""

    def __init__(self, *, clock: Clock = time.monotonic) -> None:
        self._clock = clock
        self._entries: dict[str, CacheEntry] = {}

    def get(self, url: str) -> CacheEntry | None:
        """Return the entry even when stale; callers check :meth:`is_fresh`."""
        return self._entries.get(url)

    def is_fresh(self, entry: CacheEntry) -> bool:
        return entry.expires_at > self._clock()

    def put(
        self, url: str, value: Any, ttl_seconds: int, *, etag: str | None, last_modified: str | None
    ) -> None:
        self._entries[url] = CacheEntry(value, self._clock() + ttl_seconds, etag, last_modified)

    def touch(self, url: str, ttl_seconds: int) -> None:
        """Extend an entry's life after a 304 Not Modified."""
        entry = self._entries.get(url)
        if entry is not None:
            entry.expires_at = self._clock() + ttl_seconds

    def clear(self) -> None:
        self._entries.clear()


class SecClient:
    """Fair-access SEC HTTP client: headers, throttle, cache, bounded retries, typed errors."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.BaseTransport | None = None,
        clock: Clock = time.monotonic,
        sleep: Sleep = time.sleep,
    ) -> None:
        self._settings = settings
        self._sleep = sleep
        self._limiter = RateLimiter(settings.max_requests_per_second, clock=clock, sleep=sleep)
        self._cache = ResponseCache(clock=clock)
        self._lock = threading.Lock()
        self._http = httpx.Client(
            timeout=settings.timeout_seconds,
            follow_redirects=True,
            headers={"User-Agent": settings.user_agent, "Accept": "application/json"},
            transport=transport,
        )

    def fetch_ticker_map(self) -> Any:
        """``company_tickers_exchange.json`` (cached for ``ticker_map_ttl_seconds``)."""
        return self.get_json(TICKER_MAP_URL, ttl_seconds=self._settings.ticker_map_ttl_seconds)

    def fetch_submissions(self, cik_padded: str) -> Any:
        """``CIK##########.json`` for a 10-digit CIK (cached for ``submissions_ttl_seconds``)."""
        url = submissions_url(cik_padded)
        return self.get_json(url, ttl_seconds=self._settings.submissions_ttl_seconds)

    def fetch_submissions_file(self, name: str) -> Any:
        """A history file from ``filings.files[].name`` (cached for ``submissions_ttl_seconds``)."""
        url = submissions_file_url(name)
        return self.get_json(url, ttl_seconds=self._settings.submissions_ttl_seconds)

    def get_json(self, url: str, *, ttl_seconds: int) -> Any:
        """Parsed JSON for ``url``, served from cache while fresh; revalidated when stale."""
        with self._lock:
            entry = self._cache.get(url)
            if entry is not None and self._cache.is_fresh(entry):
                logger.debug("cache hit for %s", url)
                return entry.value
        headers: dict[str, str] = {}
        if entry is not None:
            if entry.etag:
                headers["If-None-Match"] = entry.etag
            if entry.last_modified:
                headers["If-Modified-Since"] = entry.last_modified
        logger.debug("fetching %s (%s)", url, "revalidate" if entry else "miss")
        response = self._request_with_retries(url, headers)
        if response.status_code == 304:
            if entry is None:
                raise SecRequestError(url, status_code=304, detail="304 without a cached entry")
            with self._lock:
                self._cache.touch(url, ttl_seconds)
            return entry.value
        try:
            value = response.json()
        except ValueError as exc:
            raise SecRequestError(
                url, status_code=response.status_code, detail="body is not JSON"
            ) from exc
        if ttl_seconds > 0:
            etag = response.headers.get("ETag")
            last_modified = response.headers.get("Last-Modified")
            with self._lock:
                self._cache.put(url, value, ttl_seconds, etag=etag, last_modified=last_modified)
        return value

    def clear_cache(self) -> None:
        with self._lock:
            self._cache.clear()

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> SecClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _request_with_retries(self, url: str, headers: dict[str, str]) -> httpx.Response:
        last_status: int | None = None
        last_retry_after: float | None = None
        for attempt in range(1, RETRY_ATTEMPTS + 1):
            self._limiter.acquire()  # every attempt, including retries
            retry_after: float | None = None
            try:
                response = self._http.get(url, headers=headers)
            except httpx.TransportError as exc:
                last_status, reason = None, type(exc).__name__
            else:
                status = response.status_code
                if status in (200, 304):
                    return response
                if status == 404:
                    raise SecNotFound(url)
                if status not in RETRYABLE_STATUS:
                    raise SecRequestError(url, status_code=status)
                last_status, reason = status, f"HTTP {status}"
                retry_after = _parse_retry_after(response.headers.get("Retry-After"))
                last_retry_after = retry_after
            if attempt == RETRY_ATTEMPTS:
                break
            delay = min(BACKOFF_BASE_SECONDS * 2 ** (attempt - 1), BACKOFF_MAX_SECONDS)
            if retry_after is not None:
                delay = retry_after
            logger.warning(
                "SEC request failed (%s) for %s; attempt %d/%d, retrying in %.1fs",
                reason,
                url,
                attempt,
                RETRY_ATTEMPTS,
                delay,
            )
            self._sleep(delay)
        logger.error(
            "SEC unavailable for %s after %d attempts (last: %s)", url, RETRY_ATTEMPTS, last_status
        )
        raise SecUnavailable(
            url, status_code=last_status, retry_after=last_retry_after, attempts=RETRY_ATTEMPTS
        )
