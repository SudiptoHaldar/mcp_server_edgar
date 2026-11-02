"""Offline tests for scripts/check_artifact.py (req_003 v3.02).

The judges are pure functions over archive listings, metadata text and an observation dict; the
fixtures are the real listings recorded from the v3.01 build on 2026-11-01. Nothing here builds,
spawns ``uvx`` or touches the network.
"""

import hashlib
import importlib.util
import re
import sys
import tomllib
from pathlib import Path
from types import ModuleType

import pytest

import mcp_edgar

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_artifact.py"
PYPROJECT_TEXT = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
DESCRIPTION = tomllib.loads(PYPROJECT_TEXT)["project"]["description"]
VERSION = "0.1.0"
STEM = f"mcp_server_edgar-{VERSION}"
DI = f"{STEM}.dist-info"
MARKER = "<!-- mcp-name: io.github.SudiptoHaldar/mcp-server-edgar -->"
PUBLIC_REPO = "https://github.com/SudiptoHaldar/mcp_server_edgar"
MODULE_FILES = [
    "__init__.py",
    "edgar_urls.py",
    "filings.py",
    "form_types.py",
    "outcomes.py",
    "prompts.py",
    "resources.py",
    "sec_client.py",
    "server.py",
    "settings.py",
    "tickers.py",
]
MODULES = set(MODULE_FILES)
WHEEL_NAMES = [
    "mcp_edgar/",
    "mcp_edgar/data/",
    "mcp_edgar/data/form_types.json",
    *[f"mcp_edgar/{m}" for m in MODULE_FILES],
    f"{DI}/licenses/",
    f"{DI}/licenses/LICENSE",
    f"{DI}/",
    f"{DI}/WHEEL",
    f"{DI}/entry_points.txt",
    f"{DI}/METADATA",
    f"{DI}/RECORD",
]
SDIST_NAMES = [
    f"{STEM}/PKG-INFO",
    STEM,
    f"{STEM}/LICENSE",
    f"{STEM}/README.md",
    f"{STEM}/pyproject.toml",
    f"{STEM}/src",
    f"{STEM}/src/mcp_edgar",
    f"{STEM}/src/mcp_edgar/data",
    f"{STEM}/src/mcp_edgar/data/form_types.json",
    *[f"{STEM}/src/mcp_edgar/{m}" for m in MODULE_FILES],
]
HEADER = [
    "Metadata-Version: 2.4",
    "Name: mcp-server-edgar",
    f"Version: {VERSION}",
    "Summary: Latest SEC filing and calendar-year filing history for a stock ticker.",
    "Author: Sudipto Haldar",
    "License-Expression: MIT",
    "License-File: LICENSE",
    "Classifier: Development Status :: 3 - Alpha",
    "Classifier: Programming Language :: Python :: 3",
    "Requires-Dist: httpx>=0.28.1",
    "Requires-Python: >=3.11",
    f"Project-URL: Homepage, {PUBLIC_REPO}",
    f"Project-URL: Issues, {PUBLIC_REPO}/issues",
    f"Project-URL: Repository, {PUBLIC_REPO}",
    "Description-Content-Type: text/markdown",
]
BODY = f"# SEC EDGAR MCP Server\n\n{MARKER}\n\nA read-only MCP server over stdio.\n"
METADATA = "\n".join(HEADER) + "\n\n" + BODY
ENTRY_POINTS = "[console_scripts]\nmcp-server-edgar = mcp_edgar.server:main\n\n"
SMOKE_OK = {
    "name": "SEC EDGAR Filings",
    "version": VERSION,
    "protocol": "2025-11-25",
    "instructions": 903,
    "counts": (3, 1, 4, 3),
    "forms": {"status": "ok", "total_count": 386},
    "seconds": 2.9,
}
WHEEL_FILE = f"{STEM}-py3-none-any.whl"
SDIST_FILE = f"{STEM}.tar.gz"
WHEEL_SHA = "a" * 64
SDIST_SHA = "b" * 64
LOCAL_DIGESTS = {WHEEL_FILE: WHEEL_SHA, SDIST_FILE: SDIST_SHA}
INDEX_OK = {  # the shape of https://pypi.org/pypi/<name>/<version>/json (req_005 v5.01)
    "info": {
        "name": "mcp-server-edgar",
        "version": VERSION,
        "summary": DESCRIPTION,
        "requires_python": ">=3.11",
        "project_urls": {
            "Homepage": PUBLIC_REPO,
            "Repository": PUBLIC_REPO,
            "Issues": PUBLIC_REPO + "/issues",
        },
    },
    "urls": [
        {
            "filename": WHEEL_FILE,
            "packagetype": "bdist_wheel",
            "digests": {"sha256": WHEEL_SHA},
            "yanked": False,
            "upload_time_iso_8601": "2026-11-02T10:00:00.000000Z",
        },
        {
            "filename": SDIST_FILE,
            "packagetype": "sdist",
            "digests": {"sha256": SDIST_SHA},
            "yanked": False,
            "upload_time_iso_8601": "2026-11-02T10:00:01.000000Z",
        },
    ],
}


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_artifact", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look the module up by name (gotchas #18)
    spec.loader.exec_module(module)
    return module


mod = load()


def metadata(header: list[str] = HEADER, body: str = BODY) -> tuple[dict, str]:
    return mod.parse_metadata("\n".join(header) + "\n\n" + body)


# --- module shape -------------------------------------------------------------------------


def test_script_has_no_future_import_and_is_small() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert re.search(r"^from __future__ import", source, re.MULTILINE) is None
    assert len(source.splitlines()) < 500  # budget revised from 450 for --from-pypi (v5.01)


def test_expected_version_reads_pyproject_and_matches_package() -> None:
    assert mod.expected_version('[project]\nname = "x"\nversion = "9.8.7"\n') == "9.8.7"
    assert mod.expected_version(PYPROJECT_TEXT) == mcp_edgar.__version__


def test_expected_description_reads_pyproject() -> None:
    assert mod.expected_description('[project]\nname = "x"\ndescription = "Hi."\n') == "Hi."
    assert mod.expected_description(PYPROJECT_TEXT) == DESCRIPTION and len(DESCRIPTION) < 100


def test_stem_and_dist_info() -> None:
    assert mod.stem(VERSION) == STEM
    assert mod.dist_info(VERSION) == DI


# --- row 1: dist files --------------------------------------------------------------------


def test_dist_files_pass_with_one_wheel_and_one_sdist() -> None:
    ok, observed = mod.check_dist_files([f"{STEM}-py3-none-any.whl", f"{STEM}.tar.gz"], VERSION)
    assert ok and "stale" not in observed


@pytest.mark.parametrize(
    "names",
    [
        [f"{STEM}-py3-none-any.whl", "mcp_server_edgar-0.0.9-py3-none-any.whl", f"{STEM}.tar.gz"],
        ["mcp_server_edgar-0.2.0-py3-none-any.whl", f"{STEM}.tar.gz"],
        [f"{STEM}-py3-none-any.whl"],
        [],
    ],
)
def test_dist_files_fail_on_stale_wrong_or_missing(names: list[str]) -> None:
    ok, observed = mod.check_dist_files(names, VERSION)
    assert not ok and "stale or missing" in observed


# --- rows 2 and 5: listings ---------------------------------------------------------------


def test_wheel_rows_pass_on_the_recorded_listing() -> None:
    ok, observed = mod.check_wheel_required(WHEEL_NAMES, VERSION)
    assert ok and "entries=21" in observed, observed
    ok, observed = mod.check_wheel_hygiene(WHEEL_NAMES, VERSION, MODULES)
    assert ok and "modules=11" in observed, observed


@pytest.mark.parametrize(
    "dropped",
    ["mcp_edgar/data/form_types.json", f"{DI}/licenses/LICENSE", f"{DI}/entry_points.txt"],
)
def test_wheel_required_fails_when_an_entry_is_missing(dropped: str) -> None:
    names = [n for n in WHEEL_NAMES if n != dropped]
    ok, observed = mod.check_wheel_required(names, VERSION)
    assert not ok and dropped in observed
    assert mod.check_wheel_hygiene(names, VERSION, MODULES)[0] or dropped.endswith(".py")


@pytest.mark.parametrize("extra", ["mcp_edgar/tests/test_x.py", "foo/bar.py", "mcp_edgar/x.pyc"])
def test_wheel_hygiene_fails_on_forbidden_or_outside_entries(extra: str) -> None:
    names = [*WHEEL_NAMES, extra]
    assert mod.check_wheel_required(names, VERSION)[0]
    assert not mod.check_wheel_hygiene(names, VERSION, MODULES)[0]


def test_wheel_hygiene_fails_when_a_checkout_module_is_not_shipped() -> None:
    ok, observed = mod.check_wheel_hygiene(WHEEL_NAMES, VERSION, MODULES | {"new_module.py"})
    assert not ok and "new_module.py" in observed


def test_sdist_entries_pass_on_the_recorded_listing() -> None:
    ok, observed = mod.check_sdist_entries(SDIST_NAMES, VERSION, MODULES)
    assert ok, observed
    assert "modules=11" in observed and "catalogue=True" in observed


@pytest.mark.parametrize(
    "mutation",
    [
        lambda names: [*names, f"{STEM}/tests", f"{STEM}/tests/test_x.py"],
        lambda names: [n for n in names if not n.endswith("/LICENSE")],
        lambda names: [*names, f"{STEM}/uv.lock"],
        lambda names: [n for n in names if not n.endswith("/tickers.py")],
    ],
)
def test_sdist_entries_fail_on_extra_missing_or_forbidden(mutation) -> None:
    ok, _ = mod.check_sdist_entries(mutation(SDIST_NAMES), VERSION, MODULES)
    assert not ok


# --- rows 3 and 6: metadata ----------------------------------------------------------------


def test_parse_metadata_splits_header_fields_and_body() -> None:
    fields, body = mod.parse_metadata(METADATA)
    assert fields["Version"] == [VERSION] and len(fields["Project-URL"]) == 3
    assert fields["Classifier"] == [HEADER[7][12:], HEADER[8][12:]]
    assert body == BODY


def test_metadata_rows_pass_on_the_recorded_header_and_body() -> None:
    fields, body = metadata()
    ok, observed = mod.check_metadata_header(fields, VERSION)
    assert ok and "project_urls=3" in observed, observed
    ok, observed = mod.check_long_description(body)
    assert ok and "marker_once=True" in observed, observed


@pytest.mark.parametrize(
    "header",
    [
        [h if not h.startswith("Version:") else "Version: 0.2.0" for h in HEADER],
        [*HEADER, f"Project-URL: Docs, {PUBLIC_REPO}/wiki"],
        [h.replace("SudiptoHaldar/mcp_server_edgar", "Sagentic-Systems/mcp_edgar") for h in HEADER],
        [h for h in HEADER if not h.startswith("License-File")],
        [h for h in HEADER if not h.startswith("Description-Content-Type")],
    ],
)
def test_metadata_header_fails_on_bad_header(header: list[str]) -> None:
    fields, body = metadata(header=header)
    assert not mod.check_metadata_header(fields, VERSION)[0]
    assert mod.check_long_description(body)[0]  # the body is untouched


@pytest.mark.parametrize(
    "body",
    [
        BODY.replace(MARKER, " " + MARKER),
        BODY + MARKER + "\n",
        BODY.replace(MARKER, ""),
        BODY + "Built from C:\\Users\\me\\repo\n",
        BODY + "See reqspecs/ for the process.\n",
    ],
)
def test_long_description_fails_on_bad_body(body: str) -> None:
    fields, parsed_body = metadata(body=body)
    assert mod.check_metadata_header(fields, VERSION)[0]  # the header is untouched
    assert not mod.check_long_description(parsed_body)[0]


def test_pkg_info_judges_name_version_and_licence_file() -> None:
    fields, _ = metadata()
    assert mod.check_pkg_info(fields, VERSION)[0]
    assert not mod.check_pkg_info(fields, "0.2.0")[0]
    assert not mod.check_pkg_info({k: v for k, v in fields.items() if k != "Name"}, VERSION)[0]


# --- row 4: entry point --------------------------------------------------------------------


def test_entry_points_exact_text_with_trailing_blank_lines() -> None:
    assert mod.check_entry_points(ENTRY_POINTS)[0]
    assert not mod.check_entry_points(ENTRY_POINTS.replace(":main", ":run"))[0]
    assert not mod.check_entry_points(ENTRY_POINTS.rstrip() + "\nother = mcp_edgar:x\n")[0]


# --- rows 7 to 9: twine and smoke judges -----------------------------------------------------


def test_twine_passed_needs_two_passed_lines_and_exit_zero() -> None:
    two = f"Checking {STEM}-py3-none-any.whl: PASSED\nChecking {STEM}.tar.gz: PASSED\n"
    assert mod.twine_passed(two, 0) == (True, "rc=0; passed_lines=2")
    assert not mod.twine_passed(two.splitlines()[0], 0)[0]
    assert not mod.twine_passed(two, 1)[0]


def test_smoke_command_launches_the_wheel_in_isolation() -> None:
    wheel = Path("dist") / WHEEL_FILE
    assert mod.smoke_command(wheel, None, VERSION) == [
        "uvx",
        "--isolated",
        "--refresh",
        "--from",
        str(wheel),
        "mcp-server-edgar",
    ]
    with_python = mod.smoke_command(wheel, "3.11", VERSION)
    assert with_python[-3:] == ["--python", "3.11", "mcp-server-edgar"]


def test_smoke_command_from_the_index_pins_the_version() -> None:
    pinned = f"mcp-server-edgar@{VERSION}"
    assert mod.smoke_command(None, None, VERSION) == ["uvx", "--isolated", "--refresh", pinned]
    assert mod.smoke_command(None, "3.11", VERSION)[-3:] == ["--python", "3.11", pinned]
    assert "--from" not in mod.smoke_command(None, "3.11", VERSION)


def test_judge_smoke_passes_on_the_recorded_observation() -> None:
    ok, observed = mod.judge_smoke(SMOKE_OK, VERSION)
    assert ok, observed
    assert "server=SEC EDGAR Filings 0.1.0" in observed and "form_types=ok 386" in observed


@pytest.mark.parametrize(
    "change",
    [
        {"counts": (3, 1, 4, 2)},
        {"version": "0.0.9"},
        {"name": "Something Else"},
        {"forms": {"status": "invalid_input", "total_count": 0}},
        {"forms": {"status": "ok", "total_count": 10}},
        {"protocol": ""},
        {"instructions": 0},
        {"seconds": 600.0},
    ],
)
def test_judge_smoke_fails_on_each_deviation(change: dict) -> None:
    ok, _ = mod.judge_smoke({**SMOKE_OK, **change}, VERSION)
    assert not ok


def test_judge_smoke_tolerates_a_bare_failure_dict() -> None:
    assert not mod.judge_smoke({}, VERSION)[0]


# --- row 10 (--from-pypi): the index record -------------------------------------------------


def with_info(**changes: object) -> dict:
    return {**INDEX_OK, "info": {**INDEX_OK["info"], **changes}}


def with_urls(urls: list[dict]) -> dict:
    return {**INDEX_OK, "urls": urls}


BAD_DIGEST_WHEEL = {**INDEX_OK["urls"][0], "digests": {"sha256": "c" * 64}}


def test_sha256_of_matches_hashlib(tmp_path: Path) -> None:
    path = tmp_path / WHEEL_FILE
    path.write_bytes(b"wheel bytes")
    assert mod.sha256_of(path) == hashlib.sha256(b"wheel bytes").hexdigest()


def test_index_release_passes_on_the_recorded_payload() -> None:
    ok, observed = mod.check_index_release(INDEX_OK, LOCAL_DIGESTS, VERSION, DESCRIPTION)
    assert ok, observed
    assert "files=2" in observed and "digests_match=True" in observed and "yanked=0" in observed
    assert "uploaded=2026-11-02T10:00:01" in observed


@pytest.mark.parametrize(
    ("payload", "local"),
    [
        (with_info(version="0.0.9"), LOCAL_DIGESTS),
        (with_info(name="other-package"), LOCAL_DIGESTS),
        (with_info(summary=DESCRIPTION + "!"), LOCAL_DIGESTS),
        (with_info(requires_python=">=3.10"), LOCAL_DIGESTS),
        (
            with_info(
                project_urls={
                    **INDEX_OK["info"]["project_urls"],
                    "Homepage": PUBLIC_REPO.replace("SudiptoHaldar", "Sagentic-Systems"),
                }
            ),
            LOCAL_DIGESTS,
        ),
        (
            with_urls([*INDEX_OK["urls"], {**INDEX_OK["urls"][0], "filename": "extra.whl"}]),
            LOCAL_DIGESTS,
        ),
        (with_urls(INDEX_OK["urls"][:1]), LOCAL_DIGESTS),
        (with_urls([BAD_DIGEST_WHEEL, INDEX_OK["urls"][1]]), LOCAL_DIGESTS),
        (with_urls([{**INDEX_OK["urls"][0], "yanked": True}, INDEX_OK["urls"][1]]), LOCAL_DIGESTS),
        (INDEX_OK, {"mcp_server_edgar-0.1.0-py2-none-any.whl": WHEEL_SHA, SDIST_FILE: SDIST_SHA}),
    ],
)
def test_index_release_fails_on_each_deviation(payload: dict, local: dict[str, str]) -> None:
    ok, observed = mod.check_index_release(payload, local, VERSION, DESCRIPTION)
    assert not ok, observed


def test_index_release_accepts_the_underscore_spelling_and_an_empty_payload() -> None:
    underscore = with_info(name="mcp_server_edgar")
    assert mod.check_index_release(underscore, LOCAL_DIGESTS, VERSION, DESCRIPTION)[0]
    ok, observed = mod.check_index_release({}, LOCAL_DIGESTS, VERSION, DESCRIPTION)
    assert not ok and "files=0" in observed and "digests_match=False" in observed


def test_row_titles_default_and_from_pypi() -> None:
    default = mod.row_titles(False, ["3.11"])
    assert len(default) == 11 and default[0] == "dist/ contents"
    assert default[-2:] == ["smoke (current interpreter)", "smoke (--python 3.11)"]
    index = mod.row_titles(True, ["3.11"])
    assert len(index) == 12 and index[:9] == default[:9] and index[9] == "PyPI release record"
    assert index[-2] == "smoke from PyPI (current interpreter)"
    assert index[-1] == "smoke from PyPI (--python 3.11)"
    assert len(mod.row_titles(True, ["3.12", "3.13"])) == 13


# --- CLI plumbing --------------------------------------------------------------------------


def test_parse_args_defaults() -> None:
    args = mod.parse_args([])
    assert args.pythons == ["3.11"] and not args.no_twine and not args.no_smoke and not args.build
    assert args.dist == ROOT / "dist"


def test_parse_args_overrides() -> None:
    args = mod.parse_args(["--no-smoke", "--no-twine", "--python", "3.12", "--python", "3.13"])
    assert args.pythons == ["3.12", "3.13"] and args.no_twine and args.no_smoke


def test_parse_args_from_pypi_and_its_build_conflict() -> None:
    assert not mod.parse_args([]).from_pypi
    assert mod.parse_args(["--from-pypi"]).from_pypi
    with pytest.raises(SystemExit):
        mod.parse_args(["--from-pypi", "--build"])


def test_main_from_pypi_still_needs_dist(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    assert mod.main(["--from-pypi", "--dist", str(tmp_path / "nowhere"), "--no-smoke"]) == 2
    assert "missing or stale" in capsys.readouterr().err


def test_main_exits_two_on_a_missing_dist(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    assert mod.main(["--dist", str(tmp_path / "nowhere"), "--no-smoke", "--no-twine"]) == 2
    assert "missing or stale" in capsys.readouterr().err


def test_main_exits_two_on_a_stale_dist(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    for name in (f"{STEM}-py3-none-any.whl", "mcp_server_edgar-0.0.1-py3-none-any.whl"):
        (tmp_path / name).write_bytes(b"")
    assert mod.main(["--dist", str(tmp_path), "--no-smoke", "--no-twine"]) == 2
    assert "stale" in capsys.readouterr().err


def test_row_dataclass_and_entry_points_are_callable() -> None:
    row = mod.Row(1, "x", "y", None)
    assert row.judge is None and callable(mod.main) and callable(mod.run_smoke)
