"""Source hygiene guards for src/mcp_edgar.

stdout is the MCP wire over stdio (reqspecs/references/common/gotchas.md #1), so no module may
call print() or touch sys.stdout. Only settings.py may read the environment.
"""

import ast
import re
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parents[1] / "src" / "mcp_edgar"
ENV_PATTERN = re.compile(r"os\.environ|getenv")


def source_files() -> list[Path]:
    files = sorted(SRC_DIR.rglob("*.py"))
    assert files, f"no source files found under {SRC_DIR}"
    return files


def find_stdout_violations(source: str, filename: str) -> list[str]:
    """Return human-readable locations of print() calls and sys.stdout access in ``source``."""
    tree = ast.parse(source, filename)
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            is_print_name = isinstance(func, ast.Name) and func.id == "print"
            is_builtins_print = (
                isinstance(func, ast.Attribute)
                and func.attr == "print"
                and isinstance(func.value, ast.Name)
                and func.value.id == "builtins"
            )
            if is_print_name or is_builtins_print:
                violations.append(f"{filename}:{node.lineno}: print() call")
        if (
            isinstance(node, ast.Attribute)
            and node.attr == "stdout"
            and isinstance(node.value, ast.Name)
            and node.value.id == "sys"
        ):
            violations.append(f"{filename}:{node.lineno}: sys.stdout access")
    return violations


def test_no_print_or_stdout_in_src() -> None:
    violations: list[str] = []
    for path in source_files():
        violations.extend(find_stdout_violations(path.read_text(encoding="utf-8"), path.name))
    assert violations == []


def test_checker_detects_print_and_stdout() -> None:
    snippet = (
        '"""Docstring that mentions print and stdout is fine."""\n'
        "import builtins, sys\n"
        "def f():\n"
        '    print("x")\n'
        '    builtins.print("y")\n'
        '    sys.stdout.write("z")\n'
    )
    found = find_stdout_violations(snippet, "snippet.py")
    assert [v.split(": ", 1)[1] for v in found] == [
        "print() call",
        "print() call",
        "sys.stdout access",
    ]


def test_checker_ignores_docstring_mentions() -> None:
    snippet = '"""never print here; stdout is the wire."""\nVALUE = "print(stdout)"\n'
    assert find_stdout_violations(snippet, "snippet.py") == []


def test_only_settings_reads_the_environment() -> None:
    offenders = [
        path.name
        for path in source_files()
        if path.name != "settings.py" and ENV_PATTERN.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []
