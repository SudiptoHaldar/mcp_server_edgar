"""Tests for mcp_edgar.settings: environment parsing, validation, caching, logging."""

import logging
import sys

import pytest

import mcp_edgar
from mcp_edgar.settings import (
    LOG_LEVELS,
    SEC_MAX_REQUESTS_PER_SECOND,
    Settings,
    SettingsError,
    configure_logging,
    default_user_agent,
    get_settings,
    reset_settings_cache,
)

# --- defaults -------------------------------------------------------------------------------


def test_defaults_from_empty_mapping() -> None:
    s = Settings.from_env({})
    assert s.user_agent == default_user_agent()
    assert s.timeout_seconds == 30.0
    assert s.ticker_map_ttl_seconds == 86_400
    assert s.submissions_ttl_seconds == 3_600
    assert s.page_cap == 100
    assert s.max_requests_per_second == SEC_MAX_REQUESTS_PER_SECOND == 10
    assert s.log_level == "INFO"


def test_default_user_agent_carries_version_and_contact() -> None:
    ua = default_user_agent()
    assert mcp_edgar.__version__ in ua
    assert "@" in ua
    assert ua.startswith("mcp-server-edgar/")


def test_empty_string_means_unset() -> None:
    s = Settings.from_env({"MCP_EDGAR_PAGE_CAP": "", "MCP_EDGAR_LOG_LEVEL": "   "})
    assert s.page_cap == 100
    assert s.log_level == "INFO"


# --- overrides ------------------------------------------------------------------------------


def test_overrides_are_parsed_and_typed() -> None:
    s = Settings.from_env(
        {
            "MCP_EDGAR_USER_AGENT": " acme-edgar/2.0 (ops@acme.example) ",
            "MCP_EDGAR_TIMEOUT_SECONDS": "7.5",
            "MCP_EDGAR_TICKER_MAP_TTL_SECONDS": "0",
            "MCP_EDGAR_SUBMISSIONS_TTL_SECONDS": "120",
            "MCP_EDGAR_PAGE_CAP": "25",
            "MCP_EDGAR_MAX_REQUESTS_PER_SECOND": "3",
            "MCP_EDGAR_LOG_LEVEL": "debug",
        }
    )
    assert s.user_agent == "acme-edgar/2.0 (ops@acme.example)"
    assert s.timeout_seconds == 7.5
    assert s.ticker_map_ttl_seconds == 0
    assert s.submissions_ttl_seconds == 120
    assert s.page_cap == 25
    assert s.max_requests_per_second == 3
    assert s.log_level == "DEBUG"


def test_settings_is_frozen() -> None:
    s = Settings.from_env({})
    with pytest.raises(AttributeError):
        s.page_cap = 5  # type: ignore[misc]


# --- validation -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("var", "value"),
    [
        ("MCP_EDGAR_PAGE_CAP", "abc"),
        ("MCP_EDGAR_PAGE_CAP", "0"),
        ("MCP_EDGAR_PAGE_CAP", "1001"),
        ("MCP_EDGAR_MAX_REQUESTS_PER_SECOND", "11"),
        ("MCP_EDGAR_MAX_REQUESTS_PER_SECOND", "0"),
        ("MCP_EDGAR_TIMEOUT_SECONDS", "0"),
        ("MCP_EDGAR_TIMEOUT_SECONDS", "-1"),
        ("MCP_EDGAR_TIMEOUT_SECONDS", "fast"),
        ("MCP_EDGAR_TICKER_MAP_TTL_SECONDS", "-5"),
        ("MCP_EDGAR_SUBMISSIONS_TTL_SECONDS", "1.5"),
        ("MCP_EDGAR_LOG_LEVEL", "VERBOSE"),
        ("MCP_EDGAR_USER_AGENT", "no-contact-here/1.0"),
    ],
)
def test_invalid_values_raise_settings_error_naming_the_variable(var: str, value: str) -> None:
    with pytest.raises(SettingsError) as excinfo:
        Settings.from_env({var: value})
    assert str(excinfo.value).startswith(var + ":")
    assert isinstance(excinfo.value, ValueError)


def test_log_levels_tuple_is_complete() -> None:
    assert LOG_LEVELS == ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


# --- cached accessor ------------------------------------------------------------------------


def test_get_settings_is_cached_until_reset(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_settings_cache()
    monkeypatch.setenv("MCP_EDGAR_PAGE_CAP", "42")
    first = get_settings()
    assert first.page_cap == 42
    monkeypatch.setenv("MCP_EDGAR_PAGE_CAP", "7")
    assert get_settings() is first
    reset_settings_cache()
    assert get_settings().page_cap == 7


# --- logging --------------------------------------------------------------------------------


def test_configure_logging_installs_single_stderr_handler() -> None:
    configure_logging("WARNING")
    configure_logging("WARNING")  # idempotent thanks to force=True
    root = logging.getLogger()
    assert root.level == logging.WARNING
    assert len(root.handlers) == 1
    handler = root.handlers[0]
    assert isinstance(handler, logging.StreamHandler)
    assert handler.stream is sys.stderr


def test_logging_goes_to_stderr_not_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO")
    logging.getLogger("mcp_edgar.test").info("hello from the test")
    out, err = capsys.readouterr()
    assert out == ""
    assert "hello from the test" in err
    assert "INFO mcp_edgar.test" in err


def test_configure_logging_respects_level(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("ERROR")
    logging.getLogger("mcp_edgar.test").warning("suppressed")
    logging.getLogger("mcp_edgar.test").error("shown")
    out, err = capsys.readouterr()
    assert out == ""
    assert "suppressed" not in err
    assert "shown" in err
