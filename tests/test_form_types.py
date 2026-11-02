"""Tests for mcp_edgar.form_types: the bundled catalogue, search, validation, loading."""

import json
from collections.abc import Iterator
from datetime import date
from importlib import resources
from typing import Any

import pytest

from mcp_edgar import form_types
from mcp_edgar.form_types import (
    MAX_QUERY_LENGTH,
    CatalogueError,
    FormType,
    FormTypeCatalogue,
    InvalidQuery,
    get_catalogue,
    reset_catalogue,
    validate_query,
)


@pytest.fixture(autouse=True)
def _fresh_catalogue() -> Iterator[None]:
    reset_catalogue()
    yield
    reset_catalogue()


def entry(form: str, filings: int, description: str | None = "Desc") -> dict[str, Any]:
    return {
        "form": form,
        "description": description,
        "amendment": form.endswith("/A"),
        "filings": filings,
        "topics": ["Topic"] if description else [],
    }


def good_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "generated_on": "2026-11-01",
        "quarters": ["2025Q4", "2026Q1"],
        "sources": ["https://example.test/index", "https://example.test/forms"],
        "form_types": [entry("4", 100), entry("10-K", 50), entry("10-K/A", 5, None)],
    }
    payload.update(overrides)
    return payload


# --- the bundled file -----------------------------------------------------------------------


def test_bundled_file_is_valid_json_and_small() -> None:
    resource = resources.files("mcp_edgar").joinpath("data").joinpath("form_types.json")
    text = resource.read_text(encoding="utf-8")
    assert len(text.encode("utf-8")) < 150_000
    assert set(json.loads(text)) == {"generated_on", "quarters", "sources", "form_types"}
    assert text.endswith("\n")


def test_bundled_metadata() -> None:
    catalogue = get_catalogue()
    assert date.fromisoformat(catalogue.generated_on) <= date.today()
    assert len(catalogue.quarters) == 4
    assert list(catalogue.quarters) == sorted(catalogue.quarters)
    assert any("full-index" in source for source in catalogue.sources)


def test_bundled_entries_are_many_unique_and_ordered() -> None:
    entries = get_catalogue().entries
    forms = [e.form for e in entries]
    assert len(entries) >= 250
    assert len(set(forms)) == len(forms)
    assert all(f == f.strip() and f for f in forms)
    assert [e.form for e in sorted(entries, key=lambda e: (-e.filings, e.form))] == forms
    assert all(e.amendment == e.form.endswith("/A") for e in entries)
    assert all(e.filings >= 1 for e in entries)
    assert all(e.description is None or e.description for e in entries)


@pytest.mark.parametrize("form", ["4", "10-K", "10-K/A", "8-K", "10-Q", "8-K/A"])
def test_common_codes_are_present(form: str) -> None:
    assert form in {e.form for e in get_catalogue().entries}


def test_schedule_13g_is_present_under_one_spelling() -> None:
    forms = {e.form for e in get_catalogue().entries}
    assert forms & {"SCHEDULE 13G", "SC 13G"}


def test_common_codes_carry_descriptions_and_topics() -> None:
    by_form = {e.form: e for e in get_catalogue().entries}
    described = [f for f in ("10-K", "10-Q", "8-K", "4") if by_form[f].description]
    assert described == ["10-K", "10-Q", "8-K", "4"]
    assert by_form["10-K"].topics
    assert sum(1 for e in get_catalogue().entries if e.description) >= 20


# --- search ---------------------------------------------------------------------------------


def test_search_none_returns_everything_in_order() -> None:
    catalogue = get_catalogue()
    assert catalogue.search(None) == list(catalogue.entries)


def test_search_is_a_case_insensitive_substring_match() -> None:
    results = get_catalogue().search("10-k")
    forms = {e.form for e in results}
    assert {"10-K", "10-K/A"} <= forms
    assert all(
        "10-k" in e.form.lower() or (e.description or "").lower().find("10-k") >= 0 for e in results
    )


def test_search_slash_a_covers_every_amendment() -> None:
    catalogue = get_catalogue()
    results = catalogue.search("/A")
    amendments = {e.form for e in catalogue.entries if e.amendment}
    assert amendments <= {e.form for e in results}
    # a code may contain "/A" without ending in it (e.g. "C/A-W"); it matches but is no amendment
    assert all("/a" in e.form.lower() or "/a" in (e.description or "").lower() for e in results)
    assert catalogue.search("zzzqqq") == []


def test_search_matches_descriptions_too() -> None:
    catalogue = FormTypeCatalogue.from_payload(
        good_payload(form_types=[entry("X-1", 2, "Annual report"), entry("Y-2", 1, "Proxy")])
    )
    assert [e.form for e in catalogue.search("annual")] == ["X-1"]


# --- validate_query -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("query", "expected"),
    [(None, None), ("", None), ("   ", None), (" 13G ", "13G"), ("10-k", "10-k")],
)
def test_validate_query_normalises(query: str | None, expected: str | None) -> None:
    assert validate_query(query) == expected


@pytest.mark.parametrize("query", ["x" * (MAX_QUERY_LENGTH + 1), 7, ["13G"]])
def test_validate_query_rejects(query: Any) -> None:
    with pytest.raises(InvalidQuery, match="^query"):
        validate_query(query)


# --- loading --------------------------------------------------------------------------------


def test_get_catalogue_is_cached_and_resettable() -> None:
    first = get_catalogue()
    assert first is get_catalogue()
    reset_catalogue()
    assert get_catalogue() is not first


def test_from_payload_accepts_a_good_payload() -> None:
    catalogue = FormTypeCatalogue.from_payload(good_payload())
    assert catalogue.generated_on == "2026-11-01"
    assert catalogue.quarters == ("2025Q4", "2026Q1")
    assert catalogue.entries[0] == FormType("4", "Desc", False, 100, ("Topic",))
    assert catalogue.metadata() == {
        "generated_on": "2026-11-01",
        "quarters": ["2025Q4", "2026Q1"],
        "sources": ["https://example.test/index", "https://example.test/forms"],
    }
    assert catalogue.entries[2].as_dict() == {
        "form": "10-K/A",
        "description": None,
        "amendment": True,
        "filings": 5,
        "topics": [],
    }


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        ([], "not a JSON object"),
        (good_payload(generated_on="yesterday"), "generated_on"),
        (good_payload(quarters=[]), "quarters"),
        (good_payload(quarters=["2025-Q4"]), "quarters"),
        (good_payload(sources="x"), "sources"),
        (good_payload(form_types=[]), "non-empty"),
        (good_payload(form_types=[entry("4", 1), entry("4", 1)]), "duplicate"),
        (good_payload(form_types=[entry("10-K", 1), entry("4", 5)]), "sorted"),
        (good_payload(form_types=[{**entry("4", 1), "amendment": True}]), "amendment"),
        (good_payload(form_types=[entry("4", 0)]), "filings"),
        (good_payload(form_types=[entry(" 4", 1)]), "invalid form"),
        (good_payload(form_types=[{**entry("4", 1), "topics": [""]}]), "topics"),
        (good_payload(form_types=[{**entry("4", 1), "description": ""}]), "description"),
    ],
)
def test_malformed_payloads_raise_catalogue_error(payload: Any, match: str) -> None:
    with pytest.raises(CatalogueError, match=match):
        FormTypeCatalogue.from_payload(payload)


def test_missing_file_is_a_catalogue_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class NoFiles:
        @staticmethod
        def files(package: str) -> Any:
            raise FileNotFoundError(package)

    monkeypatch.setattr(form_types, "resources", NoFiles)
    with pytest.raises(CatalogueError, match="missing"):
        FormTypeCatalogue.load()
