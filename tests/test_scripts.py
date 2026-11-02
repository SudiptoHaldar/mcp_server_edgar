"""Smoke checks for the dev scripts: the Inspector checklist rows and the catalogue refresher.

Neither script is run here (they shell out to npx or fetch ~20 MB from the SEC); their pure
parts are exercised on inline fixtures.
"""

import importlib.util
import json
import sys
from collections import Counter
from datetime import date
from pathlib import Path
from types import ModuleType

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def load_module(name: str = "inspector_checks") -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look the module up by name (gotchas #18)
    spec.loader.exec_module(module)
    return module


def load_runner() -> ModuleType:
    """The checks module must be in ``sys.modules`` before the runner's bare import runs."""
    load_module("inspector_checks")
    return load_module("inspector_checklist")


def test_checklist_has_twenty_six_distinct_rows() -> None:
    module = load_module()
    assert [row.number for row in module.ROWS] == list(range(1, 28))
    assert all(callable(row.check) for row in module.ROWS)
    methods = Counter(row.method for row in module.ROWS)
    assert methods == Counter(
        {
            "tools/list": 1,
            "tools/call": 15,
            "resources/list": 1,
            "resources/templates/list": 1,
            "resources/read": 5,
            "prompts/list": 1,
            "prompts/get": 3,
        }
    )
    assert all(row.tool is not None for row in module.ROWS if row.method == "tools/call")
    assert all(row.uri for row in module.ROWS if row.method == "resources/read")
    assert all(
        row.tool and row.prompt_args is not None
        for row in module.ROWS
        if row.method == "prompts/get"
    )


def test_runner_imports_the_rows_from_the_checks_module() -> None:
    runner = load_runner()
    checks = sys.modules["inspector_checks"]
    assert runner.ROWS is checks.ROWS
    assert callable(runner.run_inspector) and callable(runner.main) and callable(runner.cli_error)
    assert not hasattr(runner, "check_tools_list")  # checks live in one place only
    prompt_row = next(r for r in checks.ROWS if r.method == "prompts/get" and r.prompt_args)
    cmd = runner.command("npx", prompt_row)
    assert cmd[-4:] == ["--prompt-name", "review_latest_filing", "--prompt-args", "ticker=AAPL"]
    read_row = next(r for r in checks.ROWS if r.method == "resources/read")
    expected = ["--method", "resources/read", "--uri", read_row.uri]
    assert runner.command("npx", read_row)[-4:] == expected


def test_rows_cover_all_tools_resources_prompts_and_the_sdk_error_path() -> None:
    module = load_module()
    tool_rows = {row.tool for row in module.ROWS if row.method == "tools/call"}
    assert tool_rows == set(module.EXPECTED_TOOLS)
    assert len(module.EXPECTED_TOOLS) == 3 and module.EXPECTED_TOOLS[-1] == "list_form_types"
    assert sum("isError" in row.expectation for row in module.ROWS) == 1
    assert module.EXPECTED_RESOURCES == ["edgar://form-types"]
    assert module.EXPECTED_TEMPLATES == [
        "edgar://companies/{ticker}/profile",
        "edgar://companies/{ticker}/latest",
        "edgar://issuers/{cik}/profile",
        "edgar://issuers/{cik}/latest",
    ]
    read_uris = [row.uri for row in module.ROWS if row.method == "resources/read"]
    assert "edgar://companies/AAPL/latest" in read_uris and "edgar://form-types" in read_uris
    assert module.EXPECTED_PROMPTS == [
        "review_latest_filing",
        "review_filings_by_year",
        "explain_form_types",
    ]
    prompt_rows = [row.tool for row in module.ROWS if row.method == "prompts/get"]
    assert prompt_rows == ["review_latest_filing", "explain_form_types", "review_filings_by_year"]


def test_row_description_renders_json_arguments_resource_uris_and_prompts() -> None:
    module = load_module()
    row = next(r for r in module.ROWS if r.args.get("forms"))
    assert 'forms=["10-K"]' in row.describe()
    assert module.ROWS[0].describe() == "tools/list"
    assert module.ROWS[16].describe() == "resources/list"
    assert module.ROWS[17].describe() == "resources/templates/list"
    assert module.ROWS[19].describe() == "resources/read edgar://companies/AAPL/latest"
    assert module.ROWS[23].describe() == "prompts/list"
    assert module.ROWS[24].describe() == "prompts/get review_latest_filing ticker=AAPL"
    assert module.ROWS[25].describe() == "prompts/get explain_form_types"
    assert module.ROWS[26].describe() == "prompts/get review_filings_by_year ticker=AAPL year=2025"


def test_prompt_checks_judge_messages() -> None:
    module = load_module()
    listing = next(r for r in module.ROWS if r.method == "prompts/list")
    prompts = [
        {
            "name": n,
            "title": "T",
            "description": "One line.",
            "arguments": [{"name": a, "required": req} for a, req in args],
        }
        for n, args in zip(module.EXPECTED_PROMPTS, module.EXPECTED_PROMPT_ARGS, strict=True)
    ]
    for item in prompts:
        for argument in item["arguments"]:
            argument["description"] = "Described."
    assert listing.check({"prompts": prompts}, {})[0] is True
    prompts[0]["arguments"][0]["description"] = ""  # v6.03: every argument must be described
    assert listing.check({"prompts": prompts}, {})[0] is False
    prompts[0]["arguments"][0]["description"] = "Described."
    prompts[2]["arguments"][0]["required"] = True
    assert listing.check({"prompts": prompts}, {})[0] is False
    year_row = next(r for r in module.ROWS if r.tool == "review_filings_by_year")
    year_text = (
        'Call list_filings_by_year with ticker "AAPL" and year 2025; next_offset; report_date. '
        "Do not give investment advice."
    )
    year_good = {"messages": [{"role": "user", "content": {"type": "text", "text": year_text}}]}
    assert year_row.check(year_good, {})[0] is True
    year_bad = {
        "messages": [
            {
                "role": "user",
                "content": {"type": "text", "text": year_text.replace("next_offset", "x")},
            }
        ]
    }
    assert year_row.check(year_bad, {})[0] is False
    latest = next(r for r in module.ROWS if r.tool == "review_latest_filing")
    text = (
        'Call get_latest_filing with ticker "AAPL"; cite filing_date and report_date; if '
        "ambiguous ... Do not give investment advice."
    )
    good = {"messages": [{"role": "user", "content": {"type": "text", "text": text}}]}
    assert latest.check(good, {})[0] is True
    bad = {
        "messages": [
            {
                "role": "user",
                "content": {"type": "text", "text": text.replace("get_latest_filing", "x")},
            }
        ]
    }
    assert latest.check(bad, {})[0] is False
    assert module.prompt_payload(good) == text
    assert (
        module.prompt_payload(
            {"messages": [{"role": "assistant", "content": {"type": "text", "text": "x"}}]}
        )
        == ""
    )
    assert module.prompt_payload({}) == ""
    form_types = next(r for r in module.ROWS if r.tool == "explain_form_types")
    ok_text = "Call list_form_types; codes ending in /A; catalogue.generated_on says."
    assert form_types.check(
        {"messages": [{"role": "user", "content": {"type": "text", "text": ok_text}}]}, {}
    )[0]
    with_query = ok_text + ' with query "13G"'
    assert not form_types.check(
        {"messages": [{"role": "user", "content": {"type": "text", "text": with_query}}]}, {}
    )[0]


def test_resource_checks_judge_contents_and_errors() -> None:
    module = load_module()
    entries = [{"form": "4", "filings": 9}] * 250
    good = {
        "contents": [
            {
                "uri": "edgar://form-types",
                "mimeType": "application/json",
                "text": json.dumps(
                    {"catalogue": {"generated_on": "2026-11-01"}, "form_types": entries}
                ),
            }
        ]
    }
    read_form_types = next(r for r in module.ROWS if r.uri == "edgar://form-types")
    assert read_form_types.check(good, {"first_form": "4"})[0] is True
    assert read_form_types.check(good, {"first_form": "10-K"})[0] is False
    short = {"contents": [{"text": json.dumps({"catalogue": {}, "form_types": entries[:10]})}]}
    assert read_form_types.check(short, {"first_form": "4"})[0] is False
    assert module.resource_payload({"contents": [{"text": "not json"}]}) == {}
    assert module.resource_payload({}) == {}
    error_row = next(r for r in module.ROWS if "ZZZZZZ" in (r.uri or ""))
    assert error_row.check({"isError": True, "error": {"message": "no issuer ZZZZZZ"}}, {})[0]
    assert not error_row.check({"contents": [{"text": "{}"}]}, {})[0]
    runner = load_runner()
    assert runner.cli_error('x\n{"error":{"code":"error","message":"Unknown resource"}}\n') == {
        "code": "error",
        "message": "Unknown resource",
    }
    assert runner.cli_error("plain stderr") is None


def test_pure_checks_judge_structured_content() -> None:
    module = load_module()
    invalid_year = next(r for r in module.ROWS if r.args.get("year") == 1850)
    good = {"structuredContent": {"status": "invalid_input", "field": "year"}}
    bad = {"structuredContent": {"status": "ok"}}
    assert invalid_year.check(good, {})[0] is True
    assert invalid_year.check(bad, {})[0] is False
    is_error = next(r for r in module.ROWS if "isError" in r.expectation)
    assert is_error.check({"isError": True, "content": [{"text": "year missing"}]}, {})[0]
    assert not is_error.check({"isError": False, "content": []}, {})[0]
    form_types = next(r for r in module.ROWS if r.tool == "list_form_types" and r.args)
    good = {
        "structuredContent": {"status": "ok", "form_types": [{"form": "10-K"}, {"form": "10-K/A"}]}
    }
    assert form_types.check(good, {})[0] is True
    assert (
        form_types.check({"structuredContent": {"status": "ok", "form_types": []}}, {})[0] is False
    )


def test_dedented_requires_the_paragraph_to_start_at_column_zero() -> None:
    module = load_module()
    assert module.dedented("Summary.\n\nDetails start here.\n    nested: fine") is True
    assert module.dedented("Summary.\n\n    Indented paragraph.") is False
    assert module.dedented("Summary only.") is False


# --- scripts/refresh_form_types.py ----------------------------------------------------------

INDEX_SNIPPET = (
    "Description:           Master Index of EDGAR Dissemination Feed by Form Type\n"
    "Last Data Received:    September 30, 2025\n"
    "Comments:              webmaster@sec.gov\n"
    "\n\n"
    "Form Type   Company Name                                                  CIK         "
    "Date Filed  File Name\n"
    "-------------------------------------------------------------------------------------\n"
    "10-K             Apple Inc.                                                    320193"
    "      2025-10-31  edgar/data/320193/0000320193-25-000079.txt\n"
    "10-K/A           Example Corp                                                  1     "
    "      2025-08-01  edgar/data/1/0000000001-25-000001.txt\n"
    "\n"
    "SCHEDULE 13G/A   Big Fund LP                                                   2     "
    "      2025-07-15  edgar/data/2/0000000002-25-000002.txt\n"
    "4                Insider                                                       3     "
    "      2025-07-16  edgar/data/3/0000000003-25-000003.txt\n"
    "4                Insider Two                                                   4     "
    "      2025-07-17  edgar/data/4/0000000004-25-000004.txt\n"
)

FORMS_SNIPPET = """<table><thead><tr><th>Number</th><th>Description</th><th>Last Updated</th>
<th>SEC Number</th><th>Topic(s)</th></tr></thead><tbody>
<tr><td></td><td>Examination Brochure (PDF)</td><td>Oct. 2026</td><td>SEC 2389</td><td></td></tr>
<tr><td><a href="/files/form10-k.pdf">10-K</a></td>
<td>Annual report pursuant to Section 13 or 15(d) (PDF)</td><td>Feb. 2025</td><td>SEC1673</td>
<td>Securities Exchange Act of 1934, Small Businesses</td></tr>
<tr><td>4</td><td>Statement of changes in beneficial ownership&#039;s (PDF)</td><td>Dec. 2014</td>
<td>SEC1474</td><td>Securities Exchange Act of 1934</td></tr>
<tr><td>X</td><td>Only four cells</td><td>x</td><td>y</td></tr>
</tbody></table>"""


def test_parse_form_index_counts_the_17_char_form_column() -> None:
    module = load_module("refresh_form_types")
    assert module.parse_form_index(INDEX_SNIPPET) == Counter(
        {"4": 2, "10-K": 1, "10-K/A": 1, "SCHEDULE 13G/A": 1}
    )
    assert module.parse_form_index("no separator here\n10-K   x\n") == Counter()


def test_parse_forms_page_reads_five_cell_rows() -> None:
    module = load_module("refresh_form_types")
    rows = module.parse_forms_page(FORMS_SNIPPET)
    assert [row.number for row in rows] == ["10-K", "4"]
    assert rows[0].description == "Annual report pursuant to Section 13 or 15(d)"
    assert rows[0].topics == ("Securities Exchange Act of 1934", "Small Businesses")
    assert rows[0].sec_number == "SEC1673"
    assert rows[1].description == "Statement of changes in beneficial ownership's"


def test_completed_quarters_and_index_url() -> None:
    module = load_module("refresh_form_types")
    assert module.completed_quarters(date(2026, 11, 1), 4) == [
        (2025, 4),
        (2026, 1),
        (2026, 2),
        (2026, 3),
    ]
    assert module.completed_quarters(date(2026, 1, 1), 1) == [(2025, 4)]
    assert module.completed_quarters(date(2026, 4, 15), 2) == [(2025, 4), (2026, 1)]
    assert (
        module.index_url(2025, 3)
        == "https://www.sec.gov/Archives/edgar/full-index/2025/QTR3/form.gz"
    )


def test_build_payload_sorts_decorates_and_renders_deterministically() -> None:
    module = load_module("refresh_form_types")
    rows = module.parse_forms_page(FORMS_SNIPPET) + [
        module.FormsListRow("12b-25", "Notification of late filing", "x", "y", ("Topic",)),
        module.FormsListRow("N-PORT", "Monthly portfolio", "x", "y", ()),
    ]
    counts = Counter({"4": 2, "10-K": 1, "10-K/A": 1, "NT 10-K": 1, "NPORT-P": 7, "424B2": 9})
    payload = module.build_payload(counts, rows, [(2025, 4), (2026, 1)], date(2026, 11, 1))
    assert payload["generated_on"] == "2026-11-01"
    assert payload["quarters"] == ["2025Q4", "2026Q1"]
    forms = [entry["form"] for entry in payload["form_types"]]
    assert forms == ["424B2", "NPORT-P", "4", "10-K", "10-K/A", "NT 10-K"]
    by_form = {entry["form"]: entry for entry in payload["form_types"]}
    assert by_form["424B2"]["description"] is None and by_form["424B2"]["topics"] == []
    assert by_form["NPORT-P"]["description"] == "Monthly portfolio"  # alias
    assert by_form["NT 10-K"]["description"] == "Notification of late filing"  # alias
    assert by_form["10-K/A"]["amendment"] is True
    assert by_form["10-K/A"]["description"] == by_form["10-K"]["description"]  # base match
    rendered = module.render(payload)
    assert rendered == module.render(json.loads(rendered)) and rendered.endswith("\n")
