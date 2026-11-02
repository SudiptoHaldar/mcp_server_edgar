"""Runtime configuration (environment variables with defaults) and stderr-only logging.

This is the only module that reads the process environment. Every tunable is an
``MCP_EDGAR_*`` variable; an unset or empty variable takes its default. Helpers receive a
``Settings`` value as an argument rather than calling :func:`get_settings` themselves.
"""

from __future__ import annotations

import functools
import logging
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass

from mcp_edgar import __version__

DEFAULT_CONTACT = "sudiptokanti@yahoo.com"
ENV_PREFIX = "MCP_EDGAR_"
SEC_MAX_REQUESTS_PER_SECOND = 10
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_TICKER_MAP_TTL_SECONDS = 86_400
DEFAULT_SUBMISSIONS_TTL_SECONDS = 3_600
DEFAULT_PAGE_CAP = 100
MAX_PAGE_CAP = 1_000
MAX_TTL_SECONDS = 10**9
DEFAULT_LOG_LEVEL = "INFO"

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


class SettingsError(ValueError):
    """An environment variable holds an unusable value. The message starts with its name."""


def default_user_agent() -> str:
    """SEC fair-access User-Agent: product/version plus maintainer contact."""
    return f"mcp-server-edgar/{__version__} ({DEFAULT_CONTACT})"


def _get(env: Mapping[str, str], var: str) -> str | None:
    raw = env.get(var, "").strip()
    return raw or None


def _int(var: str, raw: str, lo: int, hi: int) -> int:
    message = f"{var}: expected an integer between {lo} and {hi}, got {raw!r}"
    try:
        value = int(raw)
    except ValueError:
        raise SettingsError(message) from None
    if not lo <= value <= hi:
        raise SettingsError(message)
    return value


def _float_positive(var: str, raw: str) -> float:
    message = f"{var}: expected a number greater than 0, got {raw!r}"
    try:
        value = float(raw)
    except ValueError:
        raise SettingsError(message) from None
    if not value > 0:
        raise SettingsError(message)
    return value


def _level(var: str, raw: str) -> str:
    value = raw.upper()
    if value not in LOG_LEVELS:
        raise SettingsError(f"{var}: expected one of {', '.join(LOG_LEVELS)}, got {raw!r}")
    return value


def _user_agent(var: str, raw: str) -> str:
    if "@" not in raw:
        raise SettingsError(
            f"{var}: must include a maintainer contact e-mail, e.g. "
            f"'mcp-server-edgar/{__version__} (you@example.com)', got {raw!r}"
        )
    return raw


@dataclass(frozen=True, slots=True)
class Settings:
    """Immutable snapshot of the server configuration."""

    user_agent: str
    timeout_seconds: float
    ticker_map_ttl_seconds: int
    submissions_ttl_seconds: int
    page_cap: int
    max_requests_per_second: int
    log_level: str

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        """Build settings from ``env`` (default: ``os.environ``). Pure; raises SettingsError."""
        source: Mapping[str, str] = os.environ if env is None else env

        def pick(suffix: str) -> tuple[str, str | None]:
            var = ENV_PREFIX + suffix
            return var, _get(source, var)

        var, raw = pick("USER_AGENT")
        user_agent = _user_agent(var, raw) if raw else default_user_agent()

        var, raw = pick("TIMEOUT_SECONDS")
        timeout = _float_positive(var, raw) if raw else DEFAULT_TIMEOUT_SECONDS

        var, raw = pick("TICKER_MAP_TTL_SECONDS")
        ticker_ttl = _int(var, raw, 0, MAX_TTL_SECONDS) if raw else DEFAULT_TICKER_MAP_TTL_SECONDS

        var, raw = pick("SUBMISSIONS_TTL_SECONDS")
        subs_ttl = _int(var, raw, 0, MAX_TTL_SECONDS) if raw else DEFAULT_SUBMISSIONS_TTL_SECONDS

        var, raw = pick("PAGE_CAP")
        page_cap = _int(var, raw, 1, MAX_PAGE_CAP) if raw else DEFAULT_PAGE_CAP

        var, raw = pick("MAX_REQUESTS_PER_SECOND")
        rps = (
            _int(var, raw, 1, SEC_MAX_REQUESTS_PER_SECOND)
            if raw
            else SEC_MAX_REQUESTS_PER_SECOND
        )

        var, raw = pick("LOG_LEVEL")
        level = _level(var, raw) if raw else DEFAULT_LOG_LEVEL

        return cls(
            user_agent=user_agent,
            timeout_seconds=timeout,
            ticker_map_ttl_seconds=ticker_ttl,
            submissions_ttl_seconds=subs_ttl,
            page_cap=page_cap,
            max_requests_per_second=rps,
            log_level=level,
        )


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings, read from ``os.environ`` once."""
    return Settings.from_env()


def reset_settings_cache() -> None:
    """Forget the cached settings (tests only)."""
    get_settings.cache_clear()


def configure_logging(level: str) -> None:
    """Route all logging to stderr at ``level``.

    ``force=True`` replaces the Rich handler the MCP SDK installs when ``MCPServer`` is
    constructed. stdout is never touched: it carries the MCP wire over stdio.
    """
    logging.basicConfig(level=level, stream=sys.stderr, format=LOG_FORMAT, force=True)
