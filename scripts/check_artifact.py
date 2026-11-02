"""Check the built artifact in ``dist/`` and smoke-test it from a clean environment (req_003 v3.02).

    uv run python scripts/check_artifact.py [--build] [--dist PATH] [--python 3.11 ...]
                                            [--no-twine] [--no-smoke]

Eleven rows: dist/ holds exactly one wheel and one sdist; the wheel carries the package, the
form-type catalogue, the licence and the entry point (row 2) and nothing from this repository's
process (row 3); METADATA (rows 4-5) and PKG-INFO (row 8) agree with ``pyproject.toml`` and the
long description carries the Registry marker; the sdist is src + four files (row 7); ``twine
check`` passes (row 9); ``uvx --isolated --refresh --from <wheel> mcp-server-edgar`` launched
from an empty temporary directory answers ``initialize``, lists 3/1/4/3 primitives and serves
``list_form_types`` from the installed wheel, on the current interpreter and on Python 3.11
(rows 10-11).
The Markdown table it prints is the M4 record (as ``inspector_checklist.py`` is for M3/M3b).

Exit codes: 0 all rows pass, 1 a row failed, 2 ``dist/`` missing or stale (not exactly one wheel
and one sdist), ``uv build`` failed, or ``uvx`` not found. No ``from __future__ import
annotations`` here: the file is path-loaded by tests (gotchas #18).
"""

import argparse
import asyncio
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
    "tests/",
    "scripts/",
    "reqspecs/",
    ".claude/",
    "fixtures",
    "__pycache__/",
    ".pyc",
    ".venv/",
    "release_number.md",
    ".code-workspace",
    "uv.lock",
    ".github/",
    "C:",
    "\\",
)
LEAKS = ("Sagentic-Systems", "C:\\", "reqspecs", ".claude")
SDIST_TOP = frozenset({"LICENSE", "PKG-INFO", "README.md", "pyproject.toml", "src"})
MIN_FORM_TYPES = 250
SMOKE_TIMEOUT = 180
TWINE_TIMEOUT = 300
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


def stem(version: str) -> str:
    return f"mcp_server_edgar-{version}"


def dist_info(version: str) -> str:
    return f"{stem(version)}.dist-info"


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


def smoke_command(wheel: Path, python: str | None) -> list[str]:
    python_args = ["--python", python] if python else []
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


async def smoke(wheel: Path, python: str | None) -> dict[str, Any]:
    """Launch the wheel with uvx from an empty directory and observe the server over stdio."""
    started = time.perf_counter()
    with tempfile.TemporaryDirectory() as tmp:
        argv = smoke_command(wheel.resolve(), python)
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


def run_smoke(wheel: Path, python: str | None, version: str) -> Verdict:
    try:
        observed = asyncio.run(asyncio.wait_for(smoke(wheel, python), timeout=SMOKE_TIMEOUT))
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
    args = parser.parse_args(argv)
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


def build_rows(args: argparse.Namespace, version: str, dist_verdict: Verdict) -> list[Row]:
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
    smoke_expectation = (
        f"{SERVER_NAME} {version}; counts 3/1/4/3; list_form_types ok >= {MIN_FORM_TYPES}"
    )
    rows = [
        Row(1, "dist/ contents", "one wheel + one sdist, versioned stems", lambda: dist_verdict),
        Row(
            2,
            "wheel required entries",
            "package, server, catalogue, licences/LICENSE, METADATA, entry_points, WHEEL, RECORD",
            lambda: check_wheel_required(wheel_names, version),
        ),
        Row(
            3,
            "wheel hygiene",
            "two roots only; nothing forbidden; module set == checkout",
            lambda: check_wheel_hygiene(wheel_names, version, modules),
        ),
        Row(
            4,
            "METADATA header",
            f"Name/Version {version}/MIT/LICENSE/>=3.11/3 public URLs/markdown",
            lambda: check_metadata_header(fields, version),
        ),
        Row(
            5,
            "METADATA long description",
            "marker once on its own line; no private strings",
            lambda: check_long_description(body),
        ),
        Row(
            6,
            "entry_points.txt",
            f"[console_scripts] {EXECUTABLE} = {PACKAGE}.server:main",
            lambda: check_entry_points(entry_points),
        ),
        Row(
            7,
            "sdist entries",
            "top level LICENSE/PKG-INFO/README.md/pyproject.toml/src; catalogue; module parity",
            lambda: check_sdist_entries(sdist_names, version, modules),
        ),
        Row(
            8,
            "PKG-INFO",
            f"Name {DIST_NAME}; Version {version}; License-File LICENSE",
            lambda: check_pkg_info(pkg_fields, version),
        ),
        Row(
            9,
            "uvx twine check",
            "PASSED for wheel and sdist",
            None if args.no_twine else lambda: run_twine(wheel, sdist),
        ),
        Row(
            10,
            "smoke (current interpreter)",
            smoke_expectation,
            None if args.no_smoke else lambda: run_smoke(wheel, None, version),
        ),
    ]
    for python in args.pythons:
        judge = None if args.no_smoke else (lambda p=python: run_smoke(wheel, p, version))
        rows.append(Row(len(rows) + 1, f"smoke (--python {python})", smoke_expectation, judge))
    return rows


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    version = expected_version((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
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
    rows = build_rows(args, version, dist_verdict)
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
        print(f"not an M4 record: {skips} rows skipped")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
