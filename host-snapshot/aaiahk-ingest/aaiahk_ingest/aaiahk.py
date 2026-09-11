# aaiahk_ingest/aaiahk.py
"""HK AAIA (Hong Kong Air Accident Investigation Authority) HTML scraper.

Source: https://www.tlb.gov.hk/aaia/eng/investigation_reports/index.html
- ONE server-rendered HTML page holds the whole report register as a <table>.
- Each <tr> is one occurrence with columns:
    Year (hidden) | Date (local time) | Classification | Description |
    Status | Preliminary Reports / Public Notice | Interim Statement |
    Investigation Report
  The last three columns each hold zero or one PDF download link.
- The canonical case_id (IVR-YYYY-NN / ITR-YYYY-NN / PLR-YYYY-NN) lives in the
  anchor TEXT ("Download IVR-2025-01"), NOT in the href filename — legacy hrefs
  use schemes like "Investigation Report 05_2022.pdf".
- PDFs live under /aaia/doc/ and serve fine with a browser UA + Referer; they
  are English text-layer PDFs.

case_id resolution per row (richest narrative first):
    Investigation Report link  -> IVR case_id  (final report)
    else Interim Statement link -> ITR case_id (interim)
    else Preliminary/Notice link -> PLR case_id (preliminary)
A row with no downloadable PDF in any of those columns is skipped.

pdf_url follows the SAME precedence (final > interim > preliminary) so the
chosen narrative matches the case_id.
"""
import html as _html
import re
import datetime

BASE = "https://www.tlb.gov.hk"
INDEX_URL = BASE + "/aaia/eng/investigation_reports/index.html"
REFERER = INDEX_URL
DELAY = 1.8

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0 Safari/537.36"
)

HEADERS = {
    "User-Agent": UA,
    "Referer": REFERER,
}

# ──────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────

_MONTHS_EN = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
    # tolerate common abbreviations
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}

# ──────────────────────────────────────────────
# Compiled regexes
# ──────────────────────────────────────────────

_TABLE_RE = re.compile(r"<table.*?</table>", re.DOTALL | re.IGNORECASE)
_TR_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.DOTALL | re.IGNORECASE)
_TD_RE = re.compile(r"<td.*?</td>", re.DOTALL | re.IGNORECASE)
_A_RE = re.compile(r"""<a[^>]+href=["']([^"']+)["'][^>]*>(.*?)</a>""", re.DOTALL | re.IGNORECASE)

# A code token like IVR-2025-01, ITR-2026-02, PLR-2024-03 (also tolerant of
# legacy underscores / single-segment numbers in the anchor text).
# The leading (?<![A-Za-z]) prevents bleeding on neighbour keys such as
# 'SURVIVR-…' or other source codes that merely *contain* the substring.
_CODE_RE = re.compile(
    r"(?<![A-Za-z])((IVR|ITR|PLR)[-_ ]?(\d{4})[-_ ](\d{1,3}))(?![A-Za-z])",
    re.IGNORECASE,
)

# "DD Month YYYY" date in the Date column.
_DATE_RE = re.compile(r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})")

# True location prepositions — used to bound the aircraft head so a
# location-internal 'of' cannot win.  Flight-phase words (during/after/on
# Take-off) are deliberately excluded: the aircraft connective 'of' often
# follows them ("... during Take-off and Landing of BGD Dual 2 ...").
_LOC_KW = (
    r"at|en[- ]?route|enroute|over|along|near"
)
# Pattern A: "<event> of|involving <Aircraft> <loc-kw> …" — take the LAST
# of/involving so 'Loss of Control - Inflight of Zlin …' resolves correctly.
_AIRCRAFT_OF_RE = re.compile(
    r"\b(?:of|involving)\s+(.+?)(?=\s+(?:" + _LOC_KW + r")\b|$)",
    re.IGNORECASE,
)
# Pattern B (aircraft-first): "<Aircraft> Accident|Collision|Commanded… at …".
_AIRCRAFT_FIRST_RE = re.compile(
    r"^(.+?)\s+(?:Accident|Incident|Collision|Commanded)\b",
    re.IGNORECASE,
)


# ──────────────────────────────────────────────
# Client factory
# ──────────────────────────────────────────────

def make_client():
    """Return an httpx.Client configured with a browser UA and Referer."""
    import httpx
    return httpx.Client(
        headers=HEADERS,
        follow_redirects=True,
        timeout=60.0,
    )


# ──────────────────────────────────────────────
# case_id helpers
# ──────────────────────────────────────────────

def _normalize_case_id(raw: str) -> str | None:
    """
    Normalize a raw code token to canonical 'PREFIX-YYYY-NN' form.

    Accepts variants: 'IVR-2025-1', 'IVR_2025_01', 'ivr 2026 2', 'ITR-2026-02'.
    Returns None when no IVR/ITR/PLR code is present.
    """
    if not raw:
        return None
    m = _CODE_RE.search(raw)
    if not m:
        return None
    prefix = m.group(2).upper()
    year = m.group(3)
    seq = int(m.group(4))
    return f"{prefix}-{year}-{seq:02d}"


def make_case_id(*candidates: str) -> str | None:
    """
    Return the first normalizable case_id from the given anchor-text candidates,
    in caller-supplied precedence order (IVR text first, then ITR, then PLR).
    """
    for cand in candidates:
        cid = _normalize_case_id(cand or "")
        if cid:
            return cid
    return None


# ──────────────────────────────────────────────
# Row-field parsing helpers
# ──────────────────────────────────────────────

def _cell_text(td_html: str) -> str:
    return _html.unescape(_TAG_SUB(td_html)).strip()


_TAGS = re.compile(r"<[^>]+>")
_WSX = re.compile(r"\s+")


def _TAG_SUB(s: str) -> str:
    return _WSX.sub(" ", _TAGS.sub(" ", s)).strip()


def _first_link(td_html: str) -> tuple[str | None, str | None]:
    """Return (href, anchor_text) of the first <a> in the cell, or (None, None)."""
    m = _A_RE.search(td_html)
    if not m:
        return None, None
    href = _html.unescape(m.group(1).strip())
    txt = _html.unescape(_TAG_SUB(m.group(2)))
    return href, txt


def _abs_url(href: str | None) -> str | None:
    if not href:
        return None
    if href.startswith("http"):
        return href
    if href.startswith("/"):
        return BASE + href
    # relative like ../../doc/...  -> normalise against the doc root
    href = re.sub(r"^(?:\.\./)+", "/aaia/", href)
    if not href.startswith("/"):
        href = "/aaia/eng/investigation_reports/" + href
    return BASE + href


def _parse_date(text: str) -> str | None:
    """Parse 'DD Month YYYY' → ISO 'YYYY-MM-DD', or None."""
    m = _DATE_RE.search(text or "")
    if not m:
        return None
    day, month_name, year = int(m.group(1)), m.group(2).lower(), int(m.group(3))
    month = _MONTHS_EN.get(month_name)
    if not month:
        return None
    try:
        return datetime.date(year, month, day).isoformat()
    except ValueError:
        return None


def _parse_aircraft(description: str) -> str | None:
    """
    Best-effort aircraft-type extraction from the AAIA description.

    Two description shapes occur:
      A. "<event phrase> of|involving <Aircraft> <loc-kw> <location>"
      B. "<Aircraft> Accident|Collision|Commanded ... at <location>"

    Pattern A is preferred (covers the vast majority); the LAST 'of/involving'
    is used so phrases like 'Loss of Control - Inflight of Zlin Z242L' resolve
    to the aircraft, not 'Control'.  Pattern B handles aircraft-first rows.
    Returns None when neither shape matches.
    """
    if not description:
        return None

    ac = None
    # Restrict to the text BEFORE the first location/phase keyword so a
    # location-internal 'of' ("along the shore of Fu Tau Sha") cannot win.
    head_m = re.search(r"\b(?:" + _LOC_KW + r")\b", description, re.IGNORECASE)
    head = description[: head_m.start()] if head_m else description
    # Take the text after the LAST 'of'/'involving' connector in the head, so
    # 'Loss of Control - Inflight of Zlin Z242L' -> 'Zlin Z242L'.
    conn_matches = list(re.finditer(r"\b(?:of|involving)\s+", head, re.IGNORECASE))
    if conn_matches:
        ac = head[conn_matches[-1].end():]
    else:
        m = _AIRCRAFT_FIRST_RE.match(description)
        if m:
            ac = m.group(1)

    if not ac:
        return None
    ac = ac.strip().rstrip(".,").strip()
    # Trim a trailing flight-phase clause that the head heuristic may have kept
    # ("Boeing 787 during an Approach to ..." -> "Boeing 787").
    ac = re.split(
        r"\s+(?:during|after|on\s+departure|on\s+approach|on\s+an|on\s+a\s)\b",
        ac, maxsplit=1, flags=re.IGNORECASE,
    )[0].strip()
    # drop a trailing possessive ("Airbus A330-343's" -> "Airbus A330-343")
    ac = re.sub(r"['’]s$", "", ac).strip()
    return ac or None


def _classify(classification: str) -> str | None:
    """Normalize the Classification cell to a canonical report_type."""
    c = (classification or "").strip().lower()
    if not c:
        return None
    if c.startswith("serious"):
        return "Serious incident"
    if c.startswith("accident"):
        return "Accident"
    if c.startswith("incident"):
        return "Incident"
    return classification.strip()


# ──────────────────────────────────────────────
# Discovery + listing parse (single page)
# ──────────────────────────────────────────────

def parse_listing(html: str, year_url: str = "") -> list[dict]:
    """
    Parse the single AAIA register page → list of occurrence dicts.

    Each dict has:
      case_id            str   canonical 'IVR-2025-01' (or ITR-/PLR- fallback)
      report_url         None  (no separate landing page; here for parity)
      pdf_url_es         None  (parity column; AAIA is English-only)
      pdf_url_en         str|None  the chosen (richest-tier) report PDF URL
      pdf_url            str|None  same as pdf_url_en (the selected document)
      event_class        str|None  'Accident' | 'Serious incident' | 'Incident'
      aircraft           str|None
      registration       None  (not in the listing; lives inside the PDF)
      date_of_occurrence str|None  ISO YYYY-MM-DD
      location           str|None  (description text retained for slugging)
      title              str   the full Description text
      superseded_codes   list[str]  see below

    Rows with no derivable case_id (no IVR/ITR/PLR download link) are skipped.
    De-duplicates on case_id (keeps the first occurrence).

    Each dict also carries:
      superseded_codes  list[str]  all lower-priority codes on the same row
                                   that are superseded by case_id.  Empty when
                                   case_id is the only code on the row (e.g. a
                                   plain PLR-only row with no IVR/ITR yet).
                                   Used by discover() to mark stale
                                   ITR/PLR rows as superseded so build()
                                   can clean them out of aaiahk_accidents.
    """
    tbl_m = _TABLE_RE.search(html)
    if not tbl_m:
        return []
    table = tbl_m.group(0)

    rows: list[dict] = []
    seen: set[str] = set()

    for tr_m in _TR_RE.finditer(table):
        tr = tr_m.group(1)
        if "<th" in tr.lower():
            continue  # header row
        tds = _TD_RE.findall(tr)
        if len(tds) < 8:
            continue

        date_text = _cell_text(tds[1])
        classification = _cell_text(tds[2])
        description = _cell_text(tds[3])

        pre_href, pre_txt = _first_link(tds[5])  # Preliminary / Public Notice
        itr_href, itr_txt = _first_link(tds[6])  # Interim Statement
        ivr_href, ivr_txt = _first_link(tds[7])  # Investigation Report

        # case_id precedence: final report > interim > preliminary
        case_id = make_case_id(ivr_txt, itr_txt, pre_txt)
        if not case_id:
            continue
        if case_id in seen:
            continue
        seen.add(case_id)

        # pdf_url precedence matches case_id precedence
        if ivr_href:
            pdf_url = _abs_url(ivr_href)
        elif itr_href:
            pdf_url = _abs_url(itr_href)
        else:
            pdf_url = _abs_url(pre_href)

        # All codes present on this row (all ITR/PLR links in each column).
        # Any code that is NOT case_id is superseded by case_id.
        all_codes_on_row: list[str] = []
        for td in (tds[5], tds[6], tds[7]):
            for m in _A_RE.finditer(td):
                txt = _html.unescape(_TAG_SUB(m.group(2)))
                cid = _normalize_case_id(txt)
                if cid and cid not in all_codes_on_row:
                    all_codes_on_row.append(cid)
        superseded_codes = [c for c in all_codes_on_row if c != case_id]

        rows.append({
            "case_id": case_id,
            "report_url": None,
            "pdf_url_es": None,
            "pdf_url_en": pdf_url,
            "pdf_url": pdf_url,
            "event_class": _classify(classification),
            "aircraft": _parse_aircraft(description),
            "registration": None,
            "date_of_occurrence": _parse_date(date_text),
            "location": description or None,
            "title": description,
            "superseded_codes": superseded_codes,
        })

    return rows


# ──────────────────────────────────────────────
# Download
# ──────────────────────────────────────────────

def download(client, pdf_url: str, dest) -> None:
    """
    GET pdf_url with a Referer header and write bytes to dest.
    Raises httpx.HTTPStatusError on non-2xx responses.
    """
    resp = client.get(pdf_url, headers={"Referer": REFERER})
    resp.raise_for_status()
    with open(dest, "wb") as fh:
        fh.write(resp.content)
