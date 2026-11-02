"""Regenerate ``src/mcp_edgar/data/form_types.json`` from the SEC (run before each release).

Sources (gotchas #20): the quarterly EDGAR full index by form type
(``https://www.sec.gov/Archives/edgar/full-index/<YYYY>/QTR<n>/form.gz``, ~5 MB gzip each) for
the last four completed quarters, and the SEC Forms List (``https://www.sec.gov/forms?page=N``)
for descriptions and topics. Eleven requests, ~20 MB; never run at tool time.

    uv run python scripts/refresh_form_types.py [--quarters 4] [--out PATH] [--today YYYY-MM-DD]

Exit codes: 0 written, 1 fetch/parse failure (nothing written). No ``from __future__ import
annotations`` here: the dataclass must survive being loaded by file path (gotchas #18).
"""

import argparse
import gzip
import html
import json
import re
import sys
import time
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import httpx

from mcp_edgar.settings import Settings

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "src" / "mcp_edgar" / "data" / "form_types.json"
INDEX_URL = "https://www.sec.gov/Archives/edgar/full-index/{year}/QTR{quarter}/form.gz"
FORMS_URL = "https://www.sec.gov/forms"
FORM_COLUMN_WIDTH = (
    17  # data rows pad the form type to 17 chars (codes are <= 15); header is narrower
)
MAX_FORMS_PAGES = 20
PAUSE_SECONDS = 0.2
TIMEOUT_SECONDS = 120

# EDGAR submission-type base code -> SEC Forms List number (targets verified live 2026-11-01).
ALIASES: dict[str, str] = {
    "NPORT-P": "N-PORT",
    "NPORT-EX": "N-PORT",
    "13F-HR": "13F",
    "13F-NT": "13F",
    "N-CSRS": "N-CSR",
    "N-MFP2": "N-MFP",
    "N-MFP3": "N-MFP",
    "24F-2NT": "24F-2",
    "25-NSE": "25",
    "S-8 POS": "S-8",
    "8-A12B": "8-A",
    "8-A12G": "8-A",
    "NT 10-K": "12b-25",
    "NT 10-Q": "12b-25",
    "NT 20-F": "12b-25",
    "NT 11-K": "12b-25",
}


@dataclass(frozen=True)
class FormsListRow:
    number: str
    description: str
    last_updated: str
    sec_number: str
    topics: tuple[str, ...]


# --- pure helpers ---------------------------------------------------------------------------


def completed_quarters(today: date, count: int) -> list[tuple[int, int]]:
    """The last ``count`` completed calendar quarters, oldest first (the current one excluded)."""
    year, quarter = today.year, (today.month - 1) // 3 + 1
    out: list[tuple[int, int]] = []
    for _ in range(count):
        quarter -= 1
        if quarter == 0:
            quarter, year = 4, year - 1
        out.append((year, quarter))
    return list(reversed(out))


def index_url(year: int, quarter: int) -> str:
    return INDEX_URL.format(year=year, quarter=quarter)


def parse_form_index(text: str) -> Counter[str]:
    """Count form types in a ``form.idx`` body: skip the header up to the ``---`` separator."""
    counts: Counter[str] = Counter()
    started = False
    for line in text.splitlines():
        if not started:
            started = line.startswith("---")
            continue
        if line.strip():
            counts[line[:FORM_COLUMN_WIDTH].strip()] += 1
    return counts


def _clean(cell: str) -> str:
    return html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", cell))).strip()


def parse_forms_page(page_html: str) -> list[FormsListRow]:
    """Rows of one Forms List page (five cells each); rows without a number are skipped."""
    rows: list[FormsListRow] = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", page_html, flags=re.S):
        tds = re.findall(r"<td[^>]*>(.*?)</td>", tr, flags=re.S)
        if len(tds) != 5:
            continue
        cells = [_clean(td) for td in tds]
        if not cells[0]:
            continue
        description = re.sub(r"\s*\(PDF\)$", "", cells[1])
        topics = tuple(t.strip() for t in cells[4].split(",") if t.strip())
        rows.append(FormsListRow(cells[0], description, cells[2], cells[3], topics))
    return rows


def build_payload(
    counts: Counter[str],
    rows: Iterable[FormsListRow],
    quarters: list[tuple[int, int]],
    today: date,
) -> dict[str, Any]:
    by_number = {row.number: row for row in rows}
    entries: list[dict[str, Any]] = []
    for form, filings in counts.items():
        base = form[:-2] if form.endswith("/A") else form
        row = by_number.get(ALIASES.get(base, base))
        entries.append(
            {
                "form": form,
                "description": row.description if row else None,
                "amendment": form.endswith("/A"),
                "filings": filings,
                "topics": list(row.topics) if row else [],
            }
        )
    entries.sort(key=lambda entry: (-entry["filings"], entry["form"]))
    return {
        "generated_on": today.isoformat(),
        "quarters": [f"{year}Q{quarter}" for year, quarter in quarters],
        "sources": [INDEX_URL, FORMS_URL],
        "form_types": entries,
    }


def render(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


# --- fetching -------------------------------------------------------------------------------


def fetch_index_counts(client: httpx.Client, quarters: list[tuple[int, int]]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for year, quarter in quarters:
        url = index_url(year, quarter)
        print(f"fetching {url}", file=sys.stderr)
        response = client.get(url)
        response.raise_for_status()
        counts.update(parse_form_index(gzip.decompress(response.content).decode("latin-1")))
        time.sleep(PAUSE_SECONDS)
    return counts


def fetch_forms_list(client: httpx.Client) -> list[FormsListRow]:
    rows: list[FormsListRow] = []
    for page in range(MAX_FORMS_PAGES):
        response = client.get(FORMS_URL, params={"page": page})
        response.raise_for_status()
        page_rows = parse_forms_page(response.text)
        if not page_rows:
            break
        rows.extend(page_rows)
        time.sleep(PAUSE_SECONDS)
    return rows


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    tmp.replace(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--quarters", type=int, default=4, help="completed quarters to count")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--today", type=date.fromisoformat, default=date.today())
    args = parser.parse_args(argv)
    quarters = completed_quarters(args.today, args.quarters)
    headers = {"User-Agent": Settings.from_env({}).user_agent, "Accept-Encoding": "gzip, deflate"}
    try:
        with httpx.Client(headers=headers, timeout=TIMEOUT_SECONDS, follow_redirects=True) as c:
            counts = fetch_index_counts(c, quarters)
            rows = fetch_forms_list(c)
        if not counts or not rows:
            raise ValueError("no form types or no Forms List rows were parsed")
        payload = build_payload(counts, rows, quarters, args.today)
        write_atomic(args.out, render(payload))
    except (httpx.HTTPError, OSError, ValueError) as exc:
        print(f"refresh failed: {exc}", file=sys.stderr)
        return 1
    described = sum(1 for entry in payload["form_types"] if entry["description"])
    print(
        f"wrote {args.out}: {len(payload['form_types'])} form types ({described} with a "
        f"description) over {', '.join(payload['quarters'])}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
