"""Check the built artifact in ``dist/`` and smoke-test it from a clean environment (req_003 v3.02).

    uv run python scripts/check_artifact.py [--build] [--dist PATH] [--python 3.11 ...]
                                            [--no-twine] [--no-smoke] [--from-pypi]

Default mode, eleven rows (the M4 record): one wheel + one sdist in dist/ (row 1); wheel entries
and hygiene (rows 2-3); METADATA header and long description with the Registry marker (rows 4-5);
entry point (row 6); sdist entries (row 7); PKG-INFO (row 8); ``twine check`` (row 9); ``uvx
--isolated --refresh --from <wheel> mcp-server-edgar`` from an empty temporary directory answers
``initialize``, lists 3/1/4/3 primitives and serves ``list_form_types`` from the installed wheel,
on the current interpreter and on Python 3.11 (rows 10-11).

``--from-pypi`` (req_005 v5.01), twelve rows (the M6 record): rows 1-9 over the uploaded files;
row 10 fetches the PyPI JSON release record and requires its two files to equal dist/ by name and
SHA-256 (plus version, summary, requires-python, public URLs, not yanked); rows 11-12 smoke
``uvx --isolated --refresh mcp-server-edgar@<version>`` from the index. Refuses ``--build``.

Exit codes: 0 all rows pass, 1 a row failed, 2 dist/ missing or stale, ``uv build`` failed, or
``uvx`` not found. No ``from __future__ import annotations``: path-loaded by tests (gotchas #18).
"""

import argparse
import asyncio
import hashlib
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]
DIST_NAME = "mcp-server-edgar"
PACKAGE = "mcp_edgar"
EXECUTABLE = "mcp-server-edgar"
MARKER = "<!-- mcp-name: io.github.SudiptoHaldar/mcp-server-edgar -->"
PUBLIC_REPO = "https://github.com/SudiptoHaldar/mcp_server_edgar"
SERVER_NAME = "SEC EDGAR Filings"
FORBIDDEN = (
    *("tests/", "scripts/", "reqspecs/", ".claude/", "fixtures", "__pycache__/", ".pyc"),
    *(".venv/", "release_number.md", ".code-workspace", "uv.lock", ".github/", "C:", "\\"),
)
LEAKS = ("Sagentic-Systems", "C:\\", "reqspecs", ".claude")
SDIST_TOP = frozenset({"LICENSE", "PKG-INFO", "README.md", "pyproject.toml", "src"})
MIN_FORM_TYPES = 250
SMOKE_TIMEOUT = 180
TWINE_TIMEOUT = 300
INDEX_JSON = "https://pypi.org/pypi/{name}/{version}/json"
INDEX_TIMEOUT = 30
DEFAULT_PYTHONS = ["3.11"]

Verdict = tuple[bool, str]
Fields = dict[str, list[str]]


@dataclass(frozen=True)
class Row:
    number: int
    check: str
    expectation: str
    judge: Callable[[], Verdict] | None  # None: the row is skipped (--no-twine / --no-smoke)


# --- pure judges --------------------------------------------------------------------------


def expected_version(pyproject_text: str) -> str:
    return str(tomllib.loads(pyproject_text)["project"]["version"])


def expected_description(pyproject_text: str) -> str:
    return str(tomllib.loads(pyproject_text)["project"]["description"])


def stem(version: str) -> str:
    return f"mcp_server_edgar-{version}"


def dist_info(version: str) -> str:
    return f"{stem(version)}.dist-info"


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check_dist_files(names: list[str], version: str) -> Verdict:
    wheels = sorted(n for n in names if n.endswith(".whl"))
    sdists = sorted(n for n in names if n.endswith(".tar.gz"))
    ok = wheels == [f"{stem(version)}-py3-none-any.whl"] and sdists == [f"{stem(version)}.tar.gz"]
    note = "" if ok else "; stale or missing artifacts (expected exactly one wheel and one sdist)"
    return ok, f"wheels={wheels}; sdists={sdists}{note}"


def modules_in(names: list[str], prefix: str) -> set[str]:
    """Top-level ``*.py`` names directly under ``prefix`` (no sub-packages today)."""
    tails = (n[len(prefix) :] for n in names if n.startswith(prefix) and n.endswith(".py"))
    return {t for t in tails if "/" not in t}


def forbidden_in(names: list[str]) -> list[str]:
    return [n for n in names if any(token in n for token in FORBIDDEN)]


def check_wheel_required(names: list[str], version: str) -> Verdict:
    di = dist_info(version)
    required = [
        f"{PACKAGE}/__init__.py",
        f"{PACKAGE}/server.py",
        f"{PACKAGE}/data/form_types.json",
        f"{di}/licenses/LICENSE",
        f"{di}/METADATA",
        f"{di}/entry_points.txt",
        f"{di}/WHEEL",
        f"{di}/RECORD",
    ]
    missing = [r for r in required if r not in names]
    return not missing, f"entries={len(names)}; missing={missing}"


def check_wheel_hygiene(names: list[str], version: str, modules: set[str]) -> Verdict:
    di = dist_info(version)
    outside = [n for n in names if not n.startswith((f"{PACKAGE}/", f"{di}/"))]
    forbidden = forbidden_in(names)
    shipped = modules_in(names, f"{PACKAGE}/")
    ok = not outside and not forbidden and shipped == modules
    return ok, (
        f"modules={len(shipped)}; outside={outside}; forbidden={forbidden}; "
        f"module_diff={sorted(shipped ^ modules)}"
    )


def parse_metadata(text: str) -> tuple[Fields, str]:
    """Header fields (multi-valued) and the long-description body of METADATA / PKG-INFO."""
    header, _, body = text.partition("\n\n")
    fields: Fields = {}
    for line in header.splitlines():
        key, sep, value = line.partition(": ")
        if sep:
            fields.setdefault(key, []).append(value)
    return fields, body


def check_metadata_header(fields: Fields, version: str) -> Verdict:
    want = {
        "Name": [DIST_NAME],
        "Version": [version],
        "License-Expression": ["MIT"],
        "License-File": ["LICENSE"],
        "Requires-Python": [">=3.11"],
        "Description-Content-Type": ["text/markdown"],
    }
    bad = [key for key, value in want.items() if fields.get(key) != value]
    urls = fields.get("Project-URL", [])
    urls_ok = len(urls) == 3 and all(PUBLIC_REPO in url for url in urls)
    return not bad and urls_ok, f"bad_fields={bad}; project_urls={len(urls)}; public={urls_ok}"


def check_long_description(body: str) -> Verdict:
    marker_once = body.count(MARKER) == 1 and MARKER in body.splitlines()
    leaks = [token for token in LEAKS if token in body]
    ok = marker_once and not leaks
    return ok, f"lines={len(body.splitlines())}; marker_once={marker_once}; leaks={leaks}"


def check_entry_points(text: str) -> Verdict:
    ok = text.strip() == f"[console_scripts]\n{EXECUTABLE} = {PACKAGE}.server:main"
    return ok, f"entry_points={text.strip()!r}"


def check_sdist_entries(names: list[str], version: str, modules: set[str]) -> Verdict:
    top = {n.split("/")[1] for n in names if n.count("/") == 1}
    prefix = f"{stem(version)}/src/{PACKAGE}/"
    shipped = modules_in(names, prefix)
    has_data = f"{prefix}data/form_types.json" in names
    forbidden = forbidden_in(names)
    ok = top == SDIST_TOP and has_data and not forbidden and shipped == modules
    return ok, (
        f"top={sorted(top)}; modules={len(shipped)}; catalogue={has_data}; "
        f"forbidden={forbidden}; module_diff={sorted(shipped ^ modules)}"
    )


def check_pkg_info(fields: Fields, version: str) -> Verdict:
    ok = (
        fields.get("Name") == [DIST_NAME]
        and fields.get("Version") == [version]
        and fields.get("License-File") == ["LICENSE"]
    )
    return ok, f"name={fields.get('Name')}; version={fields.get('Version')}"


def twine_passed(stdout: str, returncode: int) -> Verdict:
    passed = stdout.count("PASSED")
    return returncode == 0 and passed == 2, f"rc={returncode}; passed_lines={passed}"


def check_index_release(
    payload: dict, local_digests: dict[str, str], version: str, description: str
) -> Verdict:
    """Row 10: the release record on pypi.org names exactly the local files, byte for byte."""
    info = payload.get("info") or {}
    urls = payload.get("urls") or []
    remote = {u.get("filename"): (u.get("digests") or {}).get("sha256") for u in urls}
    types = sorted(str(u.get("packagetype", "")) for u in urls)
    yanked = [u.get("filename") for u in urls if u.get("yanked")]
    project_urls = list((info.get("project_urls") or {}).values())
    ok = (
        info.get("version") == version
        and str(info.get("name", "")).lower().replace("_", "-") == DIST_NAME
        and info.get("summary") == description
        and info.get("requires_python") == ">=3.11"
        and len(project_urls) == 3
        and all(PUBLIC_REPO in url for url in project_urls)
        and remote == local_digests
        and types == ["bdist_wheel", "sdist"]
        and not yanked
    )
    uploaded = max((str(u.get("upload_time_iso_8601", "")) for u in urls), default="")
    return ok, (
        f"index={info.get('name')} {info.get('version')}; files={len(urls)}; "
        f"digests_match={remote == local_digests}; types={types}; yanked={len(yanked)}; "
        f"uploaded={uploaded}"
    )


def smoke_command(wheel: Path | None, python: str | None, version: str) -> list[str]:
    """``--from <wheel>`` for the local artifact; ``name@version`` resolves from the index."""
    python_args = ["--python", python] if python else []
    if wheel is None:
        return ["uvx", "--isolated", "--refresh", *python_args, f"{DIST_NAME}@{version}"]
    return ["uvx", "--isolated", "--refresh", "--from", str(wheel), *python_args, EXECUTABLE]


def judge_smoke(observed: dict[str, Any], version: str) -> Verdict:
    forms = observed.get("forms") or {}
    ok = (
        observed.get("name") == SERVER_NAME
        and observed.get("version") == version
        and bool(observed.get("protocol"))
        and observed.get("instructions", 0) > 0
        and tuple(observed.get("counts", ())) == (3, 1, 4, 3)
        and forms.get("status") == "ok"
        and forms.get("total_count", 0) >= MIN_FORM_TYPES
        and observed.get("seconds", float("inf")) < 120
    )
    return ok, (
        f"server={observed.get('name')} {observed.get('version')}; "
        f"protocol={observed.get('protocol')}; counts={observed.get('counts')}; "
        f"form_types={forms.get('status')} {forms.get('total_count')}; "
        f"{observed.get('seconds', 0.0):.1f}s"
    )


def row_titles(from_pypi: bool, pythons: list[str]) -> list[str]:
    """The row sequence of both modes: 11 rows by default, 12 with ``--from-pypi``."""
    base = [
        *("dist/ contents", "wheel required entries", "wheel hygiene", "METADATA header"),
        *("METADATA long description", "entry_points.txt", "sdist entries", "PKG-INFO"),
        "uvx twine check",
    ]
    prefix = "smoke from PyPI" if from_pypi else "smoke"
    extra = ["PyPI release record"] if from_pypi else []
    smokes = [f"{prefix} (current interpreter)", *(f"{prefix} (--python {p})" for p in pythons)]
    return [*base, *extra, *smokes]


# --- subprocess-backed rows ---------------------------------------------------------------


def run_twine(wheel: Path, sdist: Path) -> Verdict:
    try:
        completed = subprocess.run(
            ["uvx", "twine", "check", str(wheel), str(sdist)],
            capture_output=True,
            text=True,
            timeout=TWINE_TIMEOUT,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return False, f"twine failed: {exc}"
    passed, observed = twine_passed(completed.stdout, completed.returncode)
    tail = "" if passed else "; " + " / ".join(completed.stderr.strip().splitlines()[-2:])
    return passed, observed + tail


def run_index_row(version: str, local_digests: dict[str, str], description: str) -> Verdict:
    url = INDEX_JSON.format(name=DIST_NAME, version=version)
    try:
        headers = {"Accept": "application/json"}
        response = httpx.get(url, timeout=INDEX_TIMEOUT, follow_redirects=True, headers=headers)
    except httpx.HTTPError as exc:
        return False, f"index fetch failed: {type(exc).__name__}: {exc}"
    if response.status_code == 404:
        return False, "not on the index yet (404): PyPI propagation takes up to ~1 min, re-run"
    if response.status_code != 200:
        return False, f"index answered HTTP {response.status_code}"
    return check_index_release(response.json(), local_digests, version, description)


async def smoke(wheel: Path | None, python: str | None, version: str) -> dict[str, Any]:
    """Launch the wheel (or the index release) with uvx from an empty directory and observe it."""
    started = time.perf_counter()
    with tempfile.TemporaryDirectory() as tmp:
        argv = smoke_command(None if wheel is None else wheel.resolve(), python, version)
        params = StdioServerParameters(
            command=argv[0], args=argv[1:], cwd=tmp, env={"MCP_EDGAR_LOG_LEVEL": "WARNING"}
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                counts = (
                    len((await session.list_tools()).tools),
                    len((await session.list_resources()).resources),
                    len((await session.list_resource_templates()).resource_templates),
                    len((await session.list_prompts()).prompts),
                )
                forms = await session.call_tool("list_form_types", {"limit": 1})
    return {
        "name": init.server_info.name,
        "version": init.server_info.version,
        "protocol": init.protocol_version or "",
        "instructions": len(init.instructions or ""),
        "counts": counts,
        "forms": forms.structured_content or {},
        "seconds": time.perf_counter() - started,
    }


def run_smoke(wheel: Path | None, python: str | None, version: str) -> Verdict:
    try:
        observed = asyncio.run(
            asyncio.wait_for(smoke(wheel, python, version), timeout=SMOKE_TIMEOUT)
        )
    except Exception as exc:  # the SDK raises ExceptionGroup; its text names the cause
        return False, f"smoke failed: {type(exc).__name__}: {exc}"
    return judge_smoke(observed, version)


# --- CLI ----------------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check dist/ and smoke-test the wheel with uvx.")
    parser.add_argument("--build", action="store_true", help="remove dist/ and run uv build first")
    parser.add_argument("--dist", type=Path, default=ROOT / "dist", help="artifact directory")
    parser.add_argument(
        "--python", dest="pythons", action="append", help="interpreter(s) for extra smoke rows"
    )
    parser.add_argument("--no-twine", action="store_true", help="skip the twine check row")
    parser.add_argument("--no-smoke", action="store_true", help="skip the uvx smoke rows")
    parser.add_argument(
        "--from-pypi",
        action="store_true",
        help="row 10 compares dist/ to pypi.org; the smoke rows install from the index",
    )
    args = parser.parse_args(argv)
    if args.from_pypi and args.build:
        parser.error("--from-pypi judges the files that were uploaded; do not rebuild them")
    if args.pythons is None:
        args.pythons = list(DEFAULT_PYTHONS)
    return args


def read_zip_member(wheel: Path, name: str) -> str:
    with zipfile.ZipFile(wheel) as archive:
        return archive.read(name).decode("utf-8") if name in archive.namelist() else ""


def read_tar_member(sdist: Path, name: str) -> str:
    with tarfile.open(sdist) as archive:
        if name not in archive.getnames():
            return ""
        member = archive.extractfile(name)
        return member.read().decode("utf-8") if member else ""


def build_rows(
    args: argparse.Namespace, version: str, description: str, dist_verdict: Verdict
) -> list[Row]:
    wheel = args.dist / f"{stem(version)}-py3-none-any.whl"
    sdist = args.dist / f"{stem(version)}.tar.gz"
    di = dist_info(version)
    modules = {p.name for p in (ROOT / "src" / PACKAGE).glob("*.py")}
    with zipfile.ZipFile(wheel) as archive:
        wheel_names = archive.namelist()
    with tarfile.open(sdist) as archive:
        sdist_names = archive.getnames()
    fields, body = parse_metadata(read_zip_member(wheel, f"{di}/METADATA"))
    entry_points = read_zip_member(wheel, f"{di}/entry_points.txt")
    pkg_fields, _ = parse_metadata(read_tar_member(sdist, f"{stem(version)}/PKG-INFO"))
    judges: list[tuple[str, Callable[[], Verdict] | None]] = [
        ("one wheel + one sdist, versioned stems", lambda: dist_verdict),
        (
            "package, server, catalogue, licences/LICENSE, METADATA, entry_points, WHEEL, RECORD",
            lambda: check_wheel_required(wheel_names, version),
        ),
        (
            "two roots only; nothing forbidden; module set == checkout",
            lambda: check_wheel_hygiene(wheel_names, version, modules),
        ),
        (
            f"Name/Version {version}/MIT/LICENSE/>=3.11/3 public URLs/markdown",
            lambda: check_metadata_header(fields, version),
        ),
        ("marker once on its own line; no private strings", lambda: check_long_description(body)),
        (
            f"[console_scripts] {EXECUTABLE} = {PACKAGE}.server:main",
            lambda: check_entry_points(entry_points),
        ),
        (
            "top level LICENSE/PKG-INFO/README.md/pyproject.toml/src; catalogue; module parity",
            lambda: check_sdist_entries(sdist_names, version, modules),
        ),
        (
            f"Name {DIST_NAME}; Version {version}; License-File LICENSE",
            lambda: check_pkg_info(pkg_fields, version),
        ),
        ("PASSED for wheel and sdist", None if args.no_twine else lambda: run_twine(wheel, sdist)),
    ]
    if args.from_pypi:
        digests = {wheel.name: sha256_of(wheel), sdist.name: sha256_of(sdist)}
        judges.append(
            (
                "two files on pypi.org == dist/ by name and sha256; version/summary/>=3.11/"
                "3 public URLs; not yanked",
                lambda: run_index_row(version, digests, description),
            )
        )
    launch = None if args.from_pypi else wheel
    smoke_expectation = (
        f"{SERVER_NAME} {version}; counts 3/1/4/3; list_form_types ok >= {MIN_FORM_TYPES}"
    )
    for python in [None, *args.pythons]:
        judge = None if args.no_smoke else (lambda p=python: run_smoke(launch, p, version))
        judges.append((smoke_expectation, judge))
    titles = row_titles(args.from_pypi, args.pythons)
    return [
        Row(number, title, expectation, judge)
        for number, (title, (expectation, judge)) in enumerate(zip(titles, judges, strict=True), 1)
    ]


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    pyproject_text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    version = expected_version(pyproject_text)
    description = expected_description(pyproject_text)
    if args.build:
        shutil.rmtree(args.dist, ignore_errors=True)
        built = subprocess.run(["uv", "build", "--out-dir", str(args.dist)], check=False)
        if built.returncode != 0:
            print(f"uv build failed with exit {built.returncode}", file=sys.stderr)
            return 2
    names = sorted(p.name for p in args.dist.iterdir()) if args.dist.is_dir() else []
    dist_verdict = check_dist_files(names, version)
    if not dist_verdict[0]:
        print(f"dist/ is missing or stale: {dist_verdict[1]}", file=sys.stderr)
        return 2
    if not (args.no_twine and args.no_smoke) and shutil.which("uvx") is None:
        print("uvx not found: install uv (https://docs.astral.sh/uv/)", file=sys.stderr)
        return 2
    rows = build_rows(args, version, description, dist_verdict)
    lines = ["| # | Check | Expectation | Observed | Result |", "|---|---|---|---|---|"]
    passes = fails = skips = 0
    for row in rows:
        if row.judge is None:
            verdict, observed = "SKIP", "skipped by flag"
            skips += 1
        else:
            passed, observed = row.judge()
            verdict = "PASS" if passed else "FAIL"
            passes += passed
            fails += not passed
        lines.append(f"| {row.number} | {row.check} | {row.expectation} | {observed} | {verdict} |")
    print("\n".join(lines))
    suffix = f" ({skips} skipped)" if skips else ""
    print(f"\n{'PASS' if fails == 0 else 'FAIL'} {passes}/{len(rows)}{suffix}")
    if skips:
        print(f"not an {'M6' if args.from_pypi else 'M4'} record: {skips} rows skipped")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
