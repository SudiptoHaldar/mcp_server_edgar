"""Shared fixtures: logging/settings isolation and network-free SEC client helpers."""

import json
import logging
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest

from mcp_edgar.sec_client import SecClient
from mcp_edgar.settings import Settings, reset_settings_cache

FIXTURES = Path(__file__).parent / "fixtures"

Routes = Mapping[str, Sequence[httpx.Response | Exception]]


@pytest.fixture(autouse=True)
def _isolate_logging_and_settings() -> Iterator[None]:
    """Reset root logging handlers and the cached settings after every test.

    `configure_logging` binds a handler to whatever `sys.stderr` is at call time; inside a
    test that is the capsys buffer, which is closed at teardown. Removing the handler here
    prevents "I/O operation on closed file" noise in later tests.
    """
    yield
    reset_settings_cache()
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    root.addHandler(logging.NullHandler())
    root.setLevel(logging.WARNING)


def fixture_json(name: str) -> Any:
    """Parsed JSON of ``tests/fixtures/<name>``."""
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FakeClock:
    """Deterministic monotonic clock; ``sleep`` advances it and records every call."""

    def __init__(self) -> None:
        self.now = 1_000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@dataclass
class Recorded:
    """Every request the mock transport saw, with the fake-clock time it arrived."""

    requests: list[httpx.Request] = field(default_factory=list)
    times: list[float] = field(default_factory=list)


def make_sec_transport(
    routes: Routes, clock: FakeClock | None = None
) -> tuple[httpx.MockTransport, Recorded]:
    """Mock transport: ``routes`` maps a URL to ordered responses (or exceptions to raise).

    The last item repeats for further calls; an unknown URL answers 404. Responses are copied
    per call so one template may serve many requests.
    """
    recorded = Recorded()
    counters: dict[str, int] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        recorded.requests.append(request)
        if clock is not None:
            recorded.times.append(clock())
        url = str(request.url)
        items = routes.get(url)
        if items is None:
            return httpx.Response(404)
        index = counters.get(url, 0)
        counters[url] = index + 1
        item = items[min(index, len(items) - 1)]
        if isinstance(item, Exception):
            raise item
        return httpx.Response(item.status_code, headers=item.headers, content=item.content)

    return httpx.MockTransport(handler), recorded


ClientFactory = Callable[..., tuple[SecClient, Recorded, FakeClock]]


@pytest.fixture
def make_client() -> Iterator[ClientFactory]:
    """Factory: ``make_client(routes, env={...})`` -> (SecClient, Recorded, FakeClock)."""
    created: list[SecClient] = []

    def factory(
        routes: Routes, *, env: Mapping[str, str] | None = None
    ) -> tuple[SecClient, Recorded, FakeClock]:
        settings = Settings.from_env(dict(env or {}))
        clock = FakeClock()
        transport, recorded = make_sec_transport(routes, clock=clock)
        client = SecClient(settings, transport=transport, clock=clock, sleep=clock.sleep)
        created.append(client)
        return client, recorded, clock

    yield factory
    for client in created:
        client.close()
