# jtsb_ingest/jtsb.py
"""JTSB (Japan Transport Safety Board) HTML scraper.

Single listing page: https://jtsb.mlit.go.jp/airrep.html
~430 rows, server-rendered, UTF-8 with BOM, no pagination.

Each row encodes: occurrence date, publish date, type (Accident / Serious
Incident), occurrence category, flight phase, operator, aircraft type,
registration, location, English PDF link, Japanese PDF link.

The case_id is extracted from the Japanese PDF URL:
  rep-acci/AA{YYYY}-{m}-{seq}-{REG}.pdf   → AA{YYYY}-{m}-{seq}
  rep-inci/AI{YYYY}-{m}-{seq}-{REG}.pdf   → AI{YYYY}-{m}-{seq}
Older two-part form:
  rep-acci/AA{YYYY}-{seq}-{REG}.pdf        → AA{YYYY}-{seq}
Rows whose Japanese PDF URL lacks an AA/AI prefix (very old or interim
keika reports) receive case_id=None and are skipped.
"""

import re
from urllib.parse import urljoin

BASE = "https://jtsb.mlit.go.jp"
INDEX_URL = "https://jtsb.mlit.go.jp/airrep.html"
DELAY = 1.5

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
_HEADERS = {"User-Agent": _UA}

# ── HTML token helpers ────────────────────────────────────────────────────────

_TAG_RE = re.compile(r"<[^>]+>", re.DOTALL)
_WS_RE = re.compile(r"\s+")


def _cell_text(cell_html: str) -> str:
    """Strip all tags and collapse whitespace from a single TD inner HTML."""
    s = _TAG_RE.sub(" ", cell_html)
    return _WS_RE.sub(" ", s).strip()


# ── Row-level parsers ─────────────────────────────────────────────────────────

# TD pattern: captures inner HTML of each cell (greedy is OK inside one TR)
_TD_RE = re.compile(r"<TD[^>]*>(.*?)</TD>", re.DOTALL | re.IGNORECASE)

# English PDF href
_EN_RE = re.compile(r'<A\s[^>]*href="(eng-air_report/[^"]+)"', re.IGNORECASE)

# Japanese PDF href
_JP_RE = re.compile(r'<A\s[^>]*href="(aircraft/rep-(?:acci|inci)/[^"]+)"', re.IGNORECASE)

# case_id from JP PDF path — captures AA/AI prefix with 2 or 3 numeric segments
_CASE_RE = re.compile(
    r"rep-(?:acci|inci)/(A[AI]\d{4}-\d+(?:-\d+)?)-",
)

# date YYYY.MM.DD → ISO
_DATE_RE = re.compile(r"(\d{4})\.(\d{2})\.(\d{2})")

# TR blocks
_TR_RE = re.compile(r"<TR>(.*?)</TR>", re.DOTALL | re.IGNORECASE)


def _parse_date(raw: str) -> str | None:
    m = _DATE_RE.search(raw)
    if not m:
        return None
    y, mo, d = m.groups()
    return f"{y}-{mo}-{d}"


def _normalise_type(raw: str) -> str | None:
    s = raw.strip()
    if not s:
        return None
    if "Serious" in s:
        return "Serious Incident"
    if "Accident" in s:
        return "Accident"
    return s or None


def parse_listing(html: str) -> list[dict]:
    """
    Parse the JTSB airrep.html listing page.

    Returns a list of dicts with keys:
        case_id, report_url, pdf_url, jp_pdf_url,
        report_type, category, flight_phase, operator,
        aircraft, registration, date_of_occurrence, location

    Skips rows that:
      - are header rows (contain <th )
      - lack an English PDF link
      - lack a Japanese PDF link with a parseable AA/AI case_id
    """
    rows = []
    seen: set[str] = set()

    for tr_m in _TR_RE.finditer(html):
        tr_inner = tr_m.group(1)

        # Skip header rows
        if re.search(r"<th\b", tr_inner, re.IGNORECASE):
            continue

        cells = _TD_RE.findall(tr_inner)
        if len(cells) < 9:
            continue

        # ── English PDF ──────────────────────────────────────────────────
        # Links live in the last cell (index 9)
        link_cell = cells[9] if len(cells) > 9 else tr_inner
        en_m = _EN_RE.search(link_cell)
        if not en_m:
            continue
        en_href = en_m.group(1)
        report_url = urljoin(BASE + "/", en_href)
        pdf_url = report_url

        # ── Japanese PDF → case_id ───────────────────────────────────────
        jp_m = _JP_RE.search(link_cell)
        if not jp_m:
            continue
        jp_href = jp_m.group(1)
        jp_pdf_url = urljoin(BASE + "/", jp_href)

        case_m = _CASE_RE.search(jp_href)
        if not case_m:
            continue
        case_id = case_m.group(1)

        if case_id in seen:
            # Duplicate (interim re-listing of same investigation)
            continue
        seen.add(case_id)

        # ── Metadata cells ───────────────────────────────────────────────
        # Canonical column order (most rows):
        #   0: date_of_occurrence, 1: date_of_publication,
        #   2: type, 3: category, 4: flight_phase,
        #   5: operator, 6: aircraft, 7: registration, 8: location, 9: PDFs
        #
        # A minority of older rows have category and flight_phase in cols 2-3
        # with type in col 4. Detect by scanning cols 2-4 for type keywords.
        date_of_occurrence = _parse_date(_cell_text(cells[0]))
        operator = _cell_text(cells[5]) or None
        aircraft = _cell_text(cells[6]) or None
        registration = _cell_text(cells[7]) or None
        location = _cell_text(cells[8]) or None

        # Find report_type by scanning cells 2-4
        report_type = None
        type_col = None
        for col in (2, 3, 4):
            t = _normalise_type(_cell_text(cells[col]))
            if t in ("Accident", "Serious Incident"):
                report_type = t
                type_col = col
                break

        # category and flight_phase occupy the remaining cols from {2,3,4}
        # after removing type_col; canonical fallback is col 3 / col 4.
        if type_col is not None:
            remaining = [c for c in (2, 3, 4) if c != type_col]
            category = _cell_text(cells[remaining[0]]) or None
            flight_phase = _cell_text(cells[remaining[1]]) or None
        else:
            # type not found (shouldn't happen); fill defensively
            report_type = _cell_text(cells[2]) or None
            category = _cell_text(cells[3]) or None
            flight_phase = _cell_text(cells[4]) or None

        rows.append(
            {
                "case_id": case_id,
                "report_url": report_url,
                "pdf_url": pdf_url,
                "jp_pdf_url": jp_pdf_url,
                "report_type": report_type,
                "category": category,
                "flight_phase": flight_phase,
                "operator": operator,
                "aircraft": aircraft,
                "registration": registration,
                "date_of_occurrence": date_of_occurrence,
                "location": location,
            }
        )

    return rows


# ── Network helpers ───────────────────────────────────────────────────────────


def iter_index(client) -> list[dict]:
    """
    GET INDEX_URL and return parse_listing(resp.text).

    The caller is responsible for setting the User-Agent on the client.
    httpx decodes UTF-8 with BOM transparently; falls back to utf-8 on errors.
    """
    resp = client.get(INDEX_URL)
    resp.raise_for_status()
    # httpx may return bytes for content; handle both
    if isinstance(resp.content, bytes):
        html = resp.content.decode("utf-8-sig", errors="replace")
    else:
        html = resp.text
    return parse_listing(html)


def download(client, pdf_url: str, dest: str) -> None:
    """
    Download pdf_url to dest (binary).  Raises RuntimeError on non-200.

    The caller is responsible for setting the User-Agent on the client.
    """
    resp = client.get(pdf_url)
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code} for {pdf_url}")
    with open(dest, "wb") as fh:
        fh.write(resp.content)
