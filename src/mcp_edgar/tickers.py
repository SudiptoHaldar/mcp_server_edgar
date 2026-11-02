"""Ticker → issuer resolution over the SEC ticker map (company_tickers_exchange.json).

Both MCP tools start here. ``TickerResolver.resolve`` turns user input into exactly one
``Issuer`` with a 10-digit CIK or raises one of four typed outcomes: ``InvalidTicker`` (bad
input, no I/O performed), ``UnknownTicker`` (nothing listed; no ``data.sec.gov`` request is ever
made), ``AmbiguousTicker`` (one ticker under several CIKs; candidates attached), ``InvalidCik``
(an optional ``cik`` that is malformed or not listed under the ticker). A CIK is never guessed
(gotchas #3, #4): a caller may *select* one of the listed candidates, never supply an arbitrary
one. Network access goes through ``SecClient`` only.
"""

from __future__ import annotations

import dataclasses
import logging
import re
import threading
from dataclasses import dataclass
from typing import Any

from mcp_edgar.sec_client import SecClient

logger = logging.getLogger(__name__)

MAX_TICKER_LENGTH = 10
TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,9}$")
CIK_DIGITS = 10
REQUIRED_FIELDS = ("cik", "name", "ticker", "exchange")


class TickerError(Exception):
    """Base for ticker outcomes; ``ticker`` is the caller's input, stripped but not upper-cased."""

    def __init__(self, message: str, *, ticker: str) -> None:
        super().__init__(message)
        self.ticker = ticker


class InvalidTicker(TickerError, ValueError):
    """The input is empty, too long, or contains characters no SEC ticker uses."""


class UnknownTicker(TickerError, LookupError):
    """No issuer is listed under the normalised ticker."""

    def __init__(self, ticker: str) -> None:
        super().__init__(f"no SEC issuer is listed under ticker {ticker!r}", ticker=ticker)


class AmbiguousTicker(TickerError, LookupError):
    """Several distinct CIKs are listed under one ticker; ``candidates`` are in SEC file order."""

    def __init__(self, ticker: str, *, candidates: tuple[Issuer, ...]) -> None:
        super().__init__(
            f"ticker {ticker!r} is listed for {len(candidates)} issuers; choose one", ticker=ticker
        )
        self.candidates = candidates


class InvalidCik(TickerError, ValueError):
    """``cik`` is malformed, or not one of the CIKs the SEC lists under the ticker."""

    def __init__(self, message: str, *, cik: str, ticker: str) -> None:
        super().__init__(message, ticker=ticker)
        self.cik = cik


class TickerMapError(ValueError):
    """The ticker map payload is not the SEC shape this module expects."""


def normalize_ticker(raw: str) -> str:
    """Strip and upper-case ``raw``; raise ``InvalidTicker`` unless it looks like an SEC ticker."""
    stripped = raw.strip()
    if not stripped:
        raise InvalidTicker("ticker is empty", ticker=stripped)
    ticker = stripped.upper()
    if len(ticker) > MAX_TICKER_LENGTH:
        raise InvalidTicker(
            f"ticker {stripped!r} is longer than {MAX_TICKER_LENGTH} characters", ticker=stripped
        )
    if not TICKER_RE.match(ticker):
        bad = "".join(
            sorted({c for c in ticker if not (c.isascii() and (c.isalnum() or c in ".-"))})
        )
        detail = (
            f"contains unsupported characters {bad!r}"
            if bad
            else "must start with a letter or digit"
        )
        raise InvalidTicker(f"ticker {stripped!r} {detail}", ticker=stripped)
    return ticker


def format_cik(cik: int | str) -> str:
    """Zero-pad a CIK (int or digit string, 1–10 digits) to the 10-digit ``data.sec.gov`` form."""
    if isinstance(cik, bool) or not isinstance(cik, int | str):
        raise ValueError(f"CIK must be an int or a digit string, got {cik!r}")
    text = str(cik).strip()
    if not (text.isascii() and text.isdigit() and 1 <= len(text) <= CIK_DIGITS):
        raise ValueError(f"CIK must be 1-{CIK_DIGITS} digits, got {cik!r}")
    return text.zfill(CIK_DIGITS)


def _wanted_cik(cik: object, ticker: str) -> str | None:
    """``None``/blank → no selection; else the 10-digit form, or ``InvalidCik`` (no I/O)."""
    if cik is None or (isinstance(cik, str) and not cik.strip()):
        return None
    try:
        return format_cik(cik)  # type: ignore[arg-type]
    except ValueError:
        raise InvalidCik(
            f"cik must be 1-10 digits, got {cik!r}", cik=str(cik), ticker=ticker
        ) from None


@dataclass(frozen=True, slots=True)
class Issuer:
    """One SEC registrant as listed in the ticker map."""

    cik: str
    name: str
    ticker: str
    exchange: str | None

    def as_dict(self) -> dict[str, str | None]:
        """Candidate shape for the tool layer: ``{"cik", "name", "ticker", "exchange"}``."""
        return {
            "cik": self.cik,
            "name": self.name,
            "ticker": self.ticker,
            "exchange": self.exchange,
        }


class TickerMap:
    """Case-insensitive ticker → issuers index built from the SEC ticker map payload.

    ``by_cik`` is the reverse index (10-digit CIK → every issuer row listed under it, SEC file
    order, one per ticker) that the ``edgar://`` resources use (req_006 v6.01).
    """

    def __init__(
        self,
        index: dict[str, tuple[Issuer, ...]],
        by_cik: dict[str, tuple[Issuer, ...]] | None = None,
    ) -> None:
        self._index = index
        self._by_cik = by_cik or {}
        self._count = sum(len(issuers) for issuers in index.values())

    @classmethod
    def from_payload(cls, payload: Any) -> TickerMap:
        """Build the index; column positions come from ``fields``, never from fixed offsets."""
        if not isinstance(payload, dict):
            raise TickerMapError("ticker map payload is not a JSON object")
        fields, data = payload.get("fields"), payload.get("data")
        if not isinstance(fields, list) or not isinstance(data, list):
            raise TickerMapError("ticker map payload lacks the 'fields' and 'data' lists")
        missing = [name for name in REQUIRED_FIELDS if name not in fields]
        if missing:
            raise TickerMapError(f"ticker map 'fields' is missing {missing}")
        pos = {name: fields.index(name) for name in REQUIRED_FIELDS}
        width = max(pos.values()) + 1
        grouped: dict[str, dict[str, Issuer]] = {}
        order: list[tuple[str, str]] = []  # (cik, ticker) in order of first appearance
        for i, row in enumerate(data):
            if not isinstance(row, list) or len(row) < width:
                raise TickerMapError(f"ticker map row {i} is malformed: {row!r}")
            try:
                cik = format_cik(row[pos["cik"]])
            except ValueError as exc:
                raise TickerMapError(
                    f"ticker map row {i} has an invalid CIK {row[pos['cik']]!r}"
                ) from exc
            ticker = str(row[pos["ticker"]]).strip().upper()
            if not ticker:
                raise TickerMapError(f"ticker map row {i} has an empty ticker")
            raw_exchange = row[pos["exchange"]]
            exchange = None if raw_exchange in (None, "") else str(raw_exchange)
            by_cik = grouped.setdefault(ticker, {})
            existing = by_cik.get(cik)
            if existing is None:
                by_cik[cik] = Issuer(cik, str(row[pos["name"]]), ticker, exchange)
                order.append((cik, ticker))
            elif existing.exchange is None and exchange is not None:
                by_cik[cik] = dataclasses.replace(existing, exchange=exchange)
        reverse: dict[str, list[Issuer]] = {}
        for cik, ticker in order:
            reverse.setdefault(cik, []).append(grouped[ticker][cik])
        return cls(
            {ticker: tuple(by_cik.values()) for ticker, by_cik in grouped.items()},
            {cik: tuple(issuers) for cik, issuers in reverse.items()},
        )

    def issuers_for_cik(self, cik: str) -> tuple[Issuer, ...]:
        """Every issuer row listed under a 10-digit CIK (SEC file order); ``()`` when unlisted."""
        return self._by_cik.get(cik, ())

    def lookup(self, ticker: str) -> tuple[Issuer, ...]:
        """Issuers under an already-normalised ticker; tries the ``.``/``-`` alias on a miss."""
        found = self._index.get(ticker)
        if found:
            return found
        if "." in ticker:
            alt = ticker.replace(".", "-")
        elif "-" in ticker:
            alt = ticker.replace("-", ".")
        else:
            return ()
        return self._index.get(alt, ())

    def __len__(self) -> int:
        return self._count


class TickerResolver:
    """Resolve raw tickers to issuers; rebuilds its index only when the cached payload changes."""

    def __init__(self, client: SecClient) -> None:
        self._client = client
        self._lock = threading.Lock()
        self._payload: Any = None
        self._map: TickerMap | None = None

    def resolve(self, raw_ticker: str, *, cik: str | int | None = None) -> Issuer:
        """One issuer, or InvalidTicker / UnknownTicker / AmbiguousTicker / InvalidCik.

        ``cik`` selects one issuer when the ticker is listed under several CIKs; it must be a CIK
        the SEC lists under that ticker, so nothing is guessed, and an unknown ticker wins over
        any ``cik``. ``SecClientError`` / ``TickerMapError`` propagate unchanged (never "unknown").
        """
        ticker = normalize_ticker(raw_ticker)
        wanted = _wanted_cik(cik, raw_ticker.strip())  # validated before any request
        payload = self._client.fetch_ticker_map()
        issuers = self._map_for(payload).lookup(ticker)
        if not issuers:
            raise UnknownTicker(raw_ticker.strip())
        if wanted is not None:
            for issuer in issuers:
                if issuer.cik == wanted:
                    return issuer
            listed = ", ".join(issuer.cik for issuer in issuers)
            raise InvalidCik(
                f"cik {wanted} is not listed under ticker {ticker}; listed: {listed}",
                cik=str(cik),
                ticker=raw_ticker.strip(),
            )
        if len(issuers) > 1:
            logger.info("ticker %s is ambiguous: %d issuers", ticker, len(issuers))
            raise AmbiguousTicker(raw_ticker.strip(), candidates=issuers)
        return issuers[0]

    def issuers_for_cik(self, cik: str | int) -> tuple[Issuer, ...]:
        """Issuer rows the SEC lists under ``cik`` (any 1–10-digit spelling); ``()`` if none.

        ``ValueError`` (from ``format_cik``) for a malformed CIK, before any request. A CIK
        absent from the ticker map is never looked up on ``data.sec.gov`` (gotchas #3).
        """
        padded = format_cik(cik)
        payload = self._client.fetch_ticker_map()
        return self._map_for(payload).issuers_for_cik(padded)

    def _map_for(self, payload: Any) -> TickerMap:
        with self._lock:
            if self._map is None or payload is not self._payload:
                self._map = TickerMap.from_payload(payload)
                self._payload = payload
                logger.debug("ticker index built: %d issuers", len(self._map))
            return self._map
