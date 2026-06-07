# tsb_ingest/tsb.py
"""
TSB (Transportation Safety Board of Canada) aviation investigation index
and report HTML parser.

The TSB aviation index is a single-page DataTables table with ~1300+ rows.
Each row encodes: occurrence status, case_id (as a link), event date (<time>),
an occurrence-info div, and occurrence schedule/status.

Report pages are Drupal 9 HTML.  Narrative content lives in
``field--name-field-body`` divs inside a ``<main>`` element.
"""
import html as _html
import re
import time
from urllib.parse import urljoin

INDEX_URL = "https://www.tsb.gc.ca/eng/rapports-reports/aviation/index.html"
BASE = "https://www.tsb.gc.ca"
DELAY = 1.0

# Browser User-Agent for all outbound requests
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {"User-Agent": UA}

# ──────────────────────────────────────────────────────────────────────────────
# Index parsing
# ──────────────────────────────────────────────────────────────────────────────

# Each data row in the tbody
_TR_RE = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.DOTALL)

# 2nd cell: <a href="/eng/.../aXXY0000/aXXY0000.html">AXXY0000</a>
_CASE_LINK_RE = re.compile(
    r'<a\s+href="(/[^"]+/([A-Za-z0-9][^/]+)/[^"]+\.html)"[^>]*>([^<]+)</a>',
    re.IGNORECASE,
)

# <time datetime="2024-06-28T12:00:00Z">…</time>
_TIME_RE = re.compile(r'<time\s[^>]*datetime="([^"T]+)', re.IGNORECASE)

# Occurrence legacy-text div
_OCC_DIV_RE = re.compile(
    r'field--name-field-occurrence-legacy-text[^>]*>(.*?)</div>',
    re.DOTALL,
)

# Cells in a row (non-greedy, handles nested content via DOTALL)
_TD_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.DOTALL)


def _strip_tags(fragment: str) -> str:
    """Strip all HTML tags from fragment and unescape entities."""
    text = re.sub(r"<[^>]+>", " ", fragment)
    text = _html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _parse_occ_div(occ_html: str) -> dict:
    """
    Extract occurrence_type, operator, aircraft, location from the
    occurrence-legacy-text div inner HTML.

    The canonical structure is a <p> with lines separated by <br>:
        <strong>Type</strong><br>Operator<br>Aircraft, C-XXXX<br>Location

    Any line may be absent.  Returns dict with those 4 keys (None if absent).
    """
    # Find the <p> inside the div
    p_m = re.search(r"<p[^>]*>(.*?)</p>", occ_html, re.DOTALL)
    if not p_m:
        raw = _strip_tags(occ_html)
        return {
            "occurrence_type": raw or None,
            "operator": None,
            "aircraft": None,
            "location": None,
        }

    p_inner = p_m.group(1)
    # Split on <br> to get individual line fragments
    parts = re.split(r"<br\s*/?>", p_inner)
    lines = [_strip_tags(p) for p in parts]
    lines = [l for l in lines if l]  # drop empty

    return {
        "occurrence_type": lines[0] if len(lines) > 0 else None,
        "operator": lines[1] if len(lines) > 1 else None,
        "aircraft": lines[2] if len(lines) > 2 else None,
        "location": lines[3] if len(lines) > 3 else None,
    }


def parse_index(html: str) -> list[dict]:
    """
    Parse the TSB aviation index HTML and return a list of dicts.

    Each dict:
        case_id          – e.g. "A24A0019"
        report_url       – absolute https URL to the report HTML
        event_date       – ISO date "YYYY-MM-DD" from <time datetime> or None
        occurrence_type  – plain text (from <strong> in occurrence div) or None
        operator         – plain text or None
        aircraft         – plain text or None
        location         – plain text or None
        occurrence_status – plain text of the 5th cell (e.g. "Completed")
    """
    # Locate the <tbody> to avoid picking up header rows
    tbody_m = re.search(r"<tbody[^>]*>(.*?)</tbody>", html, re.DOTALL)
    if tbody_m:
        body_html = tbody_m.group(1)
    else:
        body_html = html

    results = []
    for tr_m in _TR_RE.finditer(body_html):
        row_html = tr_m.group(1)

        # ── case_id + report_url ──────────────────────────────────────────────
        link_m = _CASE_LINK_RE.search(row_html)
        if not link_m:
            continue  # no report link → skip (e.g. header row)

        href = link_m.group(1)
        # The link text is the canonical case_id; strip whitespace
        case_id = _html.unescape(link_m.group(3)).strip()
        if not case_id:
            continue

        report_url = urljoin(BASE, href)

        # ── event_date ────────────────────────────────────────────────────────
        time_m = _TIME_RE.search(row_html)
        if time_m:
            # datetime attr is ISO-8601; take date portion only
            raw_dt = time_m.group(1).strip()
            # Normalise to YYYY-MM-DD
            date_m = re.match(r"(\d{4}-\d{2}-\d{2})", raw_dt)
            event_date = date_m.group(1) if date_m else raw_dt
        else:
            event_date = None

        # ── occurrence info ───────────────────────────────────────────────────
        occ_m = _OCC_DIV_RE.search(row_html)
        if occ_m:
            occ_info = _parse_occ_div(occ_m.group(1))
        else:
            occ_info = {
                "occurrence_type": None,
                "operator": None,
                "aircraft": None,
                "location": None,
            }

        # ── occurrence_status (5th <td>) ──────────────────────────────────────
        tds = _TD_RE.findall(row_html)
        # Structure: [0]=occurrence_status_col1(Active/Completed), [1]=case_id,
        # [2]=date, [3]=occurrence_info, [4]=schedule/status date
        # Actually from inspection: col0=occurrence_status, col4=scheduled_date
        occurrence_status = _strip_tags(tds[0]) if tds else None

        results.append(
            {
                "case_id": case_id,
                "report_url": report_url,
                "event_date": event_date,
                "occurrence_status": occurrence_status,
                **occ_info,
            }
        )

    return results


# ──────────────────────────────────────────────────────────────────────────────
# Report parsing
# ──────────────────────────────────────────────────────────────────────────────

# The boilerplate disclaimer sentence to detect and strip
_DISCLAIMER_BLOCK_RE = re.compile(
    r"<section[^>]*gc-srvinfo[^>]*>.*?</section>",
    re.DOTALL,
)

# Elements whose text we never want
_NAV_RE = re.compile(r"<nav[^>]*>.*?</nav>", re.DOTALL)
_FOOTER_RE = re.compile(r"<footer[^>]*>.*?</footer>", re.DOTALL)
_HEADER_RE = re.compile(r"<header[^>]*>.*?</header>", re.DOTALL)
_TOC_RE = re.compile(r'<div[^>]*tsb_common_toc[^>]*>.*?</div>', re.DOTALL)
_REPORT_LINKS_RE = re.compile(
    r'<section[^>]*block-tsb-common-report-links[^>]*>.*?</section>',
    re.DOTALL,
)
# field--name-field-disclaimer-block (TSB investigation process boilerplate)
_DISCLAIMER_FIELD_RE = re.compile(
    r'<div[^>]*field--name-field-disclaimer-block[^>]*>.*?</div>\s*</div>',
    re.DOTALL,
)

# field--name-field-body divs hold actual report section bodies (completed reports)
_FIELD_BODY_RE = re.compile(
    r'<div\s+class="field\s+field--name-field-body[^"]*"[^>]*>(.*?)</div>\s*\n?\s*</div>',
    re.DOTALL,
)

# field--name-body divs hold narrative text in active/class-4 investigation pages
_BODY_RE = re.compile(
    r'<div\s+class="field\s+field--name-body[^"]*"[^>]*>(.*?)</div>\s*\n?\s*</div>',
    re.DOTALL,
)

# Minimum character threshold to accept a chunk as substantive content
_MIN_CHUNK_CHARS = 150

# Fallback: <p> and heading tags anywhere in the stripped main
_PARA_RE = re.compile(r"<(?:p|h[2-6]|li)[^>]*>(.*?)</(?:p|h[2-6]|li)>", re.DOTALL)


def _html_to_text(fragment: str) -> str:
    """Convert an HTML fragment to clean plain text."""
    # Replace block-level breaks with newline
    text = re.sub(r"<br\s*/?>", "\n", fragment)
    text = re.sub(r"</(?:p|li|h[2-6]|div|section)>", "\n", text)
    # Strip remaining tags
    text = re.sub(r"<[^>]+>", " ", text)
    text = _html.unescape(text)
    # Collapse horizontal whitespace, preserve paragraph breaks
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def parse_report(html: str) -> str:
    """
    Extract the narrative text from a TSB report HTML page.

    Strategy:
    1. Locate the ``<main>`` element (Drupal content region).
    2. Strip nav / footer / header / disclaimer / TOC noise blocks.
    3. Collect text from all ``field--name-field-body`` divs (report sections).
    4. If none found (active investigations with minimal content), fall back to
       all ``<p>`` / heading tags inside ``<main>``.
    5. Collapse whitespace and return clean plain text.
    """
    # ── 1. Extract <main> ─────────────────────────────────────────────────────
    main_m = re.search(r"<main\b[^>]*>", html)
    if main_m:
        main_end = html.rfind("</main>")
        if main_end > main_m.start():
            main = html[main_m.start(): main_end + 7]
        else:
            main = html[main_m.start():]
    else:
        main = html

    # ── 2. Strip noise ────────────────────────────────────────────────────────
    for pattern in (
        _DISCLAIMER_BLOCK_RE,
        _NAV_RE,
        _FOOTER_RE,
        _HEADER_RE,
        _TOC_RE,
        _REPORT_LINKS_RE,
        _DISCLAIMER_FIELD_RE,
    ):
        main = pattern.sub(" ", main)

    # ── 3. Collect field-body sections ────────────────────────────────────────
    # Collect both field--name-field-body (completed multi-section reports)
    # and field--name-body (active/class-4 investigation pages)
    field_body_chunks = _FIELD_BODY_RE.findall(main)
    body_chunks = _BODY_RE.findall(main)

    all_chunks = field_body_chunks + body_chunks

    if all_chunks:
        parts = []
        for chunk in all_chunks:
            t = _html_to_text(chunk)
            # Skip trivially short boilerplate fragments
            if len(t) >= _MIN_CHUNK_CHARS:
                parts.append(t)
        text = "\n\n".join(parts)
    else:
        # ── 4. Fallback: all <p> / headings in main ───────────────────────────
        para_chunks = _PARA_RE.findall(main)
        parts = [_html_to_text(chunk) for chunk in para_chunks]
        text = "\n\n".join(p for p in parts if p)

    # ── 5. Final cleanup ──────────────────────────────────────────────────────
    # Remove any residual disclaimer sentence that slipped through
    text = re.sub(
        r"It is not the function of the Board to assign fault[^.]*\.",
        "",
        text,
    )
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ──────────────────────────────────────────────────────────────────────────────
# HTTP helpers (live network; not used in offline tests)
# ──────────────────────────────────────────────────────────────────────────────


def iter_index(client) -> list[dict]:
    """
    GET INDEX_URL and return parse_index(response.text).

    client: httpx.Client (or compatible) configured with HEADERS.
    """
    resp = client.get(INDEX_URL)
    resp.raise_for_status()
    return parse_index(resp.text)


def fetch_report(client, url: str) -> str:
    """
    GET url and return resp.text.

    client: httpx.Client (or compatible).
    url: absolute report URL.
    """
    resp = client.get(url)
    resp.raise_for_status()
    return resp.text
