"""Packaging guards (req_003 v3.01): README, LICENSE and pyproject.toml stay correct.

The README is the PyPI page, the GitHub front page and the Registry's ownership proof at once.
It must carry the ``mcp-name`` marker on its own line, name every tool, resource, prompt and
environment variable the server exposes (computed from the server, never typed), and contain no
private URL, local path, process text or hard-coded version. Everything is offline.
"""

import asyncio
import importlib
import re
import tomllib
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pytest

import mcp_edgar
from mcp_edgar import resources, server
from mcp_edgar.settings import ENV_PREFIX, Settings

ROOT = Path(__file__).resolve().parents[1]
README_TEXT = (ROOT / "README.md").read_text(encoding="utf-8")
README_LINES = README_TEXT.splitlines()
PROJECT: dict[str, Any] = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
    "project"
]

MARKER = "<!-- mcp-name: io.github.SudiptoHaldar/mcp-server-edgar -->"
PUBLIC_REPO = "https://github.com/SudiptoHaldar/mcp_server_edgar"
URL_RE = re.compile(r"https?://[^\s)\]>\"'`]+")
ALLOWED_HOSTS = {
    "www.sec.gov",
    "data.sec.gov",
    "sec.gov",
    "github.com",
    "pypi.org",
    "registry.modelcontextprotocol.io",
    "modelcontextprotocol.io",
    "docs.astral.sh",
}
GITHUB_PATHS = ("/SudiptoHaldar/mcp_server_edgar", "/modelcontextprotocol/")
SECTION_KEYWORDS = (
    "Install",
    "Configure",
    "Tools",
    "Results",
    "Resources",
    "Prompts",
    "Configuration",
    "SEC EDGAR",
    "limits",
    "Development",
    "License",
)
REQUIRED_TOKENS = (
    "uvx mcp-server-edgar",
    "pip install mcp-server-edgar",
    PUBLIC_REPO,
    "filing_date",
    "report_date",
    "next_offset",
    "total_count",
    "ambiguous",
    "candidates",
    "cik",
    "upstream_error",
    "MIT",
    "1993",
    "10 requests",
)
FORBIDDEN_PATTERNS = (
    r"Sagentic-Systems",
    r"reqspecs",
    r"\.claude",
    r"C:\\",
    r"AIML_products",
    r"PRP",
    r"API_KEY",
    r"req_\d{3}",
    r"v\d\.\d{2}\b",
    re.escape(mcp_edgar.__version__),
)


def tools() -> list[Any]:
    return asyncio.run(server.mcp.list_tools())


def prompts() -> list[Any]:
    return asyncio.run(server.mcp.list_prompts())


SURFACE_NAMES = (
    [tool.name for tool in tools()]
    + [tool.title for tool in tools()]
    + [*resources.TEMPLATE_URIS, resources.FORM_TYPES]
    + [prompt.name for prompt in prompts()]
    + sorted(ENV_PREFIX + field.upper() for field in Settings.__dataclass_fields__)
)


# --- README: marker -----------------------------------------------------------------------


def test_marker_is_exactly_once_on_its_own_line_near_the_top() -> None:
    assert [line for line in README_LINES[:5] if line == MARKER] == [MARKER]
    assert README_TEXT.count("mcp-name") == 1


def test_marker_namespace_matches_the_repository_owner() -> None:
    owner = urlparse(PROJECT["urls"]["Repository"]).path.split("/")[1]
    namespace = MARKER.split("io.github.")[1].split("/")[0]
    assert namespace == owner == "SudiptoHaldar"


# --- README: parity with the server -------------------------------------------------------


def test_surface_names_are_computed_not_typed() -> None:
    assert len(SURFACE_NAMES) == 3 + 3 + 5 + 3 + 7
    assert "MCP_EDGAR_USER_AGENT" in SURFACE_NAMES and "edgar://form-types" in SURFACE_NAMES


@pytest.mark.parametrize("name", SURFACE_NAMES)
def test_readme_names_every_primitive_and_variable(name: str) -> None:
    assert name in README_TEXT


# --- README: required and forbidden content -----------------------------------------------


@pytest.mark.parametrize("token", REQUIRED_TOKENS)
def test_readme_carries_required_token(token: str) -> None:
    assert token in README_TEXT


def test_readme_says_no_api_key_is_needed() -> None:
    assert "no api key" in README_TEXT.lower()


@pytest.mark.parametrize("pattern", FORBIDDEN_PATTERNS)
def test_readme_has_no_private_or_process_content(pattern: str) -> None:
    assert re.search(pattern, README_TEXT) is None


@pytest.mark.parametrize("url", sorted(set(URL_RE.findall(README_TEXT))))
def test_readme_urls_are_https_on_allowed_hosts(url: str) -> None:
    parts = urlparse(url)
    assert parts.scheme == "https"
    assert parts.netloc in ALLOWED_HOSTS
    if parts.netloc == "github.com":
        assert parts.path.startswith(GITHUB_PATHS)


def test_readme_has_urls_to_check() -> None:
    assert len(set(URL_RE.findall(README_TEXT))) >= 5


# --- README: shape ------------------------------------------------------------------------


def test_readme_title_and_length() -> None:
    assert README_LINES[0] == "# SEC EDGAR MCP Server"
    assert len(README_LINES) < 350


def test_readme_sections_appear_in_order() -> None:
    headings = [line for line in README_LINES if line.startswith("## ")]
    positions = [
        next(index for index, heading in enumerate(headings) if keyword in heading)
        for keyword in SECTION_KEYWORDS
    ]
    assert len(headings) == len(SECTION_KEYWORDS)
    assert positions == sorted(positions) == list(range(len(SECTION_KEYWORDS)))


# --- pyproject.toml -----------------------------------------------------------------------


def test_project_urls_point_at_the_public_repository() -> None:
    assert PROJECT["urls"] == {
        "Homepage": PUBLIC_REPO,
        "Repository": PUBLIC_REPO,
        "Issues": PUBLIC_REPO + "/issues",
    }


def test_console_script_entry_point() -> None:
    assert PROJECT["scripts"] == {"mcp-server-edgar": "mcp_edgar.server:main"}
    assert callable(importlib.import_module("mcp_edgar.server").main)


def test_license_metadata_follows_pep_639() -> None:
    assert PROJECT["license"] == "MIT"
    assert PROJECT["license-files"] == ["LICENSE"]
    assert not any(c.startswith("License ::") for c in PROJECT["classifiers"])


def test_identity_fields() -> None:
    assert PROJECT["name"] == "mcp-server-edgar"
    assert PROJECT["readme"] == "README.md"
    assert PROJECT["requires-python"] == ">=3.11"
    assert len(PROJECT["description"]) < 100


# --- LICENSE ------------------------------------------------------------------------------


def test_license_file_is_mit() -> None:
    text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    lines = text.splitlines()
    assert lines[0] == "MIT License"
    assert "Copyright (c) 2026 Sudipto Haldar" in text
    assert "Permission is hereby granted, free of charge" in text
    assert 'THE SOFTWARE IS PROVIDED "AS IS"' in text
    assert text.endswith("\n") and len(lines) < 30
