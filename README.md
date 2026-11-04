# SEC EDGAR MCP Server

<!-- mcp-name: io.github.SudiptoHaldar/mcp-server-edgar -->

[![CI](https://github.com/SudiptoHaldar/mcp_server_edgar/actions/workflows/ci.yml/badge.svg)](https://github.com/SudiptoHaldar/mcp_server_edgar/actions/workflows/ci.yml) [![PyPI](https://img.shields.io/pypi/v/mcp-server-edgar)](https://pypi.org/project/mcp-server-edgar/) [![MCP Registry](https://img.shields.io/badge/MCP_Registry-io.github.SudiptoHaldar%2Fmcp--server--edgar-blue)](https://registry.modelcontextprotocol.io/?q=io.github.SudiptoHaldar%2Fmcp-server-edgar)

A read-only [Model Context Protocol](https://modelcontextprotocol.io/) server, over stdio, that
takes a stock ticker, resolves it to the SEC's Central Index Key (CIK), and returns filing
metadata with official EDGAR links. It exposes three tools, five resources and three prompts.
No API key is needed: SEC EDGAR is public, and the server only identifies itself with a
descriptive `User-Agent`. The server never reads, downloads or summarises filing contents; it
tells you which filings exist and where they are on sec.gov.

Published on PyPI as [`mcp-server-edgar`](https://pypi.org/project/mcp-server-edgar/) and listed
on the [Official MCP Registry](https://registry.modelcontextprotocol.io/?q=io.github.SudiptoHaldar%2Fmcp-server-edgar)
as `io.github.SudiptoHaldar/mcp-server-edgar`.

## Install and run

The server is a Python package with a console entry point. The simplest way to run it is with
[uv](https://docs.astral.sh/uv/), which fetches Python 3.11 or newer when needed:

```sh
uvx mcp-server-edgar
```

Or install it into any environment with Python 3.11 or newer:

```sh
pip install mcp-server-edgar
mcp-server-edgar
```

The executable speaks MCP on stdin and stdout and is meant to be launched by an MCP client, so
on its own it waits for a client. To try it interactively, use the
[MCP Inspector](https://github.com/modelcontextprotocol/inspector):

```sh
npx -y @modelcontextprotocol/inspector uvx mcp-server-edgar
```

## Configure your MCP client

Claude Desktop and most other JSON-configured clients take an `mcpServers` entry. The optional
`env` block sets the SEC fair-access contact (see Configuration):

```json
{
  "mcpServers": {
    "sec-edgar": {
      "command": "uvx",
      "args": ["mcp-server-edgar"],
      "env": {
        "MCP_EDGAR_USER_AGENT": "acme-research/1.0 (ops@acme.example)"
      }
    }
  }
}
```

Claude Code:

```sh
claude mcp add sec-edgar -- uvx mcp-server-edgar
```

## Tools

All three tools are read-only and return a JSON object whose `status` field says what happened
(see Results and errors). Years always refer to the **filing date**, the day the SEC received a
filing, never to the period the filing covers.

### get_latest_filing — Latest SEC filing

Newest filing for a ticker, optionally restricted to given form types.

| Argument | Type | Default | Meaning |
|---|---|---|---|
| `ticker` | string | required | Stock ticker, e.g. `AAPL`; case and surrounding spaces are ignored; `BRK.B` and `BRK-B` are equivalent |
| `forms` | list of strings | all forms | Exact form types, case-insensitive: `["10-K"]` matches `10-K` but not `10-K/A` |
| `cik` | string or integer | none | SEC CIK (1–10 digits), only after an `ambiguous` answer, to pick one of the listed candidates; it must be listed under the ticker |

Example call `get_latest_filing("AAPL", ["10-K"])`; result abridged, values as of 2026-11-01:

```json
{
  "status": "ok",
  "company": {"cik": "0000320193", "name": "Apple Inc.", "ticker": "AAPL", "exchange": "Nasdaq"},
  "forms": ["10-K"],
  "filing": {
    "form": "10-K",
    "filing_date": "2025-10-31",
    "report_date": "2025-09-27",
    "accession_number": "0000320193-25-000079",
    "filing_url": "https://www.sec.gov/Archives/edgar/data/320193/000032019325000079/0000320193-25-000079-index.htm",
    "primary_document_url": "https://www.sec.gov/Archives/edgar/data/320193/000032019325000079/aapl-20250927.htm"
  },
  "message": "Newest 10-K by Apple Inc. (AAPL): filed 2025-10-31."
}
```

`filing` is `null` when the company has no filing of the requested forms.

### list_filings_by_year — Filings by year

Every filing a company submitted during one calendar year, newest first, in pages.

| Argument | Type | Default | Meaning |
|---|---|---|---|
| `ticker` | string | required | As above |
| `year` | integer | required | Calendar year of the **filing date**, 1993 or later; a future year returns an empty list |
| `forms` | list of strings | all forms | As above |
| `limit` | integer | 100 | Page size; larger values are clamped to the server's cap |
| `offset` | integer | 0 | Filings to skip; pass the previous result's `next_offset` |
| `cik` | string or integer | none | As above |

The year is the filing year, not the fiscal year: Apple's 10-K for the fiscal year ending
September 2025 was filed on 2025-10-31, so it belongs to `year=2025`; a 10-K filed in January
2026 for a fiscal year 2025 belongs to `year=2026`. Older years are served from the SEC's
history files automatically.

Example call `list_filings_by_year("AAPL", 2025, ["10-K", "10-Q"])`; result abridged, values as
of 2026-11-01:

```json
{
  "status": "ok",
  "company": {"cik": "0000320193", "name": "Apple Inc.", "ticker": "AAPL", "exchange": "Nasdaq"},
  "year": 2025,
  "forms": ["10-K", "10-Q"],
  "limit": 100,
  "offset": 0,
  "total_count": 4,
  "returned_count": 4,
  "next_offset": null,
  "filings": [
    {
      "form": "10-K",
      "filing_date": "2025-10-31",
      "report_date": "2025-09-27",
      "accession_number": "0000320193-25-000079",
      "filing_url": "https://www.sec.gov/Archives/edgar/data/320193/000032019325000079/0000320193-25-000079-index.htm",
      "primary_document_url": "https://www.sec.gov/Archives/edgar/data/320193/000032019325000079/aapl-20250927.htm"
    }
  ],
  "message": "Apple Inc. (AAPL): 4 filings of form 10-K, 10-Q with a filing date in 2025."
}
```

To page, call again with `offset` set to `next_offset` until it is `null`. Pages never overlap.
A year with no filings is `ok` with `total_count` 0.

### list_form_types — EDGAR form types

The EDGAR submission-type codes filed in recent quarters, most common first. Use it to learn
which values `forms` accepts: `form` is exactly the code EDGAR uses (`10-K`, `10-K/A`,
`SCHEDULE 13G/A`, `424B2`, `NPORT-P`).

| Argument | Type | Default | Meaning |
|---|---|---|---|
| `query` | string | none | Case-insensitive text matched against the code and its description; `"10-K"` lists `10-K`, `10-K/A`, `10-KT`, …; `"/A"` lists amendments |
| `limit` | integer | 100 | Entries per page |
| `offset` | integer | 0 | Entries to skip; pass the previous `next_offset` |

The catalogue is a **snapshot bundled with the package**, counted over the four completed
quarters before the release and refreshed with each release; `catalogue.generated_on` and
`catalogue.quarters` say which. Calling it makes no request to the SEC. `description` and
`topics` come from the SEC Forms List when the form number is known, otherwise `null`.

Example call `list_form_types("10-K")`; result abridged, values as of 2026-11-01:

```json
{
  "status": "ok",
  "query": "10-K",
  "total_count": 6,
  "returned_count": 6,
  "next_offset": null,
  "catalogue": {"generated_on": "2026-11-01", "quarters": ["2025Q4", "2026Q1", "2026Q2", "2026Q3"]},
  "form_types": [
    {"form": "10-K", "description": "Annual report pursuant to Section 13 or 15(d)", "amendment": false, "filings": 6723},
    {"form": "10-K/A", "description": "Annual report pursuant to Section 13 or 15(d)", "amendment": true, "filings": 616}
  ],
  "message": "6 form types match '10-K' in 2025Q4-2026Q3."
}
```

## Results and errors

Every tool answers with a JSON object. Anticipated conditions are reported in `status`, never as
a protocol error, so a client can act on them:

| `status` | Meaning | Also carries |
|---|---|---|
| `ok` | The request succeeded | `company`, and `filing` / `filings` / `form_types` as shown above |
| `not_found` | No SEC issuer is listed under the ticker | — |
| `ambiguous` | Several issuers share the ticker; none is chosen | `candidates`: list of `{cik, name, ticker, exchange}`; call again with the chosen `cik` |
| `invalid_input` | An argument was rejected before any request was made | `field`: the offending argument |
| `upstream_error` | SEC EDGAR did not answer in time, was rate-limited or returned bad data | `retryable` (boolean) and `retry_after_seconds` (may be `null`) |

Every object also carries a one-line `message`. A filing record always has `form`,
`filing_date`, `report_date` (the period covered, context only, may be `null`),
`accession_number` and `filing_url` (the official filing index page on sec.gov);
`primary_document_url` points at the main document when the SEC lists one and the file name is
safe, otherwise it is `null`.

## Resources

Clients that can attach resources as context may read these JSON documents directly, without a
tool call. They are built by the same code path as the tools, so a resource and its tool always
agree.

| URI | Payload |
|---|---|
| `edgar://companies/{ticker}/profile` | `{cik, name, ticker, exchange, tickers}`: the issuer's identity plus every ticker the SEC lists under that CIK (`GOOGL` → `["GOOGL", "GOOG"]`) |
| `edgar://companies/{ticker}/latest` | `{company, filing}`: exactly the `company` and `filing` of `get_latest_filing(ticker)`; `filing` is `null` when the issuer has none |
| `edgar://issuers/{cik}/profile` | The profile addressed by CIK (any 1–10-digit spelling) |
| `edgar://issuers/{cik}/latest` | The latest filing addressed by CIK |
| `edgar://form-types` | `{catalogue, form_types}`: the whole bundled catalogue, unpaged |

Use the `edgar://issuers/{cik}/…` routes when a ticker is ambiguous; the CIK must be listed in
the SEC ticker map. Resources have no `status` envelope: an unknown ticker, a malformed or
unlisted CIK or an unmatched URI is a resource-not-found error, and an ambiguous ticker or an
SEC outage is a resource error, each carrying the same message text the tools use.

## Prompts

Prompts are reusable instructions a person selects from the client's prompt menu; the client
inserts the rendered text and the model then uses the tools. They carry no live data.

| Prompt | Title | Arguments | The rendered text asks the model to |
|---|---|---|---|
| `review_latest_filing` | Review latest SEC filing | `ticker` (required) | call `get_latest_filing`, cite the form and `filing_date`, link `filing_url`, treat `report_date` as the period covered, separate what the filing states from interpretation |
| `review_filings_by_year` | Review filings for a calendar year | `ticker`, `year` (both required) | call `list_filings_by_year`, page with `offset = next_offset` until `null`, group by form type, summarise the year, link every item |
| `explain_form_types` | Explain EDGAR form types | `query` (optional) | call `list_form_types`, explain the common codes, note that `/A` means amendment and that the catalogue is a dated snapshot |

Every prompt ends with the same guardrails: metadata and official sec.gov links only, the server
does not read filing contents, cite the EDGAR link for every filing, give no investment advice,
and on an `ambiguous` answer ask which issuer is meant before calling again with that `cik`.

## Configuration

All settings are environment variables with defaults. Unset or empty values take the default; a
bad value stops startup with exit code 1 and one line on stderr naming the variable.

| Variable | Default | Accepted | Purpose |
|---|---|---|---|
| `MCP_EDGAR_USER_AGENT` | `mcp-server-edgar/<version> (sudiptokanti@yahoo.com)` | non-empty, must contain `@` | SEC fair-access identification with a maintainer contact; set your own |
| `MCP_EDGAR_TIMEOUT_SECONDS` | `30` | number > 0 | HTTP timeout for SEC requests |
| `MCP_EDGAR_TICKER_MAP_TTL_SECONDS` | `86400` | integer ≥ 0 (0 = no cache) | Cache lifetime of the SEC ticker map |
| `MCP_EDGAR_SUBMISSIONS_TTL_SECONDS` | `3600` | integer ≥ 0 | Cache lifetime of a company's submissions |
| `MCP_EDGAR_PAGE_CAP` | `100` | integer 1..1000 | Largest page `list_filings_by_year` and `list_form_types` return |
| `MCP_EDGAR_MAX_REQUESTS_PER_SECOND` | `10` | integer 1..10 | Request ceiling; the SEC allows at most 10 requests per second and the server cannot be set higher |
| `MCP_EDGAR_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` | Log level |

The SEC asks every automated client to identify itself with a contact address, so set
`MCP_EDGAR_USER_AGENT` to your own product name and e-mail. Logs go to stderr only; stdout
carries the protocol.

## How it uses SEC EDGAR

- **Ticker to CIK**: `https://www.sec.gov/files/company_tickers_exchange.json`, cached for a
  day. One ticker listed under several CIKs is reported as `ambiguous`; one CIK with several
  tickers (`GOOGL`, `GOOG`) is one issuer.
- **Filings**: `https://data.sec.gov/submissions/CIK##########.json` (CIK zero-padded to ten
  digits), cached for an hour. Older years are read from the history files the submissions
  document lists, and only those whose date range overlaps the requested year.
- **Links**: the filing index page `…/Archives/edgar/data/<cik>/<accession>/<accession>-index.htm`
  is always returned; the primary document link is built from the SEC's file name after
  rejecting anything that is not a plain file name.
- **Fair access**: at most 10 requests per second, a descriptive `User-Agent`, in-memory
  caching per process, and bounded retries with backoff on HTTP 429 and transient 5xx responses.
  When the SEC still does not answer, the tool returns `upstream_error` with a retry hint rather
  than an empty result. See the SEC's
  [EDGAR API documentation](https://www.sec.gov/search-filings/edgar-application-programming-interfaces).

## Known limits

- Metadata and links only: no filing contents, no XBRL financial facts, no summaries, no
  investment advice.
- `year` means the calendar year the SEC received the filing, not the fiscal year it reports on.
- Years before 1993 (the start of electronic filing) are rejected as `invalid_input`.
- Only issuers in the SEC ticker map can be looked up; funds and private filers without a
  listed ticker are out of scope, and the server never queries an arbitrary CIK.
- The form-type catalogue is a snapshot taken before each release, not a live list.
- Transport is stdio only; there is no HTTP endpoint and no authentication.
- Caches live in memory for the lifetime of one server process.
- This project is not affiliated with or endorsed by the U.S. Securities and Exchange Commission.

## Development

```sh
git clone https://github.com/SudiptoHaldar/mcp_server_edgar
cd mcp_server_edgar
uv sync
uv run pytest -q                                  # unit tests never touch the network
uv run pytest -q -m live                          # opt-in checks against the real SEC
uv run ruff check .
uv run mcp dev src/mcp_edgar/server.py            # MCP Inspector GUI
uv run python scripts/inspector_checklist.py      # 27-row CLI checklist (needs Node and network)
```

Before a release, refresh the bundled form-type catalogue, then build:

```sh
uv run python scripts/refresh_form_types.py
uv build
```

Releases are published by the tag-triggered workflow in `.github/workflows/release.yml`: a
`vX.Y.Z` tag on this repository builds, publishes to PyPI (Trusted Publishing, with attestations)
and to the MCP Registry, and verifies both.

Issues and pull requests: <https://github.com/SudiptoHaldar/mcp_server_edgar/issues>.

## License

MIT, see [LICENSE](https://github.com/SudiptoHaldar/mcp_server_edgar/blob/main/LICENSE). SEC
EDGAR data is public domain and is served here under the SEC's fair-access rules.
