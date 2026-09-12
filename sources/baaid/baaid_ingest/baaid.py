# baaid_ingest/baaid.py
"""Bahamas AAID (baaid.org) scraper.

Source: https://www.baaid.org/accidents  (a Wix site)

The /accidents page is a single SERVER-RENDERED HTML page (no JS hydration
required for the data): a Wix rich-text "Aviation Occurrence Database" table.
Every report links to a PDF hosted at /_files/ugd/<prefix>_<hash>.pdf — the
PDFs carry a text layer (extractable with pdftotext).

Enumeration:
  We DO NOT call any Wix data API.  The full report list is present inline in
  the /accidents HTML as <a href=".../_files/ugd/PREFIX_HASH.pdf"> anchors,
  grouped under year headers and three sections (under-investigation,
  Accidents & Serious Incidents, Incidents & Short Investigations).  We walk
  the document in order, merge consecutive anchors that share the same PDF
  (Wix splits a single registration across multiple <a> spans, e.g. "N702"+"SV"
  => "N702SV"), and emit one row per UNIQUE PDF file-id.

case_id:
  INTRINSIC and stable: the Wix PDF file-id ("PREFIX_HASH"), normalised.  Most
  historical listing rows expose only a registration + PDF (no OCC number); the
  OCC number, where present, lives in the PDF text and is heterogeneous
  (OCC-YYYY/NNNN, OCC YYYY/NNNN, AO-YY-NNNNNN, plus typos), so it is NOT used as
  the primary key.  It is parsed at parse-time into the title/report_type.
  No encounter-order suffixes are ever added.
"""
import html as _html
import re

BASE = "https://www.baaid.org"
INDEX_URL = BASE + "/accidents"
REFERER = INDEX_URL
DELAY = 2.0

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
# Compiled regexes
# ──────────────────────────────────────────────

# A PDF anchor: capture the file-id (PREFIX_HASH) and the inner text (reg span).
_PDF_ANCHOR_RE = re.compile(
    r'<a\b[^>]*href="https://www\.baaid\.org/_files/ugd/'
    r'([0-9a-fA-F]+_[0-9a-fA-F]+)\.pdf"[^>]*>(.*?)</a>',
    re.DOTALL | re.IGNORECASE,
)

# Standalone OCC reference in listing text (rare) / PDF text.
_OCC_RE = re.compile(r"OCC[\s\-]*((?:19|20)\d{2})[\s/\-]+(\d{1,4})", re.IGNORECASE)
# Typo variant where year was written as 00YY (e.g. OCC-0026-0002 for 2026).
_OCC_TYPO_RE = re.compile(r"OCC[\s\-]*00(\d{2})[\s/\-]+(\d{1,4})", re.IGNORECASE)
# Older "Aviation Occurrence" id: AO-YY-NNNNNN.
_AO_RE = re.compile(r"\bAO[\s\-]*(\d{2})[\s\-]+(\d{4,6})\b", re.IGNORECASE)

_REG_RE = re.compile(r"^[A-Z0-9][A-Z0-9-]{2,9}$")


# ──────────────────────────────────────────────
# case_id helpers
# ──────────────────────────────────────────────

def make_case_id(file_id: str) -> str:
    """Build the intrinsic case_id from a Wix PDF file-id (PREFIX_HASH)."""
    return _normalize_case_id(file_id)


def _normalize_case_id(raw: str) -> str:
    """Normalise a raw id into a stable case_id.

    Accepts either a Wix file-id ('320f20_d8c2...') or an OCC string
    ('OCC-2024/0044').  Lowercases and collapses any run of non-alphanumeric
    characters to a single '-' (so the slash in OCC numbers is handled), then
    strips leading/trailing '-'.
    """
    s = (raw or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s


def normalize_occ(text: str) -> str | None:
    """Extract & normalise an OCC/AO occurrence number from arbitrary text.

    Returns canonical 'OCC-YYYY/NNNN' (or 'AO-YYYY/NNNNNN'), or None.
    """
    if not text:
        return None
    m = _OCC_RE.search(text)
    if m:
        return f"OCC-{m.group(1)}/{int(m.group(2)):04d}"
    m = _OCC_TYPO_RE.search(text)
    if m:
        return f"OCC-20{m.group(1)}/{int(m.group(2)):04d}"
    m = _AO_RE.search(text)
    if m:
        return f"AO-20{m.group(1)}/{int(m.group(2)):06d}"
    return None


# ──────────────────────────────────────────────
# Client factory
# ──────────────────────────────────────────────

def make_client():
    import httpx
    return httpx.Client(headers=HEADERS, follow_redirects=True, timeout=60.0)


# ──────────────────────────────────────────────
# Listing parsing
# ──────────────────────────────────────────────

def _clean_inner(s: str) -> str:
    s = _TAGSTRIP.sub("", s)
    s = _html.unescape(s)
    return s


_TAGSTRIP = re.compile(r"<[^>]+>")


def parse_listing(html: str) -> list[dict]:
    """Parse the /accidents HTML into one dict per UNIQUE PDF report.

    Each dict:
      case_id      str   intrinsic, from the Wix file-id (PREFIX_HASH)
      file_id      str   raw Wix file-id
      pdf_url      str   absolute PDF URL
      registration str|None   merged registration text from the listing
      date_of_occurrence str|None  best-effort year (YYYY) from the section header
      title        str   short human label
      event_class  str|None  listing event-type text when present
      occ_listing  str|None  OCC number if it appears inline in the listing

    Consecutive anchors that point at the SAME pdf are merged (Wix splits a
    registration across multiple spans).  De-duplicated by file_id; first
    occurrence wins for ordering/metadata.
    """
    # Tokenise: capture (file_id, inner, trailing-text-up-to-next-anchor).
    tokens = []  # list of (file_id, inner_text, trailing_text)
    last = 0
    spans = list(_PDF_ANCHOR_RE.finditer(html))
    for i, m in enumerate(spans):
        file_id = m.group(1).lower()
        inner = _clean_inner(m.group(2))
        nxt = spans[i + 1].start() if i + 1 < len(spans) else len(html)
        trailing_raw = html[m.end():nxt]
        trailing = _clean_inner(trailing_raw)
        tokens.append((file_id, inner, trailing))

    # Track current year via section/year markers found in trailing text.
    rows: list[dict] = []
    by_id: dict[str, dict] = {}
    current_year = None

    i = 0
    n = len(tokens)
    while i < n:
        file_id, inner, trailing = tokens[i]

        # Merge following tokens that share this file_id (split registration).
        reg = inner
        last_trailing = trailing
        j = i + 1
        while j < n and tokens[j][0] == file_id:
            reg += tokens[j][1]
            last_trailing = tokens[j][2]
            j += 1

        # Update current_year from the trailing text BEFORE this PDF's group
        # ended (a year header usually sits between report groups).
        yr = _scan_year(last_trailing)
        # registration cleanup
        reg_clean = _clean_registration(reg)
        occ = normalize_occ(inner + " " + last_trailing)
        event_class = _scan_event(last_trailing)

        if file_id not in by_id:
            row = {
                "case_id": make_case_id(file_id),
                "file_id": file_id,
                "pdf_url": f"{BASE}/_files/ugd/{file_id}.pdf",
                "registration": reg_clean,
                "date_of_occurrence": f"{current_year}" if current_year else None,
                "title": (reg_clean or occ or file_id),
                "event_class": event_class,
                "occ_listing": occ,
            }
            by_id[file_id] = row
            rows.append(row)

        # Advance year AFTER attributing this group, using the trailing text.
        if yr:
            current_year = yr
        i = j

    return rows


_YEAR_RE = re.compile(r"\b(19[5-9]\d|20[0-2]\d)\b")


def _scan_year(text: str) -> str | None:
    """Find a bare 4-digit year (handles Wix '2 0 2 3' spacing by squeezing)."""
    if not text:
        return None
    squeezed = re.sub(r"(?<=\d)\s+(?=\d)", "", text)
    m = _YEAR_RE.search(squeezed)
    return m.group(1) if m else None


_EVENT_HINTS = (
    "Near Miss", "Runway Excursion", "Aborted Takeoff", "Loss of Control",
    "System Component Failure", "Fuel", "Collision", "Forced Landing",
    "Hard Landing", "Gear", "Other",
)


def _scan_event(text: str) -> str | None:
    if not text:
        return None
    flat = re.sub(r"\s+", " ", text)
    for h in _EVENT_HINTS:
        if h.lower() in flat.lower():
            return h
    return None


def _clean_registration(reg: str) -> str | None:
    """Squeeze whitespace, keep a plausible registration token.

    Listing registration spans sometimes bleed event-type text; we keep the
    leading registration-shaped token only.  Authoritative reg is re-parsed
    from the PDF later, so a best-effort value is fine.
    """
    if not reg:
        return None
    flat = re.sub(r"\s+", "", reg)
    # take leading run of [A-Z0-9-]
    m = re.match(r"([A-Z0-9][A-Z0-9-]{1,15})", flat.upper())
    if not m:
        return None
    cand = m.group(1).rstrip("-")
    return cand or None


# ──────────────────────────────────────────────
# Download
# ──────────────────────────────────────────────

def download(client, pdf_url: str, dest) -> None:
    resp = client.get(pdf_url, headers={"Referer": REFERER})
    resp.raise_for_status()
    with open(dest, "wb") as fh:
        fh.write(resp.content)


# ── probable cause ────────────────────────────────────────────────────────────
#
# Bahrain AAIA reports head the section "Probable Cause" in mixed case, then
# give prose followed by "Contributing factors which resulted in ..." and a
# bulleted list. The section ends at the recommendations.
#
# Terminator vocabulary measured across all 222 PDFs on the host:
#
#     CONTRIBUTING FACTORS    11   <- part of the cause, NOT the end
#     SAFETY RECOMMENDATIONS   3
#     AAIA-SIB                 2   <- a document reference, furniture
#     RECOMMENDATION           1
#     CONCLUSIONS              1
#
# CONTRIBUTING FACTORS leads that list and is the one entry that must not be
# treated as a terminator: the contributing factors ARE the cause here, the
# same call Croatia needed for KONTRIBUTIVNI ČIMBENICI. An unmeasured
# vocabulary would have taken the most frequent following heading for the end
# of the section and truncated a fifth of the corpus at exactly the wrong line.
#
# probable_cause is what decides indexability: prod needs a quality score of
# 50, a narrative over 300 chars scores 30 and a cause over 100 scores 20, and
# factors_json, weather_summary and phase_of_flight are hardcoded null at
# projection.
_PC_HEADING_RE = re.compile(
    r"^[ \t\f]*(?:\d+(?:\.\d+)*[.)]?[ \t\f]*)?"
    r"(?:PROBABLE\s+CAUSES?|CAUSES?\s+OF\s+THE\s+(?:ACCIDENT|INCIDENT))"
    r"[ \t]*:?[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)
_PC_TERMINATOR_RE = re.compile(
    r"^[ \t\f]*(?:\d+(?:\.\d+)*[.)]?[ \t\f]*)?"
    r"(?:SAFETY\s+RECOMMENDATIONS?|RECOMMENDATIONS?|CONCLUSIONS?"
    r"|APPENDIC?E?S?|ANNEXE?S?)[ \t]*:?[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)
_PC_FURNITURE_RE = re.compile(
    r"^[ \t\f]*(?:AAIA[- ]SIB\b.*|Page\s+\d+\s+of\s+\d+"
    r"|[_\-\u2014]{10,}|\d+\s*\|\s*P\s*a\s*g\s*e)[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)
# Bullets arrive as whatever glyph the report's font mapped them to; the
# Wingdings bullet lands in the Private Use Area as U+F0B7.
_PC_BULLET_RE = re.compile(
    r"^[ \t\f]*[\u2022\u25aa\u25cf\u25a0\u00b7\u2013\u2014\-\*\uE000-\uF8FF]+[ \t\f]*",
    re.MULTILINE,
)

PROBABLE_CAUSE_MIN = 40
_PC_WINDOW = 6000  # chars; see the note in parse_probable_cause


def parse_probable_cause(text: str) -> str | None:
    """Return the Probable Cause section as one normalised string, or None."""
    if not text:
        return None
    m = _PC_HEADING_RE.search(text)
    if not m:
        return None
    # Bound the window before looking for the terminator. 13 of the 46 reports
    # that carry this heading have no following section heading at all, so an
    # unbounded capture runs to the end of the document: one produced 40,998
    # characters — the whole report filed as a probable cause. It would have
    # passed every length check downstream and read as nonsense on the page.
    #
    # 6000 is taken from the corpus: the 90th percentile here is 1,831, and the
    # longest genuine sections elsewhere are 3,707 (Philippines) and 2,227
    # (Croatia). Four Bahrain captures exceeded it, all four the heading
    # matching somewhere it should not.
    rest = text[m.end(): m.end() + _PC_WINDOW]
    end = _PC_TERMINATOR_RE.search(rest)
    body = rest[: end.start()] if end else rest

    body = _PC_FURNITURE_RE.sub("", body)
    body = _PC_BULLET_RE.sub("", body)

    out = []
    for ln in (l.strip() for l in body.splitlines()):
        if not ln:
            continue
        if out and not out[-1].endswith((".", ";", ":")):
            out[-1] = out[-1] + " " + ln
        else:
            out.append(ln)
    joined = re.sub(r"\s+", " ", " ".join(out)).strip()
    joined = re.sub(r"\s+\d+(?:\.\d+)*[.)]?\s*$", "", joined).strip()
    if end is None and len(joined) > PROBABLE_CAUSE_MIN:
        # No terminator: the window decided where this stopped, so cut back to
        # the last sentence rather than ending mid-clause.
        cut = joined.rfind(". ")
        if cut > PROBABLE_CAUSE_MIN:
            joined = joined[: cut + 1]
    return joined if len(joined) >= PROBABLE_CAUSE_MIN else None
