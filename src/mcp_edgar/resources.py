"""Read-only ``edgar://`` resources (req_006 v6.01).

A host reads a well-defined record by URI instead of asking the model to call a tool (guide v2
§8). Every payload is built by the same data-layer path as the tools (``TickerResolver``,
``FilingsService.latest_filing``, ``to_record``, ``get_catalogue``), so a resource and its tool
always agree. Anticipated failures are protocol errors: ``ResourceNotFoundError`` (JSON-RPC
``-32602``) when the URI names no record, ``ResourceError`` (``-32603``) when a record exists but
cannot be served now; the message texts are those of ``mcp_edgar.outcomes``. Anything else is a
crash, as in the tools. ``register`` is called once by ``server.py`` (no import cycle: this module
never imports ``server``). stdout is the MCP wire: ``logging`` only (gotchas #1).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ResourceError, ResourceNotFoundError

from mcp_edgar.edgar_urls import to_record
from mcp_edgar.filings import Filing
from mcp_edgar.form_types import FormTypeCatalogue, get_catalogue
from mcp_edgar.outcomes import STATUS_INVALID_INPUT, STATUS_NOT_FOUND, outcome_for_exception
from mcp_edgar.tickers import AmbiguousTicker, Issuer, format_cik

if TYPE_CHECKING:
    from mcp_edgar.server import Services

logger = logging.getLogger(__name__)

JSON = "application/json"
COMPANY_PROFILE = "edgar://companies/{ticker}/profile"
COMPANY_LATEST = "edgar://companies/{ticker}/latest"
ISSUER_PROFILE = "edgar://issuers/{cik}/profile"
ISSUER_LATEST = "edgar://issuers/{cik}/latest"
FORM_TYPES = "edgar://form-types"
TEMPLATE_URIS = (COMPANY_PROFILE, COMPANY_LATEST, ISSUER_PROFILE, ISSUER_LATEST)

Payload = dict[str, Any]


def profile_payload(issuer: Issuer, listed: Sequence[Issuer]) -> Payload:
    """``Issuer.as_dict()`` plus ``tickers``: every symbol under the CIK in SEC file order.

    The resolved ``ticker`` is always present (appended only if ``listed`` lacks it).
    """
    tickers = list(dict.fromkeys([*(entry.ticker for entry in listed), issuer.ticker]))
    return {**issuer.as_dict(), "tickers": tickers}


def latest_payload(issuer: Issuer, filing: Filing | None) -> Payload:
    """The tool's ``company`` and ``filing`` values, nothing else (spec §7 Q4)."""
    return {"company": issuer.as_dict(), "filing": to_record(filing) if filing else None}


def form_types_payload(catalogue: FormTypeCatalogue) -> Payload:
    """The whole bundled catalogue: snapshot metadata plus every entry in catalogue order."""
    return {
        "catalogue": catalogue.metadata(),
        "form_types": [entry.as_dict() for entry in catalogue.entries],
    }


def resource_error_for(exc: Exception, *, kind: str) -> ResourceError | None:
    """Protocol error for an anticipated data-layer exception, or ``None`` to re-raise.

    ``kind`` (``profile`` or ``latest``) names the alternative ``edgar://issuers/{cik}/...``
    template in the ambiguity message. Texts come from ``outcome_for_exception`` so tools and
    resources share one vocabulary.
    """
    if isinstance(exc, AmbiguousTicker):
        options = ", ".join(
            f"{c.cik} {c.name} ({c.exchange or 'no exchange'})" for c in exc.candidates
        )
        return ResourceError(f"{exc}; read edgar://issuers/<cik>/{kind} for one of: {options}")
    outcome = outcome_for_exception(exc)
    if outcome is None:
        return None
    message = outcome["message"]
    if outcome["status"] in (STATUS_INVALID_INPUT, STATUS_NOT_FOUND):
        return ResourceNotFoundError(message)
    if not outcome.get("retryable", True):
        message = f"{message}; not retryable"  # a retryable message already says how to retry
    return ResourceError(message)


def _read(name: str, argument: str, kind: str, compute: Callable[[], Payload]) -> Payload:
    """Run ``compute``; translate anticipated failures; log one line per read; never print."""
    try:
        payload = compute()
    except Exception as exc:
        error = resource_error_for(exc, kind=kind)
        if error is None:
            raise  # a crash: the SDK logs the traceback and the client sees a generic error
        logger.info("resource %s %r -> %s", name, argument, type(error).__name__)
        raise error from exc
    logger.info("resource %s %r -> ok", name, argument)
    return payload


def _issuers_for_cik(services: Services, cik: str) -> tuple[Issuer, ...]:
    """The SEC-map rows for a CIK segment; ``ResourceNotFoundError`` when malformed or unlisted."""
    try:
        padded = format_cik(cik)
    except ValueError as exc:
        raise ResourceNotFoundError(f"cik must be 1-10 digits, got {cik!r}") from exc
    listed = services.resolver.issuers_for_cik(padded)
    if not listed:
        raise ResourceNotFoundError(
            f"no SEC issuer with CIK {padded} is listed in the SEC ticker map"
        )
    return listed


def register(mcp: MCPServer, get_services: Callable[[], Services]) -> None:
    """Declare the five resources on ``mcp``; ``get_services`` is looked up at read time."""

    @mcp.resource(COMPANY_PROFILE, mime_type=JSON, title="Issuer profile by ticker")
    def company_profile(ticker: str) -> Payload:
        """Issuer identity for a stock ticker: CIK, name, exchange and all its tickers."""

        def compute() -> Payload:
            services = get_services()
            issuer = services.resolver.resolve(ticker)
            return profile_payload(issuer, services.resolver.issuers_for_cik(issuer.cik))

        return _read("company_profile", ticker, "profile", compute)

    @mcp.resource(COMPANY_LATEST, mime_type=JSON, title="Latest SEC filing by ticker")
    def company_latest_filing(ticker: str) -> Payload:
        """Newest SEC filing for a stock ticker with EDGAR links; the get_latest_filing record."""

        def compute() -> Payload:
            services = get_services()
            issuer = services.resolver.resolve(ticker)
            return latest_payload(issuer, services.filings.latest_filing(issuer))

        return _read("company_latest_filing", ticker, "latest", compute)

    @mcp.resource(ISSUER_PROFILE, mime_type=JSON, title="Issuer profile by CIK")
    def issuer_profile(cik: str) -> Payload:
        """Issuer identity for an SEC CIK in the ticker map: name, exchange and all tickers."""

        def compute() -> Payload:
            listed = _issuers_for_cik(get_services(), cik)
            return profile_payload(listed[0], listed)

        return _read("issuer_profile", cik, "profile", compute)

    @mcp.resource(ISSUER_LATEST, mime_type=JSON, title="Latest SEC filing by CIK")
    def issuer_latest_filing(cik: str) -> Payload:
        """Newest SEC filing for an SEC CIK in the ticker map, with official EDGAR links."""

        def compute() -> Payload:
            services = get_services()
            listed = _issuers_for_cik(services, cik)
            return latest_payload(listed[0], services.filings.latest_filing(listed[0]))

        return _read("issuer_latest_filing", cik, "latest", compute)

    @mcp.resource(FORM_TYPES, mime_type=JSON, title="EDGAR form-type catalogue")
    def form_types_catalogue() -> Payload:
        """Every EDGAR submission-type code filed in recent quarters, with counts (snapshot)."""

        def compute() -> Payload:
            return form_types_payload(get_catalogue())

        return _read("form_types_catalogue", FORM_TYPES, "profile", compute)
