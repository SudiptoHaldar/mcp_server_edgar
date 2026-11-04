"""The two GitHub workflows stay what the release recipe assumes (req_005 v5.04).

``release.yml`` publishes on a version tag: gate -> PyPI (Trusted Publishing behind the ``pypi``
environment) -> index verification -> Registry (GitHub OIDC) -> Registry verification. These tests
parse both workflow files with PyYAML and pin the trigger, the permissions, the guards, the SHA
pins and the steps that carry the release's guarantees. Offline; ships with the export, so no
private string appears literally (gotchas #25) and nothing outside the export is read.
"""

import re
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
RELEASE_PATH = ROOT / ".github" / "workflows" / "release.yml"
CI_PATH = ROOT / ".github" / "workflows" / "ci.yml"
RELEASE_TEXT = RELEASE_PATH.read_text(encoding="utf-8")
CI_TEXT = CI_PATH.read_text(encoding="utf-8")
RELEASE: dict[str, Any] = yaml.safe_load(RELEASE_TEXT)
CI: dict[str, Any] = yaml.safe_load(CI_TEXT)
ON: dict[str, Any] = RELEASE[True]  # PyYAML 1.1 reads the key `on` as the boolean True
JOBS: dict[str, Any] = RELEASE["jobs"]
ORDER = ["gate", "pypi", "verify-pypi", "registry", "verify-registry"]
PUBLISH_JOBS = ORDER[1:]
PROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
PUBLIC_REPO = "SudiptoHaldar/mcp_server_edgar"
PYPI_PROJECT = "https://pypi.org/project/" + PROJECT["name"] + "/"
MARKER_NAME = next(
    line
    for line in (ROOT / "README.md").read_text(encoding="utf-8").splitlines()
    if line.startswith("<!-- mcp-name: ")
).removeprefix("<!-- mcp-name: ").removesuffix(" -->")
PINS = {
    "actions/checkout": "3d3c42e5aac5ba805825da76410c181273ba90b1",
    "astral-sh/setup-uv": "68559e99cc807c1eb7e9238fd43a15bba460eec2",
    "actions/upload-artifact": "cf430e030ddbb5b0abf93d22962f4752f3646cd9",
    "actions/download-artifact": "9000827ccba6bdab643e8b6fd33ac0654aef8333",
    "sigstore/cosign-installer": "6f9f17788090df1f26f669e9d70d6ae9567deba6",
}
UNPINNED_OK = {"pypa/gh-action-pypi-publish@release/v1"}
MCP_PUBLISHER_VERSION = "v1.8.7"
USES_LINE = re.compile(r"^\s*-?\s*uses:\s*(\S+)(?:\s+#\s*(v\d+\.\d+\.\d+)\b.*)?$", re.MULTILINE)
REPOSITORY_GUARD = f"github.repository == '{PUBLIC_REPO}'"
PUSH_GUARD = "github.event_name == 'push'"


def steps(job: str) -> list[dict[str, Any]]:
    return JOBS[job]["steps"]


def runs(job: str) -> str:
    return "\n".join(step.get("run", "") for step in steps(job))


# --- trigger and top level -----------------------------------------------------------------


def test_trigger_is_version_tags_plus_a_manual_rehearsal() -> None:
    assert ON["push"] == {"tags": ["v[0-9]+.[0-9]+.[0-9]+"]}
    assert "workflow_dispatch" in ON
    assert not {"pull_request", "pull_request_target", "schedule", "release"} & set(ON)


def test_top_level_permissions_empty_and_one_release_at_a_time() -> None:
    assert RELEASE["permissions"] == {}
    assert RELEASE["concurrency"] == {"group": "release", "cancel-in-progress": False}
    assert RELEASE["name"] == "Release"


def test_job_order_and_needs_chain() -> None:
    assert list(JOBS) == ORDER
    assert [JOBS[job].get("needs") for job in ORDER] == [None, *ORDER[:-1]]


# --- per-job guards, runners, permissions ---------------------------------------------------


@pytest.mark.parametrize("job", ORDER)
def test_every_job_is_guarded_to_the_public_repository(job: str) -> None:
    condition = JOBS[job]["if"]
    assert REPOSITORY_GUARD in condition
    assert (PUSH_GUARD in condition) == (job in PUBLISH_JOBS)
    assert JOBS[job]["runs-on"] == "ubuntu-24.04"


@pytest.mark.parametrize("job", ORDER)
def test_every_job_declares_minimal_permissions(job: str) -> None:
    permissions = JOBS[job]["permissions"]
    assert isinstance(permissions, dict)
    assert permissions.get("contents") in (None, "read")
    assert set(permissions) <= {"contents", "id-token"}


def test_id_token_only_where_a_credential_is_minted() -> None:
    minting = {job for job in ORDER if JOBS[job]["permissions"].get("id-token") == "write"}
    assert minting == {"pypi", "registry"}


def test_environment_only_on_the_pypi_job() -> None:
    assert JOBS["pypi"]["environment"] == {"name": "pypi", "url": PYPI_PROJECT}
    assert all("environment" not in JOBS[job] for job in ORDER if job != "pypi")


# --- pins -----------------------------------------------------------------------------------


@pytest.mark.parametrize("text", [RELEASE_TEXT, CI_TEXT], ids=["release", "ci"])
def test_every_uses_is_sha_pinned_with_a_version_comment(text: str) -> None:
    found = USES_LINE.findall(text)
    assert found
    for ref, comment in found:
        if ref in UNPINNED_OK:
            continue
        action, sha = ref.split("@")
        assert PINS[action] == sha, ref
        assert comment, ref


def test_release_uses_exactly_the_expected_actions() -> None:
    refs = {ref for ref, _ in USES_LINE.findall(RELEASE_TEXT)}
    expected = {f"{action}@{sha}" for action, sha in PINS.items()} | UNPINNED_OK
    assert refs == expected


@pytest.mark.parametrize("doc", [RELEASE, CI], ids=["release", "ci"])
def test_checkouts_do_not_persist_credentials(doc: dict[str, Any]) -> None:
    checkouts = [
        step
        for job in doc["jobs"].values()
        for step in job["steps"]
        if str(step.get("uses", "")).startswith("actions/checkout@")
    ]
    assert checkouts
    assert all(step["with"]["persist-credentials"] is False for step in checkouts)


# --- the jobs -------------------------------------------------------------------------------


def test_gate_checks_the_tag_then_runs_the_gates_and_uploads_dist() -> None:
    text = runs("gate")
    assert "uv version --short" in text
    assert 'test "refs/tags/v${version}" = "${GITHUB_REF}"' in text
    assert "server.json" in text
    assert "uv sync --locked --all-groups" in text
    assert "uv run ruff check ." in text and "uv run pytest -q" in text
    assert "scripts/check_artifact.py --build" in text
    tag_step = next(step for step in steps("gate") if "uv version --short" in step.get("run", ""))
    assert tag_step["if"] == PUSH_GUARD
    upload = steps("gate")[-1]
    assert upload["uses"].startswith("actions/upload-artifact@")
    assert upload["with"] == {"name": "dist", "path": "dist/", "if-no-files-found": "error"}


def test_pypi_job_is_download_then_publish_only() -> None:
    download, publish = steps("pypi")
    assert download["uses"].startswith("actions/download-artifact@")
    assert download["with"] == {"name": "dist", "path": "dist/"}
    assert publish["uses"] == "pypa/gh-action-pypi-publish@release/v1"
    assert publish["with"] == {"print-hash": True, "skip-existing": True}


@pytest.mark.parametrize("text", [RELEASE_TEXT, CI_TEXT], ids=["release", "ci"])
def test_no_tokens_or_secrets_anywhere(text: str) -> None:
    for forbidden in ("secrets.", "UV_PUBLISH_TOKEN", "password", "GITHUB_TOKEN", "--token"):
        assert forbidden not in text


def test_verify_pypi_waits_for_the_index_then_runs_the_full_check() -> None:
    text = runs("verify-pypi")
    assert "scripts/check_artifact.py --from-pypi --no-twine --no-smoke" in text
    assert "sleep 30" in text
    assert "uv cache clean mcp-server-edgar" in text
    assert text.rstrip().endswith("scripts/check_artifact.py --from-pypi")


def test_registry_job_pins_and_verifies_the_cli_before_publishing() -> None:
    env = JOBS["registry"]["env"]
    assert env["MCP_PUBLISHER_VERSION"] == MCP_PUBLISHER_VERSION
    assert re.fullmatch(r"[0-9a-f]{64}", env["MCP_PUBLISHER_SHA256"])
    text = runs("registry")
    assert "mcp-publisher_linux_amd64.tar.gz" in text
    assert "sha256sum --check" in text
    assert "cosign verify-blob" in text
    assert "/.github/workflows/release.yml@refs/tags/${MCP_PUBLISHER_VERSION}" in text
    assert "https://token.actions.githubusercontent.com" in text
    assert [step["run"] for step in steps("registry")[-3:]] == [
        "./mcp-publisher validate",
        "./mcp-publisher login github-oidc",
        "./mcp-publisher publish",
    ]


def test_verify_registry_checks_name_version_and_repository() -> None:
    text = runs("verify-registry")
    assert MARKER_NAME in text
    assert "version=latest" in text
    assert ".server.packages[0].version" in text
    assert PROJECT["urls"]["Repository"] in text


# --- ci.yml and hygiene ---------------------------------------------------------------------


def test_ci_matrix_and_commands_untouched() -> None:
    test_job = CI["jobs"]["test"]
    assert test_job["strategy"]["matrix"]["python-version"] == ["3.11", "3.12", "3.13", "3.14"]
    assert test_job["runs-on"] == "${{ matrix.os }}"
    assert [step["run"] for step in test_job["steps"] if "run" in step] == [
        "uv sync --locked --all-groups",
        "uv run ruff check .",
        "uv run pytest -q",
    ]


def test_hygiene_and_budget() -> None:
    leaks = ["Sagentic-" + "Systems", "C:" + "\\", "req" + "specs", "." + "claude"]
    assert not any(leak in RELEASE_TEXT or leak in CI_TEXT for leak in leaks)
    assert len(RELEASE_TEXT.splitlines()) < 200
