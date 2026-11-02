"""``server.json`` stays in step with its sources (req_005 v5.02).

The Registry document at the repository root carries eight values that all exist elsewhere:
the README marker (``name``), the server's own name (``title``), ``pyproject.toml``
(``description``, ``repository.url``, package ``identifier``), ``__version__`` (both ``version``
fields) and ``Settings`` (environment variable names). Nothing here is typed twice, nothing
touches the network, and the file must be the canonical ``json.dumps(indent=2)`` rendering so a
hand edit cannot reorder or reformat it unnoticed.
"""

import json
import re
import tomllib
from pathlib import Path

import pytest

import mcp_edgar
from mcp_edgar import server
from mcp_edgar.settings import ENV_PREFIX, Settings

ROOT = Path(__file__).resolve().parents[1]
TEXT = (ROOT / "server.json").read_text(encoding="utf-8")
DOC = json.loads(TEXT)
PROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
README_LINES = (ROOT / "README.md").read_text(encoding="utf-8").splitlines()
MARKER_NAME = (
    next(line for line in README_LINES if line.startswith("<!-- mcp-name: "))
    .removeprefix("<!-- mcp-name: ")
    .removesuffix(" -->")
)
ENV_NAMES = {ENV_PREFIX + field.upper() for field in Settings.__dataclass_fields__}
PACKAGE = DOC["packages"][0]
SCHEMA_RE = re.compile(
    r"https://static\.modelcontextprotocol\.io/schemas/\d{4}-\d{2}-\d{2}/server\.schema\.json"
)
NAME_RE = re.compile(r"[a-zA-Z0-9.-]+/[a-zA-Z0-9._-]+")
PRIVATE_STRINGS = ("Sagentic-" + "Systems", "C:" + "\\", "req" + "specs", "." + "claude", "TODO")


# --- shape --------------------------------------------------------------------------------


def test_file_is_canonical_two_space_json_with_trailing_newline() -> None:
    assert TEXT == json.dumps(DOC, indent=2) + "\n"
    assert TEXT.isascii() and "\r" not in TEXT


def test_top_level_keys_in_the_documented_order() -> None:
    assert list(DOC) == [
        "$schema",
        "name",
        "title",
        "description",
        "repository",
        "version",
        "packages",
    ]


@pytest.mark.parametrize("token", PRIVATE_STRINGS)
def test_no_placeholder_or_private_string(token: str) -> None:
    assert token not in TEXT


def test_schema_url_is_a_dated_official_schema() -> None:
    assert SCHEMA_RE.fullmatch(DOC["$schema"])


# --- identity -----------------------------------------------------------------------------


def test_name_matches_the_readme_marker_and_the_registry_pattern() -> None:
    assert DOC["name"] == MARKER_NAME == "io.github.SudiptoHaldar/mcp-server-edgar"
    assert NAME_RE.fullmatch(DOC["name"]) and 3 <= len(DOC["name"]) <= 200
    assert DOC["name"].count("/") == 1


def test_namespace_is_the_public_repository_owner() -> None:
    owner = PROJECT["urls"]["Repository"].split("/")[3]
    assert DOC["name"].split("/")[0] == f"io.github.{owner}" == "io.github.SudiptoHaldar"


def test_title_is_the_server_name() -> None:
    assert DOC["title"] == server.mcp.name and 1 <= len(DOC["title"]) <= 100


def test_description_is_the_project_description_within_the_registry_limit() -> None:
    assert DOC["description"] == PROJECT["description"]
    assert 1 <= len(DOC["description"]) <= 100


# --- versions -----------------------------------------------------------------------------


def test_versions_agree_everywhere_and_are_exact_semver() -> None:
    assert DOC["version"] == PACKAGE["version"] == mcp_edgar.__version__ == PROJECT["version"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", DOC["version"]) and DOC["version"] != "latest"


# --- repository ---------------------------------------------------------------------------


def test_repository_is_the_public_github_repository() -> None:
    assert DOC["repository"] == {"url": PROJECT["urls"]["Repository"], "source": "github"}
    assert DOC["repository"]["url"].startswith("https://github.com/")


# --- package ------------------------------------------------------------------------------


def test_exactly_one_pypi_package_over_stdio() -> None:
    assert len(DOC["packages"]) == 1
    assert PACKAGE["registryType"] == "pypi"
    assert PACKAGE["registryBaseUrl"] == "https://pypi.org"
    assert PACKAGE["identifier"] == PROJECT["name"] == "mcp-server-edgar"
    assert PACKAGE["transport"] == {"type": "stdio"}
    assert PACKAGE["runtimeHint"] == "uvx"


def test_package_keys_in_the_documented_order() -> None:
    assert list(PACKAGE) == [
        "registryType",
        "registryBaseUrl",
        "identifier",
        "version",
        "runtimeHint",
        "transport",
        "environmentVariables",
    ]


@pytest.mark.parametrize("variable", PACKAGE["environmentVariables"], ids=lambda v: v["name"])
def test_environment_variable_is_a_real_optional_non_secret_setting(variable: dict) -> None:
    assert variable["name"] in ENV_NAMES
    assert variable["description"].strip()
    assert variable["isSecret"] is False and variable["isRequired"] is False
    assert variable["format"] == "string"


def test_environment_variable_names_are_unique_and_name_the_user_agent() -> None:
    names = [variable["name"] for variable in PACKAGE["environmentVariables"]]
    assert len(names) == len(set(names)) and "MCP_EDGAR_USER_AGENT" in names
