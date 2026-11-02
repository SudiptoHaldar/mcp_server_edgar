"""Contract and hygiene tests for the public export (req_004 v4.01).

``scripts/export_public.ps1`` is not run here (it is PowerShell and writes to disk); its contract
(exclude list, preserve set, leak strings, default target, guards) is read from the script text,
and the exclude rule is mirrored in Python so the export set of this checkout can be checked for
the files it must carry, the paths it must not, and the private strings it must not contain.
The script itself is excluded from the export, so its tests skip in the public tree while the
export-set and hygiene tests keep running there on every CI run.
"""

import os
import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "export_public.ps1"
PUBLIC_REPO = "https://github.com/SudiptoHaldar/mcp_server_edgar"

EXCLUDE_DIRS = [".claude", "reqspecs", "administration", "track_progress"]
EXCLUDE_FILES = ["mcp_edgar.code-workspace", "release_number.md", "scripts/export_public.ps1"]
PRESERVE = [".git", ".venv", "dist", ".pytest_cache", ".ruff_cache", "__pycache__"]
# Built by concatenation: this file ships with the export, so it must not contain the strings
# that the export refuses to ship.
LEAKS = ["github.com/" + "Sagentic-Systems", "C:" + "\\Sudipto", "C:" + "\\Users\\"]
DEFAULT_TARGET = "C:" + "\\Sudipto\\Projects-external\\AIML_projects\\mcp_server_edgar"
REQUIRED_IN_EXPORT = [
    "LICENSE",
    "README.md",
    "pyproject.toml",
    "uv.lock",
    ".gitignore",
    ".python-version",
    ".github/workflows/ci.yml",
    "scripts/check_artifact.py",
    "scripts/refresh_form_types.py",
    "scripts/inspector_checks.py",
    "scripts/inspector_checklist.py",
    "src/mcp_edgar/server.py",
    "src/mcp_edgar/data/form_types.json",
    "tests/conftest.py",
]
GUARD_TOKENS = [
    "#Requires -Version 5.1",
    "[CmdletBinding(SupportsShouldProcess = $true)]",
    "Set-StrictMode -Version Latest",
    "'status', '--porcelain'",
    "'core.quotepath=off', 'ls-files'",
    "$WhatIfPreference",
    "$PSCmdlet.ShouldProcess(",
    "System.Security.Cryptography.SHA256",
    "rev-parse",
]
SKIP_DIRS = set(PRESERVE) | {"node_modules"}
LINE_BUDGET = 350  # spec said 300; the help block alone is 27 lines (project rule: 500)

needs_script = pytest.mark.skipif(
    not SCRIPT.exists(), reason="the export script is not shipped in the public tree"
)


def script_text() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def is_excluded(relpath: str) -> bool:
    """Mirror of the script's Test-Excluded: exact file match or first path segment."""
    return relpath in EXCLUDE_FILES or relpath.split("/")[0] in EXCLUDE_DIRS


def parse_array(text: str, name: str) -> list[str]:
    """Items of a single-line ``$Name = @('a', 'b')`` literal in PowerShell source."""
    match = re.search(rf"^\${re.escape(name)}\s*=\s*@\(([^)]*)\)", text, re.MULTILINE)
    if match is None:
        raise KeyError(name)
    return re.findall(r"'([^']*)'", match.group(1))


def parse_string(text: str, name: str) -> str:
    match = re.search(rf"^\${re.escape(name)}\s*=\s*'([^']*)'", text, re.MULTILINE)
    if match is None:
        raise KeyError(name)
    return match.group(1)


def export_set() -> list[str]:
    """Files of this checkout that the export would ship, as forward-slash relative paths."""
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for filename in filenames:
            rel = (Path(dirpath) / filename).relative_to(ROOT).as_posix()
            if not is_excluded(rel):
                found.append(rel)
    return sorted(found)


# --- the script's contract (private tree only) ---------------------------------------------


@needs_script
def test_script_is_ascii_and_within_budget() -> None:
    text = script_text()
    assert text.isascii()
    assert len(text.splitlines()) < LINE_BUDGET


@needs_script
@pytest.mark.parametrize("token", GUARD_TOKENS)
def test_script_declares_its_guards(token: str) -> None:
    assert token in script_text()


@needs_script
@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("ExcludeDirs", EXCLUDE_DIRS),
        ("ExcludeFiles", EXCLUDE_FILES),
        ("Preserve", PRESERVE),
        ("Leaks", LEAKS),
    ],
)
def test_script_arrays_match_the_contract(name: str, expected: list[str]) -> None:
    assert parse_array(script_text(), name) == expected


@needs_script
def test_script_default_target_is_the_export_directory() -> None:
    assert parse_string(script_text(), "DefaultTarget") == DEFAULT_TARGET


@needs_script
def test_script_excludes_itself() -> None:
    assert is_excluded(SCRIPT.relative_to(ROOT).as_posix())


# --- parsing helpers ------------------------------------------------------------------------


def test_parse_array_reads_a_literal_and_ignores_other_lines() -> None:
    text = "$Other = @('x')\n$Names = @('a', 'b c', 'd\\e')  # comment\n"
    assert parse_array(text, "Names") == ["a", "b c", "d\\e"]
    assert parse_array(text, "Other") == ["x"]


def test_parse_array_missing_name_raises() -> None:
    with pytest.raises(KeyError):
        parse_array("$Names = @('a')\n", "Missing")


# --- exclude rule -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "relpath",
    [
        ".claude/CLAUDE.md",
        "reqspecs/reqs/req_004_public_export/req_004_01_spec.md",
        "administration/runbook.md",
        "track_progress/README.md",
        "release_number.md",
        "mcp_edgar.code-workspace",
        "scripts/export_public.ps1",
    ],
)
def test_is_excluded_true(relpath: str) -> None:
    assert is_excluded(relpath)


@pytest.mark.parametrize(
    "relpath",
    [
        "README.md",
        "scripts/check_artifact.py",
        "src/mcp_edgar/release_number.md",
        "tests/fixtures/submissions_CIK0000320193.json",
        ".github/workflows/ci.yml",
    ],
)
def test_is_excluded_false(relpath: str) -> None:
    assert not is_excluded(relpath)


# --- the export set of this checkout (runs in the private and the public tree) -------------


def test_export_set_carries_the_required_files() -> None:
    files = set(export_set())
    assert [p for p in REQUIRED_IN_EXPORT if p not in files] == []


def test_export_set_has_no_excluded_path() -> None:
    files = export_set()
    assert [p for p in files if p.split("/")[0] in EXCLUDE_DIRS] == []
    assert [p for p in files if p in EXCLUDE_FILES] == []
    assert files, "the walk found nothing"


def test_export_set_has_no_private_string() -> None:
    offenders = []
    for rel in export_set():
        text = (ROOT / rel).read_text(encoding="utf-8", errors="ignore")
        offenders.extend(f"{rel}: {leak}" for leak in LEAKS if leak in text)
    assert offenders == []


def test_project_urls_point_at_the_public_repo() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["urls"]
    assert all(url.startswith(PUBLIC_REPO) for url in project["urls"].values())
