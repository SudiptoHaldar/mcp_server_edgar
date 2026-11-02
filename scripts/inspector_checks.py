"""Rows and checks of the Inspector checklist (data side; the runner is ``inspector_checklist.py``).

Every ``Row`` names an Inspector CLI method (``tools/list``, ``tools/call``, ``resources/list``,
``resources/templates/list``, ``resources/read``, ``prompts/list``, ``prompts/get``), an
expectation, and a pure ``check(result, state)`` that judges the CLI's JSON result and renders
what it observed. ``state`` carries values between rows (the tool's latest filing, the first
catalogue code). Split out of the runner in req_006 v6.02 to stay under the 500-line guideline.
No ``from __future__ import annotations`` here: the ``Row`` dataclass must survive being loaded by
file path (gotchas #18).
"""

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

from mcp_edgar.resources import FORM_TYPES, TEMPLATE_URIS
from mcp_edgar.settings import Settings

EXPECTED_TOOLS = ["get_latest_filing", "list_filings_by_year", "list_form_types"]
EXPECTED_TITLES = ["Latest SEC filing", "Filings by year", "EDGAR form types"]
FILING_TOOLS = EXPECTED_TOOLS[:2]  # the tools that take a ticker (and therefore cik)
EXPECTED_RESOURCES = [FORM_TYPES]
EXPECTED_TEMPLATES = list(TEMPLATE_URIS)
EXPECTED_PROMPTS = ["review_latest_filing", "review_filings_by_year", "explain_form_types"]
EXPECTED_PROMPT_ARGS = [[("ticker", True)], [("ticker", True), ("year", True)], [("query", False)]]
AAPL_CIK = "0000320193"
Result = dict[str, Any]
State = dict[str, Any]
Check = Callable[[Result, State], tuple[bool, str]]


@dataclass(frozen=True)
class Row:
    number: int
    tool: str | None  # None with method tools/list runs tools/list
    args: dict[str, Any]
    expectation: str
    check: Check
    method: str = "tools/call"  # or a listing method, or resources/read (see `listing`, `read`)
    uri: str | None = None  # for resources/read
    prompt_args: dict[str, str] | None = None  # for prompts/get (tool carries the prompt name)

    def describe(self) -> str:
        if self.method == "tools/call" and self.tool is not None:
            rendered = " ".join(f"{k}={render(v)}" for k, v in self.args.items())
            return f"{self.tool} {rendered}".strip()
        if self.method == "resources/read":
            return f"resources/read {self.uri}"
        if self.method == "prompts/get":
            rendered = " ".join(f"{k}={v}" for k, v in (self.prompt_args or {}).items())
            return f"prompts/get {self.tool} {rendered}".strip()
        return self.method


def render(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value)


def payload(result: Result) -> dict[str, Any]:
    content = result.get("structuredContent")
    return content if isinstance(content, dict) else {}


def resource_payload(result: Result) -> dict[str, Any]:
    """The JSON object inside the first ``contents[]`` entry of a ``resources/read`` result."""
    contents = result.get("contents") or []
    text = contents[0].get("text") if contents and isinstance(contents[0], dict) else None
    try:
        parsed = json.loads(text) if text else None
    except ValueError:
        parsed = None
    return parsed if isinstance(parsed, dict) else {}


def prompt_payload(result: Result) -> str:
    """The text of the first ``user`` text message of a ``prompts/get`` result, else ``""``."""
    messages = result.get("messages") or []
    first = messages[0] if messages and isinstance(messages[0], dict) else {}
    content = first.get("content") or {}
    if first.get("role") == "user" and isinstance(content, dict) and content.get("type") == "text":
        return str(content.get("text", ""))
    return ""


def head_ok(url: str | None, state: State) -> bool:
    if not url:
        return False
    user_agent = state.setdefault("user_agent", Settings.from_env({}).user_agent)
    try:
        response = httpx.head(
            url, headers={"User-Agent": user_agent}, follow_redirects=True, timeout=30
        )
    except httpx.HTTPError:
        return False
    return response.status_code < 400


def dedented(description: str) -> bool:
    lines = description.splitlines()
    return len(lines) > 2 and not lines[0].startswith(" ") and not lines[2].startswith(" ")


# --- checks ---------------------------------------------------------------------------------


def check_tools_list(result: Result, state: State) -> tuple[bool, str]:
    tools = result.get("tools", [])
    names = [t.get("name") for t in tools]
    titles = [t.get("title") for t in tools]
    read_only = all((t.get("annotations") or {}).get("readOnlyHint") is True for t in tools)
    clean = all(dedented(t.get("description") or "") for t in tools)
    cik_last = all(
        list((t.get("inputSchema") or {}).get("properties", {}))[-1:] == ["cik"]
        for t in tools
        if t.get("name") in FILING_TOOLS
    )
    passed = names == EXPECTED_TOOLS and titles == EXPECTED_TITLES and read_only and clean
    observed = f"tools={names}; readOnly={read_only}; dedented={clean}; cik last={cik_last}"
    return passed and cik_last, observed


def check_latest(result: Result, state: State) -> tuple[bool, str]:
    data = payload(result)
    filing = data.get("filing") or {}
    state["latest_filing"] = filing  # compared with the resource reads (rows 20-21)
    head = head_ok(filing.get("filing_url"), state)
    passed = not result.get("isError") and data.get("status") == "ok" and head
    observed = f"status={data.get('status')}; {filing.get('form')} {filing.get('filing_date')}"
    return passed, f"{observed}; HEAD<400={head}"


def check_latest_ten_k(result: Result, state: State) -> tuple[bool, str]:
    data = payload(result)
    filing = data.get("filing") or {}
    primary = filing.get("primary_document_url")
    head = head_ok(primary, state) if primary else True
    passed = data.get("status") == "ok" and filing.get("form") == "10-K" and head
    return passed, f"{filing.get('form')} {filing.get('filing_date')}; primary HEAD<400={head}"


def check_status(status: str, field: str | None = None, contains: str | None = None) -> Check:
    def check(result: Result, state: State) -> tuple[bool, str]:
        data = payload(result)
        passed = data.get("status") == status
        if field is not None:
            passed = passed and data.get("field") == field
        if contains is not None:
            passed = passed and contains in str(data.get("message"))
        return passed, f"status={data.get('status')}; field={data.get('field')}"

    return check


def check_cik_match(result: Result, state: State) -> tuple[bool, str]:
    data = payload(result)
    cik = (data.get("company") or {}).get("cik")
    passed = data.get("status") == "ok" and cik == "0000320193"
    return passed, f"status={data.get('status')}; cik={cik}"


def counts(data: dict[str, Any]) -> str:
    return (
        f"total={data.get('total_count')}; returned={data.get('returned_count')}; "
        f"next_offset={data.get('next_offset')}"
    )


def check_one_page(result: Result, state: State) -> tuple[bool, str]:
    data = payload(result)
    passed = (
        data.get("status") == "ok"
        and data.get("returned_count") == data.get("total_count")
        and data.get("next_offset") is None
    )
    return passed, counts(data)


def check_ten_k_2025(result: Result, state: State) -> tuple[bool, str]:
    data = payload(result)
    filings = data.get("filings") or []
    report = filings[0].get("report_date") if filings else None
    passed = data.get("status") == "ok" and data.get("total_count") == 1
    passed = passed and str(report).endswith("-09-27")
    return passed, f"{counts(data)}; report_date={report}"


def check_first_page_2005(result: Result, state: State) -> tuple[bool, str]:
    data = payload(result)
    filings = data.get("filings") or []
    state["page_one"] = {f.get("accession_number") for f in filings}
    state["total_2005"] = data.get("total_count")
    head = head_ok(filings[0].get("filing_url") if filings else None, state)
    passed = (
        data.get("status") == "ok"
        and (data.get("total_count") or 0) > 100
        and data.get("returned_count") == 100
        and data.get("next_offset") == 100
        and head
    )
    return passed, f"{counts(data)}; HEAD<400={head}"


def check_second_page_2005(result: Result, state: State) -> tuple[bool, str]:
    data = payload(result)
    page_two = {f.get("accession_number") for f in data.get("filings") or []}
    disjoint = page_two.isdisjoint(state.get("page_one", set()))
    expected = (state.get("total_2005") or 0) - 100
    passed = (
        data.get("status") == "ok"
        and data.get("offset") == 100
        and data.get("next_offset") is None
        and data.get("returned_count") == expected
        and disjoint
    )
    return passed, f"{counts(data)}; disjoint={disjoint}"


def check_empty_year(result: Result, state: State) -> tuple[bool, str]:
    data = payload(result)
    passed = data.get("status") == "ok" and data.get("total_count") == 0
    return passed and data.get("filings") == [], counts(data)


def check_form_types_default(result: Result, state: State) -> tuple[bool, str]:
    data = payload(result)
    entries = data.get("form_types") or []
    first = entries[0] if entries else {}
    ordered = all(
        entries[i].get("filings", 0) >= entries[i + 1].get("filings", 0)
        for i in range(len(entries) - 1)
    )
    generated_on = (data.get("catalogue") or {}).get("generated_on")
    state["first_form"] = first.get("form")  # compared with the catalogue resource (row 22)
    passed = (
        data.get("status") == "ok"
        and (data.get("total_count") or 0) >= 250
        and data.get("returned_count") == 100
        and data.get("next_offset") == 100
        and ordered
    )
    observed = f"{counts(data)}; first={first.get('form')} ({first.get('filings')} filings)"
    return passed, f"{observed}; ordered={ordered}; generated_on={generated_on}"


def check_form_types_query(result: Result, state: State) -> tuple[bool, str]:
    data = payload(result)
    forms = [entry.get("form") for entry in data.get("form_types") or []]
    passed = data.get("status") == "ok" and "10-K" in forms and "10-K/A" in forms
    return passed, f"total={data.get('total_count')}; forms={forms[:6]}"


def check_is_error_mentions_year(result: Result, state: State) -> tuple[bool, str]:
    text = " ".join(str(block.get("text", "")) for block in result.get("content") or [])
    passed = result.get("isError") is True and "year" in text
    return passed, f"isError={result.get('isError')}; mentions year={'year' in text}"


# --- resource checks (req_006 v6.01) ------------------------------------------------------


def check_resources_list(result: Result, state: State) -> tuple[bool, str]:
    items = result.get("resources") or []
    uris = [item.get("uri") for item in items]
    mimes = [item.get("mimeType") for item in items]
    titles = [item.get("title") for item in items]
    passed = (
        uris == EXPECTED_RESOURCES
        and mimes == ["application/json"]
        and titles == ["EDGAR form-type catalogue"]
    )
    return passed, f"uris={uris}; mimeType={mimes}; titles={titles}"


def check_templates_list(result: Result, state: State) -> tuple[bool, str]:
    items = result.get("resourceTemplates") or []
    uris = [item.get("uriTemplate") for item in items]
    json_only = all(item.get("mimeType") == "application/json" for item in items)
    titled = all(item.get("title") for item in items)
    passed = uris == EXPECTED_TEMPLATES and json_only and titled
    return passed, f"templates={uris}; json={json_only}; titled={titled}"


def check_profile_read(result: Result, state: State) -> tuple[bool, str]:
    data = resource_payload(result)
    keys = list(data)
    tickers = data.get("tickers") or []
    passed = (
        keys == ["cik", "name", "ticker", "exchange", "tickers"]
        and data.get("cik") == AAPL_CIK
        and data.get("ticker") == "AAPL"
        and "AAPL" in tickers
    )
    return passed, f"keys={keys}; cik={data.get('cik')}; tickers={tickers}"


def check_latest_read(result: Result, state: State) -> tuple[bool, str]:
    data = resource_payload(result)
    filing = data.get("filing") or {}
    accession = filing.get("accession_number")
    state["resource_accession"] = accession  # compared with the CIK route (row 21)
    same_as_tool = accession is not None and accession == (state.get("latest_filing") or {}).get(
        "accession_number"
    )
    head = head_ok(filing.get("filing_url"), state)
    passed = (
        list(data) == ["company", "filing"]
        and (data.get("company") or {}).get("cik") == AAPL_CIK
        and same_as_tool
        and head
    )
    observed = f"{filing.get('form')} {filing.get('filing_date')}; same as row 2={same_as_tool}"
    return passed, f"{observed}; HEAD<400={head}"


def check_issuer_latest_read(result: Result, state: State) -> tuple[bool, str]:
    data = resource_payload(result)
    accession = (data.get("filing") or {}).get("accession_number")
    same = accession is not None and accession == state.get("resource_accession")
    passed = (data.get("company") or {}).get("cik") == AAPL_CIK and same
    return passed, f"cik={(data.get('company') or {}).get('cik')}; same as row 20={same}"


def check_form_types_read(result: Result, state: State) -> tuple[bool, str]:
    data = resource_payload(result)
    entries = data.get("form_types") or []
    first = entries[0].get("form") if entries else None
    generated_on = (data.get("catalogue") or {}).get("generated_on")
    same_first = first is not None and first == state.get("first_form")
    passed = list(data) == ["catalogue", "form_types"] and len(entries) >= 250
    passed = passed and bool(generated_on) and same_first
    return passed, f"entries={len(entries)}; first={first}; same as row 15={same_first}"


def check_resource_error(result: Result, state: State) -> tuple[bool, str]:
    error = result.get("error")
    message = str(error.get("message") if isinstance(error, dict) else error)
    passed = result.get("isError") is True and "ZZZZZZ" in message
    return passed, f"isError={result.get('isError')}; message={message[:80]!r}"


def listing(number: int, method: str, expectation: str, check: Check) -> Row:
    """A row for a listing method (`tools/list`, `resources/list`, `resources/templates/list`)."""
    return Row(number, None, {}, expectation, check, method=method)


def read(number: int, uri: str, expectation: str, check: Check) -> Row:
    """A `resources/read` row."""
    return Row(number, None, {}, expectation, check, method="resources/read", uri=uri)


def prompt(number: int, name: str, args: dict[str, str], expectation: str, check: Check) -> Row:
    """A `prompts/get` row; `args` are the string arguments."""
    return Row(number, name, {}, expectation, check, method="prompts/get", prompt_args=args)


# --- prompt checks (req_006 v6.02) --------------------------------------------------------


def check_prompts_list(result: Result, state: State) -> tuple[bool, str]:
    items = result.get("prompts") or []
    names = [item.get("name") for item in items]
    args = [
        [(a.get("name"), a.get("required")) for a in item.get("arguments") or []] for item in items
    ]
    titled = all(item.get("title") for item in items)
    one_line = all("\n" not in str(item.get("description") or "x") for item in items)
    described = all(a.get("description") for item in items for a in item.get("arguments") or [])
    passed = names == EXPECTED_PROMPTS and args == EXPECTED_PROMPT_ARGS and titled and one_line
    observed = f"prompts={names}; args={args}; titled={titled}; one-line={one_line}"
    return passed and described, f"{observed}; described={described}"


def check_prompt(tokens: tuple[str, ...], forbidden: str | None = None) -> Check:
    """One `user` text message containing every token and not containing `forbidden`."""

    def check(result: Result, state: State) -> tuple[bool, str]:
        text = prompt_payload(result)
        found = [token for token in tokens if token in text]
        clean = forbidden is None or forbidden not in text
        messages = len(result.get("messages") or [])
        passed = messages == 1 and len(found) == len(tokens) and clean
        return passed, f"messages={messages}; tokens={len(found)}/{len(tokens)}; clean={clean}"

    return check


GUARD = "Do not give investment advice"
LATEST_PROMPT = ("get_latest_filing", '"AAPL"', "filing_date", "report_date", "ambiguous", GUARD)
YEAR_PROMPT = ("list_filings_by_year", '"AAPL"', "2025", "next_offset", "report_date", GUARD)
FORMS_PROMPT = ("list_form_types", "/A", "catalogue.generated_on")
check_prompt_latest = check_prompt(LATEST_PROMPT)
check_prompt_year = check_prompt(YEAR_PROMPT)
check_prompt_form_types = check_prompt(FORMS_PROMPT, forbidden='query "')


AAPL = {"ticker": "AAPL"}
AAPL_2025 = {"ticker": "AAPL", "year": "2025"}
LATEST, BY_YEAR, FORMS = EXPECTED_TOOLS
INVALID_CIK = check_status("invalid_input", "cik", "0000320193")
ROWS: list[Row] = [
    listing(1, "tools/list", "three read-only tools, dedented, cik last", check_tools_list),
    Row(2, LATEST, AAPL, "ok; filing_url opens (HEAD < 400)", check_latest),
    Row(3, LATEST, {**AAPL, "forms": ["10-K"]}, "ok; form 10-K", check_latest_ten_k),
    Row(4, LATEST, {"ticker": "ZZZZZZ"}, "not_found", check_status("not_found")),
    Row(5, LATEST, {**AAPL, "cik": 320193}, "ok; cik 0000320193", check_cik_match),
    Row(
        6, LATEST, {**AAPL, "cik": "0000000001"}, "invalid_input cik naming 0000320193", INVALID_CIK
    ),
    Row(7, BY_YEAR, {**AAPL, "year": 2025}, "ok; one page", check_one_page),
    Row(
        8,
        BY_YEAR,
        {**AAPL, "year": 2025, "forms": ["10-K"]},
        "ok; total 1; FY end -09-27",
        check_ten_k_2025,
    ),
    Row(
        9,
        BY_YEAR,
        {**AAPL, "year": 2005},
        "ok; > 100; page 100; next 100; link opens",
        check_first_page_2005,
    ),
    Row(
        10,
        BY_YEAR,
        {**AAPL, "year": 2005, "offset": 100},
        "ok; offset 100; last page; disjoint",
        check_second_page_2005,
    ),
    Row(
        11,
        BY_YEAR,
        {**AAPL, "year": 1850},
        "invalid_input year",
        check_status("invalid_input", "year"),
    ),
    Row(12, BY_YEAR, {**AAPL, "year": 2030}, "ok; total 0", check_empty_year),
    Row(
        13,
        BY_YEAR,
        {**AAPL, "year": 2025, "limit": 0},
        "invalid_input limit",
        check_status("invalid_input", "limit"),
    ),
    Row(
        14, BY_YEAR, AAPL, "isError (SDK validation) mentioning year", check_is_error_mentions_year
    ),
    Row(15, FORMS, {}, "ok; >= 250 codes; page 100; ordered by filings", check_form_types_default),
    Row(16, FORMS, {"query": "10-K"}, "ok; includes 10-K and 10-K/A", check_form_types_query),
    listing(17, "resources/list", "one static resource: form-types, JSON", check_resources_list),
    listing(18, "resources/templates/list", "four JSON templates in order", check_templates_list),
    read(
        19,
        "edgar://companies/AAPL/profile",
        "profile keys in order; cik 0000320193; tickers include AAPL",
        check_profile_read,
    ),
    read(
        20,
        "edgar://companies/AAPL/latest",
        "{company, filing}; same accession as row 2; filing_url opens",
        check_latest_read,
    ),
    read(21, "edgar://issuers/320193/latest", "same accession as row 20", check_issuer_latest_read),
    read(22, FORM_TYPES, ">= 250 codes; generated_on; first form = row 15", check_form_types_read),
    read(
        23,
        "edgar://companies/ZZZZZZ/profile",
        "resource error (CLI exit 1) mentioning ZZZZZZ",
        check_resource_error,
    ),
    listing(24, "prompts/list", "three prompts, titles, required flags", check_prompts_list),
    prompt(
        25,
        "review_latest_filing",
        AAPL,
        "user text: get_latest_filing, AAPL, guardrails",
        check_prompt_latest,
    ),
    prompt(26, "explain_form_types", {}, "renders; no query clause", check_prompt_form_types),
    prompt(27, "review_filings_by_year", AAPL_2025, "tokens + guardrails", check_prompt_year),
]
