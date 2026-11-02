"""Tests for mcp_edgar.prompts: listing, rendering, optional query, guardrails, no live data.

The ``fake_services`` fixture is imported by name from ``tests.test_server`` (pytest registers
imported fixtures); ruff reads the fixture parameter as a redefinition, hence the F811 waiver.
"""

# ruff: noqa: F811

import asyncio
import logging
import re
from typing import Any

import pytest
from mcp.types import TextContent

from mcp_edgar import server
from mcp_edgar.prompts import (
    GUARDRAILS,
    normalize_ticker_text,
    render_filings_by_year,
    render_form_types,
    render_latest_filing,
)
from tests.test_server import ServicesFactory, fake_services  # noqa: F401  (fixture by import)

NAMES = ["review_latest_filing", "review_filings_by_year", "explain_form_types"]
TITLES = [
    "Review latest SEC filing",
    "Review filings for a calendar year",
    "Explain EDGAR form types",
]
ARGUMENTS = [[("ticker", True)], [("ticker", True), ("year", True)], [("query", False)]]
ACCESSION_RE = re.compile(r"\d{10}-\d{2}-\d{6}")
LONG_DIGITS_RE = re.compile(r"\d{7,}")
YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")


def list_prompts() -> list[Any]:
    return asyncio.run(server.mcp.list_prompts())


def get_prompt(name: str, arguments: dict[str, str] | None) -> Any:
    return asyncio.run(server.mcp.get_prompt(name, arguments))


def rendered(name: str, arguments: dict[str, str] | None) -> str:
    result = get_prompt(name, arguments)
    assert len(result.messages) == 1
    message = result.messages[0]
    assert message.role == "user"
    assert isinstance(message.content, TextContent)
    return message.content.text


# --- listing --------------------------------------------------------------------------------


def test_three_prompts_are_listed_in_order() -> None:
    prompts = list_prompts()
    assert [p.name for p in prompts] == NAMES
    assert [p.title for p in prompts] == TITLES
    assert [[(a.name, a.required) for a in p.arguments or []] for p in prompts] == ARGUMENTS


def test_descriptions_are_one_line_docstrings() -> None:
    for prompt in list_prompts():
        description = prompt.description or ""
        assert description.endswith(".") and "\n" not in description
        assert not description.startswith(" ") and len(description) <= 100
        assert prompt.name.split("_")[0] in ("review", "explain")


def test_every_argument_is_described() -> None:
    """v6.03: `Annotated[str, Field(description=...)]` becomes the argument description."""
    for prompt in list_prompts():
        for argument in prompt.arguments or []:
            description = argument.description or ""
            assert description.endswith(".") and 0 < len(description) <= 100
            assert "\n" not in description and not description.startswith(" ")


@pytest.mark.parametrize(
    ("name", "argument", "example"),
    [
        ("review_latest_filing", "ticker", "AAPL"),
        ("review_filings_by_year", "ticker", "AAPL"),
        ("review_filings_by_year", "year", "2025"),
        ("explain_form_types", "query", "10-K"),
    ],
)
def test_argument_descriptions_carry_an_example(name: str, argument: str, example: str) -> None:
    prompt = next(p for p in list_prompts() if p.name == name)
    description = next(a.description for a in prompt.arguments or [] if a.name == argument)
    assert example in (description or "")


# --- rendering ------------------------------------------------------------------------------


LATEST_TOKENS = (
    "AAPL",
    "get_latest_filing",
    "forms",
    "filing_url",
    "primary_document_url",
    "filing_date",
    "report_date",
    "interpretation",
)
YEAR_TOKENS = (
    "MSFT",
    "2025",
    "list_filings_by_year",
    "offset",
    "next_offset",
    "null",
    "filing_date",
    "report_date",
    "form type",
    "closer reading",
)
FORM_TYPES_TOKENS = ("list_form_types", "/A", "catalogue.generated_on", "forms argument", "null")


def test_latest_filing_prompt_renders_one_user_message() -> None:
    result = get_prompt("review_latest_filing", {"ticker": "aapl"})
    listed = next(p for p in list_prompts() if p.name == "review_latest_filing")
    assert result.description == listed.description
    text = rendered("review_latest_filing", {"ticker": "aapl"})
    assert text.startswith("Review the latest SEC filing of AAPL.")
    assert "aapl" not in text
    assert text.endswith(GUARDRAILS)


@pytest.mark.parametrize("token", LATEST_TOKENS)
def test_latest_filing_prompt_states_the_contract(token: str) -> None:
    assert token in rendered("review_latest_filing", {"ticker": "aapl"})


def test_year_prompt_renders_one_user_message() -> None:
    text = rendered("review_filings_by_year", {"ticker": " msft ", "year": " 2025 "})
    assert text.startswith("Review the SEC filings of MSFT filed during calendar year 2025.")
    assert text.endswith(GUARDRAILS)


@pytest.mark.parametrize("token", YEAR_TOKENS)
def test_year_prompt_states_the_contract(token: str) -> None:
    assert token in rendered("review_filings_by_year", {"ticker": " msft ", "year": " 2025 "})


@pytest.mark.parametrize("token", FORM_TYPES_TOKENS)
def test_form_types_prompt_states_the_contract(token: str) -> None:
    assert token in rendered("explain_form_types", {})


@pytest.mark.parametrize("arguments", [{}, None, {"query": ""}, {"query": "   "}])
def test_form_types_prompt_without_a_query(arguments: dict[str, str] | None) -> None:
    text = rendered("explain_form_types", arguments)
    assert text == render_form_types()
    for token in ("list_form_types", "/A", "catalogue.generated_on", "forms argument"):
        assert token in text
    assert 'query "' not in text and "matching" not in text
    assert text.endswith(GUARDRAILS)


def test_form_types_prompt_with_a_query() -> None:
    text = rendered("explain_form_types", {"query": " 13G "})
    assert 'Explain the EDGAR form types matching "13G".' in text
    assert 'Call list_form_types with query "13G";' in text


def test_guardrails_are_identical_in_every_prompt() -> None:
    texts = [
        rendered("review_latest_filing", {"ticker": "AAPL"}),
        rendered("review_filings_by_year", {"ticker": "AAPL", "year": "2025"}),
        rendered("explain_form_types", {"query": "13G"}),
    ]
    assert all(text.endswith("\n\n" + GUARDRAILS) for text in texts)
    for token in ("official sec.gov links only", "Do not give investment advice", "ambiguous"):
        assert token in GUARDRAILS
    assert "edgar://issuers/{cik}/latest" in GUARDRAILS and "cik" in GUARDRAILS


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("review_latest_filing", {"ticker": "AAPL"}),
        ("review_filings_by_year", {"ticker": "AAPL", "year": "2025"}),
        ("explain_form_types", {"query": "13G"}),
        ("explain_form_types", {}),
    ],
)
def test_prompts_carry_no_live_data(name: str, arguments: dict[str, str]) -> None:
    text = rendered(name, arguments)
    assert "http" not in text and "sec.gov/Archives" not in text
    assert ACCESSION_RE.search(text) is None
    assert LONG_DIGITS_RE.search(text) is None  # no CIKs
    years = set(YEAR_RE.findall(text))
    assert years == set() if "year" not in arguments else True
    if "year" in arguments:
        assert {m.group(0) for m in YEAR_RE.finditer(text)} == {arguments["year"]}


def test_prompts_make_no_requests(fake_services: ServicesFactory) -> None:
    _, recorded, _ = fake_services({})
    rendered("review_latest_filing", {"ticker": "AAPL"})
    rendered("review_filings_by_year", {"ticker": "AAPL", "year": "2025"})
    rendered("explain_form_types", {})
    assert recorded.requests == []


# --- renderers ------------------------------------------------------------------------------


@pytest.mark.parametrize(("raw", "expected"), [(" brk.b ", "BRK.B"), ("", ""), ("AAPL", "AAPL")])
def test_normalize_ticker_text(raw: str, expected: str) -> None:
    assert normalize_ticker_text(raw) == expected


def test_renderers_are_pure_and_match_the_prompts() -> None:
    assert render_latest_filing("aapl") == render_latest_filing(" AAPL ")
    assert render_latest_filing("aapl") == rendered("review_latest_filing", {"ticker": "aapl"})
    assert render_filings_by_year("aapl", "2025") == rendered(
        "review_filings_by_year", {"ticker": "aapl", "year": "2025"}
    )
    assert render_form_types("13G") == rendered("explain_form_types", {"query": "13G"})
    assert render_latest_filing("").startswith("Review the latest SEC filing of .")  # not validated


# --- errors ---------------------------------------------------------------------------------


def test_missing_required_argument_is_a_value_error() -> None:
    with pytest.raises(ValueError, match="Missing required arguments"):
        get_prompt("review_latest_filing", {})
    with pytest.raises(ValueError, match="Missing required arguments"):
        get_prompt("review_filings_by_year", {"ticker": "AAPL"})


def test_unknown_prompt_is_a_value_error() -> None:
    with pytest.raises(ValueError, match="Unknown prompt"):
        get_prompt("nope", {})


def test_unknown_argument_is_a_value_error() -> None:
    with pytest.raises(ValueError, match="Error rendering prompt"):
        get_prompt("review_latest_filing", {"ticker": "AAPL", "extra": "x"})


# --- logging and stdout ---------------------------------------------------------------------


def test_each_render_logs_one_line(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="mcp_edgar.prompts")
    rendered("review_latest_filing", {"ticker": "AAPL"})
    rendered("explain_form_types", {})
    messages = [r.getMessage() for r in caplog.records if r.name == "mcp_edgar.prompts"]
    assert messages == [
        "prompt review_latest_filing args=('AAPL',)",
        "prompt explain_form_types args=('',)",
    ]


def test_renders_write_nothing_to_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    rendered("review_latest_filing", {"ticker": "AAPL"})
    rendered("review_filings_by_year", {"ticker": "AAPL", "year": "2025"})
    rendered("explain_form_types", {"query": "13G"})
    assert capsys.readouterr().out == ""
