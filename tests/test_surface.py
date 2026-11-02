"""Surface review (req_006 v6.03, spec §4.4 S1–S6): the nine primitives stay consistent.

Three tools, one static resource, four resource templates and three prompts must keep distinct
titles, one-paragraph descriptions, instructions that name every one of them, described prompt
arguments, and a shared vocabulary (the words the prompts use exist on the tools). Everything is
in-process: no network, no fixtures.
"""

import asyncio
from typing import Any

import pytest

from mcp_edgar import server
from mcp_edgar.prompts import GUARDRAILS
from mcp_edgar.resources import FORM_TYPES, TEMPLATE_URIS
from tests.test_server import FORM_KEYS, LIST_KEYS, OK_KEYS, TOOL_NAMES

VOCABULARY = (
    "forms",
    "offset",
    "next_offset",
    "filing_url",
    "primary_document_url",
    "filing_date",
    "report_date",
    "cik",
    "ambiguous",
    "catalogue.generated_on",
    "catalogue.quarters",
)
ENVELOPE_KEYS = set(OK_KEYS) | set(LIST_KEYS) | set(FORM_KEYS)


def surface() -> tuple[list[Any], list[Any], list[Any], list[Any]]:
    return (
        asyncio.run(server.mcp.list_tools()),
        asyncio.run(server.mcp.list_resources()),
        asyncio.run(server.mcp.list_resource_templates()),
        asyncio.run(server.mcp.list_prompts()),
    )


def tool_descriptions() -> str:
    return " ".join(tool.description or "" for tool in asyncio.run(server.mcp.list_tools()))


def test_s1_every_primitive_has_a_distinct_title() -> None:
    tools, resources, templates, prompts = surface()
    titles = [item.title for group in (tools, resources, templates, prompts) for item in group]
    assert len(titles) == 11 and all(titles)
    assert len(set(titles)) == len(titles)


def test_s2_descriptions_are_one_paragraph_at_column_zero() -> None:
    tools, resources, templates, prompts = surface()
    for item in [*resources, *templates, *prompts]:
        description = item.description or ""
        assert description and "\n" not in description and not description.startswith(" ")
    for tool in tools:
        lines = (tool.description or "").splitlines()
        assert len(lines) > 2 and not lines[0].startswith(" ") and not lines[2].startswith(" ")


def test_s3_instructions_name_every_primitive() -> None:
    _, _, _, prompts = surface()
    text = server.SERVER_INSTRUCTIONS
    for name in TOOL_NAMES:
        assert name in text
    for family in ("edgar://companies/{ticker}", "edgar://issuers/{cik}", FORM_TYPES):
        assert family in text
    for prompt in prompts:
        assert prompt.name in text
    assert len(text) < 1000


def test_s4_every_prompt_argument_is_described() -> None:
    _, _, _, prompts = surface()
    arguments = [argument for prompt in prompts for argument in prompt.arguments or []]
    assert len(arguments) == 4
    assert all(a.description and a.description.endswith(".") for a in arguments)


@pytest.mark.parametrize("token", VOCABULARY)
def test_s5_prompt_vocabulary_exists_on_the_tools(token: str) -> None:
    on_tools = token in tool_descriptions()
    as_key = token.split(".")[0] in ENVELOPE_KEYS
    assert on_tools or as_key or token in GUARDRAILS


def test_s6_named_resources_are_registered_and_counts_hold() -> None:
    tools, resources, templates, prompts = surface()
    assert (len(tools), len(resources), len(templates), len(prompts)) == (3, 1, 4, 3)
    assert "edgar://issuers/{cik}/latest" in TEMPLATE_URIS
    assert "edgar://issuers/{cik}/latest" in GUARDRAILS
    assert str(resources[0].uri) == FORM_TYPES
    assert [t.uri_template for t in templates] == list(TEMPLATE_URIS)
