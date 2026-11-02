"""User-selected prompts (req_006 v6.02): guided workflows over the tools.

A prompt is a reusable message a person picks from the client's menu (guide v2 §9). Each one here
renders a single ``user`` message that tells the model which tool to call, how to page, what to
cite, the ``filing_date`` vs ``report_date`` rule, how to recover from an ``ambiguous`` result, and
the shared ``GUARDRAILS`` (metadata and official links only, no filing contents, no investment
advice). Rendering is a pure function of its string arguments: no live data, no I/O, no
validation (the tools validate). ``register`` is called once by ``server.py``; this module never
imports ``server``. stdout is the MCP wire: ``logging`` only (gotchas #1).
"""

from __future__ import annotations

import logging
from typing import Annotated

from mcp.server import MCPServer
from pydantic import Field

logger = logging.getLogger(__name__)

# Argument descriptions reach clients through `Annotated[str, Field(description=...)]`; a
# docstring `Args:` section is not read by the SDK (gotchas #22; req_006 v6.03).
Ticker = Annotated[str, Field(description="Stock ticker symbol, e.g. AAPL; case does not matter.")]
Year = Annotated[str, Field(description="Calendar year of the filing date, e.g. 2025.")]
Query = Annotated[
    str,
    Field(
        description="Optional text matched against form codes and descriptions, e.g. 10-K or 13G."
    ),
]

GUARDRAILS = (
    "This server returns filing metadata and official sec.gov links only; it does not read or "
    "summarise filing contents, so base any statement about a filing's contents on the linked "
    "documents and say when you have not read them. Cite the EDGAR link for every filing you "
    "mention. Do not give investment advice or a buy or sell recommendation. If a tool answers "
    'with status "ambiguous", list the candidates to the user, ask which issuer they mean, and '
    "call again with that candidate's cik (or read edgar://issuers/{cik}/latest)."
)


def normalize_ticker_text(ticker: str) -> str:
    """Strip and upper-case for readability; never validates, never raises."""
    return ticker.strip().upper()


def _join(*paragraphs: str) -> str:
    return "\n\n".join(paragraphs)


def render_latest_filing(ticker: str) -> str:
    """The ``review_latest_filing`` message (spec §4.1)."""
    symbol = normalize_ticker_text(ticker)
    return _join(
        f"Review the latest SEC filing of {symbol}.",
        f'First call get_latest_filing with ticker "{symbol}" to identify the filing. If you '
        'need a particular kind of filing, pass forms (for example ["10-K"]). Use the '
        "filing_url (the EDGAR filing index page) and, when present, primary_document_url as "
        "the official sources.",
        "Cite the form type and filing_date in your answer, and link the filing. report_date is "
        "the period the filing covers, not when it was filed. Separate what the filing states "
        "from your interpretation, and say which is which.",
        GUARDRAILS,
    )


def render_filings_by_year(ticker: str, year: str) -> str:
    """The ``review_filings_by_year`` message (spec §4.1)."""
    symbol, when = normalize_ticker_text(ticker), year.strip()
    return _join(
        f"Review the SEC filings of {symbol} filed during calendar year {when}.",
        f'Call list_filings_by_year with ticker "{symbol}" and year {when}; retrieve every page '
        "by calling again with offset set to the previous result's next_offset until it is "
        "null. Group the filings by form type, summarise the sequence over the year, and point "
        "out which filings may merit closer reading. Link every filing you mention with its "
        "filing_url.",
        "The year selects filings by filing_date (when the SEC received them); report_date is "
        "the period a filing covers and may fall in another year. Do not infer facts from a form "
        "type alone; say what the form is for and point to the filing.",
        GUARDRAILS,
    )


def render_form_types(query: str = "") -> str:
    """The ``explain_form_types`` message (spec §4.1); the query clauses are optional."""
    text = query.strip()
    matching = f' matching "{text}"' if text else ""
    with_query = f' with query "{text}"' if text else ""
    return _join(
        f"Explain the EDGAR form types{matching}.",
        f"Call list_form_types{with_query}; page with offset set to next_offset until it is "
        "null if you need the whole list. For the most common codes, explain what the form is "
        "for and when a company files it. Codes ending in /A are amendments of the base form. "
        "The description and topics of a code come from the SEC Forms List when available and "
        "may be null; the filings count covers the quarters named in catalogue.quarters, and "
        "catalogue.generated_on says how current the list is. These codes are exactly the "
        "values the forms argument of get_latest_filing and list_filings_by_year accepts.",
        GUARDRAILS,
    )


def register(mcp: MCPServer) -> None:
    """Declare the three prompts on ``mcp``. Docstrings are one line: the SDK ships them raw."""

    @mcp.prompt(title="Review latest SEC filing")
    def review_latest_filing(ticker: Ticker) -> str:
        """Guide a review of a company's latest SEC filing (by ticker) using get_latest_filing."""
        logger.info("prompt review_latest_filing args=%r", (ticker,))
        return render_latest_filing(ticker)

    @mcp.prompt(title="Review filings for a calendar year")
    def review_filings_by_year(ticker: Ticker, year: Year) -> str:
        """Guide a review of a company's SEC filings in a calendar year via list_filings_by_year."""
        logger.info("prompt review_filings_by_year args=%r", (ticker, year))
        return render_filings_by_year(ticker, year)

    @mcp.prompt(title="Explain EDGAR form types")
    def explain_form_types(query: Query = "") -> str:
        """Explain EDGAR form types, optionally those matching a query, using list_form_types."""
        logger.info("prompt explain_form_types args=%r", (query,))
        return render_form_types(query)
