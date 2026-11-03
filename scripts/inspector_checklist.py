"""Run the P2/P2b checklist through the MCP Inspector CLI against the real server.

Each row of ``inspector_checks.ROWS`` launches ``npx -y @modelcontextprotocol/inspector --cli uv
run --directory <repo> mcp-server-edgar --method ...`` (the same Inspector package that ``mcp dev``
opens in the browser), checks the JSON result, ``HEAD``s the sec.gov links, and prints a Markdown
table that is pasted into the implementation summary (milestones M3, M3b). Needs Node (``npx``)
and network.

    uv run python scripts/inspector_checklist.py                 # this source tree
    uv run python scripts/inspector_checklist.py --from-pypi     # uvx mcp-server-edgar@<version>

``--from-pypi`` (req_005 v5.03) launches the version this tree declares from the index instead of
the tree, for the after-publish checklist; run ``uv cache clean mcp-server-edgar`` first so the
first row downloads it. Exit codes: 0 all rows pass, 1 at least one row failed, 2 ``npx`` not
found. The rows and checks live in ``scripts/inspector_checks.py`` (req_006 v6.02 split); this
file is the runner only. No ``from __future__ import annotations`` in either file (path-loaded by
tests, gotchas #18).
"""

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from inspector_checks import ROWS, Row, render

ROOT = Path(__file__).resolve().parents[1]
INSPECTOR = "@modelcontextprotocol/inspector"
TIMEOUT_SECONDS = 180
PAUSE_SECONDS = 0.2

Result = dict[str, Any]


def cli_error(stderr: str) -> dict[str, Any] | None:
    """The ``error`` object of the one-line ``{"error": {...}}`` the CLI prints, if any."""
    for line in stderr.splitlines():
        if line.startswith('{"error"'):
            try:
                error = json.loads(line).get("error")
            except ValueError:
                return {"message": line}
            return error if isinstance(error, dict) else {"message": str(error)}
    return None


def launcher(from_pypi: bool) -> list[str]:
    """How the Inspector starts the server: this source tree, or the declared version from PyPI."""
    if not from_pypi:
        return ["uv", "run", "--directory", str(ROOT), "mcp-server-edgar"]
    import mcp_edgar  # lazy: only the index launch needs the version this tree declares

    return ["uvx", f"mcp-server-edgar@{mcp_edgar.__version__}"]


def command(npx: str, row: Row, launch: list[str] | None = None) -> list[str]:
    """The Inspector CLI invocation for one row."""
    cmd = [npx, "-y", INSPECTOR, "--cli", *(launch or launcher(False))]
    if row.method == "tools/call" and row.tool is not None:
        cmd += ["--method", "tools/call", "--tool-name", row.tool]
        for key, value in row.args.items():
            cmd += ["--tool-arg", f"{key}={render(value)}"]
    elif row.method == "resources/read":
        cmd += ["--method", "resources/read", "--uri", str(row.uri)]
    elif row.method == "prompts/get":
        cmd += ["--method", "prompts/get", "--prompt-name", str(row.tool)]
        for key, value in (row.prompt_args or {}).items():
            cmd += ["--prompt-args", f"{key}={value}"]
    else:
        cmd += ["--method", row.method]
    return cmd


def run_inspector(npx: str, row: Row, launch: list[str] | None = None) -> Result:
    completed = subprocess.run(
        command(npx, row, launch),
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
        check=False,
    )
    if completed.returncode != 0 and "tool_is_error" in completed.stderr:
        # The CLI exits 5 and prints no result when the tool answered isError:true; the server's
        # forwarded stderr (e.g. "rejected arguments: ['year']") is the only content available.
        return {"isError": True, "content": [{"type": "text", "text": completed.stderr}]}
    error = cli_error(completed.stderr) if completed.returncode != 0 else None
    if error is not None:
        # A resource or prompt error: exit 1 and {"error": {"code": ..., "message": ...}}.
        return {"isError": True, "error": error}
    if completed.returncode != 0 or not completed.stdout.strip():
        tail = " / ".join(completed.stderr.strip().splitlines()[-3:])
        raise RuntimeError(f"inspector exit {completed.returncode}: {tail}")
    return json.loads(completed.stdout)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Inspector checklist against the server.")
    parser.add_argument(
        "--from-pypi",
        action="store_true",
        help="launch uvx mcp-server-edgar@<declared version> from PyPI instead of this tree",
    )
    args = parser.parse_args(argv)
    npx = shutil.which("npx")
    if npx is None:
        print("npx not found: install Node.js (the MCP Inspector runs on Node)", file=sys.stderr)
        return 2
    launch = launcher(args.from_pypi)
    print(f"Launcher: {' '.join(launch)}")
    state: dict[str, Any] = {}
    lines = ["| # | Call | Expectation | Observed | Result |", "|---|---|---|---|---|"]
    failures = 0
    for row in ROWS:
        try:
            passed, observed = row.check(run_inspector(npx, row, launch), state)
        except (RuntimeError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
            passed, observed = False, f"run failed: {exc}"
        failures += not passed
        verdict = "PASS" if passed else "FAIL"
        lines.append(
            f"| {row.number} | `{row.describe()}` | {row.expectation} | {observed} | {verdict} |"
        )
        time.sleep(PAUSE_SECONDS)
    print("\n".join(lines))
    print(f"\n{'PASS' if failures == 0 else 'FAIL'} {len(ROWS) - failures}/{len(ROWS)}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
