"""MCP server entry point for the SEC EDGAR MCP Server.

Declares the three tools (`get_latest_filing`, `list_filings_by_year`, `list_form_types`) on one
`MCPServer`, registers the `edgar://` resources of `mcp_edgar.resources` and the prompts of
`mcp_edgar.prompts`, wires the data layer
once per process, and runs over stdio. Tools return the structured outcomes of
`mcp_edgar.outcomes` and never raise for anticipated conditions. Over stdio, stdout carries the
MCP wire; never print here (reqspecs/references/common/gotchas.md #1).
No `from __future__ import annotations` here: `mcp run`/`mcp dev` import this file by path without
registering it in `sys.modules`, and a dataclass with string annotations then fails (gotchas #18).
"""

import functools
import inspect
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from mcp_edgar import __version__, prompts, resources
from mcp_edgar.edgar_urls import to_record, to_records
from mcp_edgar.filings import (
    FilingsService,
    normalize_forms,
    paginate,
    validate_limit,
    validate_offset,
    validate_year,
)
from mcp_edgar.form_types import get_catalogue, validate_query
from mcp_edgar.outcomes import (
    latest_filing_ok,
    list_filings_ok,
    list_form_types_ok,
    outcome_for_exception,
)
from mcp_edgar.sec_client import SecClient
from mcp_edgar.settings import (
    DEFAULT_PAGE_CAP,
    Settings,
    SettingsError,
    configure_logging,
    get_settings,
)
from mcp_edgar.tickers import TickerResolver

logger = logging.getLogger(__name__)

# Sent once at initialize. Carries the vocabulary shared by every tool (the status set and what
# each status adds) so the per-call tool descriptions stay short; they keep the status names and
# point here for the meaning. Bound: < 1000 characters (tests/test_surface.py S3).
SERVER_INSTRUCTIONS = (
    "Read-only SEC EDGAR filing metadata for US-listed companies, by stock ticker. Tools: "
    "get_latest_filing returns the newest filing, optionally of given forms; "
    "list_filings_by_year returns one calendar year, paged by offset=next_offset until null; "
    "list_form_types lists the codes to pass as forms. Years refer to filing_date (when the "
    "SEC received the filing), not report_date. Results carry official sec.gov links; the "
    "server never reads filing contents. Each result has status and message: ok; not_found "
    "(no issuer for the ticker); ambiguous (candidates lists the issuers; call again with "
    "the chosen cik); invalid_input (field names the bad argument); upstream_error (SEC did "
    "not answer; retryable, retry_after_seconds). Resources: edgar://companies/{ticker}/profile "
    "and edgar://companies/{ticker}/latest (or edgar://issuers/{cik}/...) give the tools' "
    "records; edgar://form-types is the catalogue. Prompts: review_latest_filing, "
    "review_filings_by_year and explain_form_types are guided workflows."
)

mcp = MCPServer(name="SEC EDGAR Filings", version=__version__, instructions=SERVER_INSTRUCTIONS)

READ_ONLY = ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=True)

F = TypeVar("F", bound=Callable[..., Any])


def edgar_tool(title: str) -> Callable[[F], F]:
    """Register a read-only tool whose wire description is the dedented docstring.

    The SDK would otherwise ship ``fn.__doc__`` verbatim, indentation included (gotchas #19).
    """

    def register(fn: F) -> F:
        mcp.add_tool(fn, title=title, description=inspect.getdoc(fn), annotations=READ_ONLY)
        return fn

    return register


@dataclass(frozen=True, slots=True)
class Services:
    """The per-process data-layer objects every tool call shares."""

    settings: Settings
    client: SecClient
    resolver: TickerResolver
    filings: FilingsService

    @classmethod
    def build(cls, settings: Settings, *, client: SecClient | None = None) -> "Services":
        """Wire the data layer for ``settings``; ``client`` lets tests inject a mock transport."""
        client = client if client is not None else SecClient(settings)
        return cls(settings, client, TickerResolver(client), FilingsService(client, settings))


@functools.lru_cache(maxsize=1)
def get_services() -> Services:
    """Process-wide services, built on the first tool call (``mcp dev`` never calls ``main``)."""
    return Services.build(get_settings())


def reset_services() -> None:
    """Forget the cached services (tests only)."""
    get_services.cache_clear()


@edgar_tool("Latest SEC filing")
def get_latest_filing(
    ticker: str, forms: list[str] | None = None, cik: str | int | None = None
) -> dict[str, Any]:
    """Newest SEC filing for a stock ticker, with official EDGAR links.

    Resolves the ticker to its SEC CIK and returns the filing with the latest `filing_date`
    (the date the SEC received it). `forms` restricts the search to exact form types,
    case-insensitive: `["10-K"]` matches `10-K` but not `10-K/A`; omit it to consider every
    form. `report_date` (the period the filing covers) is returned as context only, so a 10-K
    filed in January 2026 for the fiscal year ending September 2025 has a 2026 `filing_date`.

    Args:
        ticker: Stock ticker symbol, e.g. `"AAPL"`. Case and surrounding whitespace are
            ignored; `BRK.B` and `BRK-B` are equivalent.
        forms: Optional list of SEC form types, e.g. `["10-K", "10-Q"]`.
        cik: Optional SEC CIK (1-10 digits, string or integer) to pick one issuer when a
            previous call returned `"ambiguous"`; it must be listed under `ticker`.

    Returns:
        A JSON object with `status` and a one-line `message`. On `"ok"`: `company` {cik, name,
        ticker, exchange}, `forms` (normalised list or null) and `filing` {form, filing_date,
        report_date, accession_number, filing_url, primary_document_url}, or `filing` null when
        the company has no filing of those forms; `filing_url` (the filing index page) is
        always present, `primary_document_url` may be null. The other statuses (`not_found`,
        `ambiguous` with `candidates`, `invalid_input` with `field`, `upstream_error` with
        `retryable`) are defined in the server instructions.

    Example:
        `get_latest_filing("AAPL", ["10-K"])` returns Apple's most recent annual report.
    """
    services = get_services()
    try:
        normalized = normalize_forms(forms)  # validated before any network call
        issuer = services.resolver.resolve(ticker, cik=cik)  # ticker/cik checks precede I/O
        filing = services.filings.latest_filing(issuer, forms=normalized)
        outcome = latest_filing_ok(issuer, normalized, to_record(filing) if filing else None)
    except Exception as exc:
        outcome = outcome_for_exception(exc)
        if outcome is None:
            raise  # a crash: the SDK logs the traceback and the client sees a generic error
    logger.info(
        "get_latest_filing ticker=%r forms=%r cik=%r -> %s", ticker, forms, cik, outcome["status"]
    )
    return outcome


@edgar_tool("Filings by year")
def list_filings_by_year(
    ticker: str,
    year: int,
    forms: list[str] | None = None,
    limit: int = DEFAULT_PAGE_CAP,
    offset: int = 0,
    cik: str | int | None = None,
) -> dict[str, Any]:
    """Filings a company submitted to the SEC during one calendar year, newest first, paged.

    `year` selects filings by `filing_date`, the date the SEC received the filing, not by
    `report_date` (the period the filing covers): a 10-K for the fiscal year ending September
    2025 that was filed in January 2026 belongs to `year=2026`. `forms` restricts the list to
    exact form types, case-insensitive (`["10-K"]` matches `10-K` but not `10-K/A`). Older
    years are served from the SEC's history files automatically.

    Args:
        ticker: Stock ticker symbol, e.g. `"AAPL"`; case and surrounding whitespace are ignored.
        year: Calendar year of the filing date, 1993 or later; a future year returns an empty list.
        forms: Optional list of SEC form types, e.g. `["10-K", "10-Q"]`; omit it for every form.
        limit: Page size (default 100); larger values are clamped to the server's cap.
        offset: Filings to skip, for paging; pass the previous result's `next_offset`.
        cik: Optional SEC CIK (1-10 digits, string or integer) to pick one issuer when a
            previous call returned `"ambiguous"`; it must be listed under `ticker`.

    Returns:
        A JSON object with `status` and a one-line `message`. On `"ok"`: `company` {cik, name,
        ticker, exchange}, `year`, `forms`, `limit` (the effective page size), `offset`,
        `total_count`, `returned_count`, `next_offset` (null on the last page) and `filings`
        (newest first; each has form, filing_date, report_date, accession_number, filing_url,
        primary_document_url); an empty year is `"ok"` with `total_count` 0. Page with
        `offset=next_offset` until null. The other statuses (`not_found`, `ambiguous` with
        `candidates`, `invalid_input` with `field`, `upstream_error` with `retryable`) are
        defined in the server instructions.

    Example:
        `list_filings_by_year("AAPL", 2025, ["10-K", "10-Q"])` lists Apple's 2025 reports.
    """
    services = get_services()
    try:
        valid_year = validate_year(year)  # the four checks precede any network call
        normalized = normalize_forms(forms)
        page_limit = validate_limit(limit, page_cap=services.settings.page_cap)
        start = validate_offset(offset)
        issuer = services.resolver.resolve(ticker, cik=cik)
        page = services.filings.list_filings_by_year(
            issuer, valid_year, forms=normalized, limit=page_limit, offset=start
        )
        outcome = list_filings_ok(page, to_records(page.filings))
    except Exception as exc:
        outcome = outcome_for_exception(exc)
        if outcome is None:
            raise
    logger.info(
        "list_filings_by_year args=%r -> %s",
        (ticker, year, forms, limit, offset, cik),
        outcome["status"],
    )
    return outcome


@edgar_tool("EDGAR form types")
def list_form_types(
    query: str | None = None, limit: int = DEFAULT_PAGE_CAP, offset: int = 0
) -> dict[str, Any]:
    """EDGAR form types (submission-type codes) filed in recent quarters, most common first.

    Use this to learn which values the `forms` argument of `get_latest_filing` and
    `list_filings_by_year` accepts: `form` is exactly the code EDGAR uses (`10-K`, `10-K/A`,
    `SCHEDULE 13G/A`, `424B2`, `NPORT-P`). The list is a snapshot bundled with the server (see
    `catalogue.generated_on` and `catalogue.quarters`); `filings` counts filings of that type
    over those quarters; `description` and `topics` come from the SEC Forms List when the form
    number is known, otherwise null. Amendments end with `/A`. No request is made to the SEC.

    Args:
        query: Optional case-insensitive text matched against the code and description
            (`"10-K"` lists `10-K`, `10-K/A`, `10-KT`, ...; `"/A"` lists amendments).
        limit: Maximum entries per page (default 100; clamped to the server's cap).
        offset: Entries to skip, for paging; pass the previous result's `next_offset`.

    Returns:
        A JSON object with `status` and a one-line `message`. On `"ok"`: `query`, `limit`,
        `offset`, `total_count`, `returned_count`, `next_offset` (null on the last page),
        `catalogue` {generated_on, quarters, sources} and `form_types` (each: form,
        description, amendment, filings, topics). `"invalid_input"` carries `field`, as
        defined in the server instructions; no other status occurs.

    Example:
        `list_form_types("13G")` lists the Schedule 13G submission types and their counts.
    """
    services = get_services()
    try:
        text = validate_query(query)  # all checks precede any work; no network is involved
        page_limit = validate_limit(limit, page_cap=services.settings.page_cap)
        start = validate_offset(offset)
        catalogue = get_catalogue()
        matches = catalogue.search(text)
        page, next_offset = paginate(matches, offset=start, limit=page_limit)
        outcome = list_form_types_ok(
            catalogue, text, page_limit, start, len(matches), page, next_offset
        )
    except Exception as exc:
        outcome = outcome_for_exception(exc)
        if outcome is None:
            raise
    logger.info("list_form_types args=%r -> %s", (query, limit, offset), outcome["status"])
    return outcome


# edgar:// resources (req_006 v6.01). The lambda defers the lookup of `get_services` to read
# time, so tests that monkeypatch it keep working. Prompts (req_006 v6.02) need no services.
resources.register(mcp, lambda: get_services())
prompts.register(mcp)


def main() -> None:
    """Load settings, route logging to stderr, and run the server over stdio.

    Console script `mcp-server-edgar`. A bad environment variable exits with status 1 and a
    one-line message on stderr.
    """
    try:
        settings = get_settings()
    except SettingsError as exc:
        raise SystemExit(f"configuration error: {exc}") from exc
    configure_logging(settings.log_level)
    logger.info(
        "mcp-server-edgar %s starting over stdio (log level %s)",
        __version__,
        settings.log_level,
    )
    mcp.run()


if __name__ == "__main__":
    main()
