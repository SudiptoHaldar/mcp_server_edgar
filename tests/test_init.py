"""Package-level checks for mcp_edgar."""

import tomllib
from pathlib import Path

import mcp_edgar


def test_version_matches_pyproject() -> None:
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    assert data["project"]["version"] == mcp_edgar.__version__
