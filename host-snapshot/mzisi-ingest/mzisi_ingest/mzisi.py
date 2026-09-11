# mzisi_ingest/mzisi.py
"""MzI (Slovenia) HTML scraper for the aviation accident-investigation service.

Source: gov.si — Ministry of Infrastructure and Energy, Služba za preiskovanje
letalskih nesreč in incidentov (aviation accident/incident investigation service).

The whole report archive lives on ONE server-rendered page that links directly
to PDF assets under /assets/ministrstva/MzI/porocila-o-letalskih-nesrecah/YYYY/.

Report-document types in the archive (by filename keyword):
  • Koncno-porocilo / KONCNO POROCILO / Koncna-porocila  → FINAL report  (keep)
  • Povzetek(-koncnega)                                   → SUMMARY of final (keep)
  • Uvodno-porocilo                                        → PRELIMINARY    (skip)
  • Obvestilo / OBVEST                                     → closure notice (skip)

We keep only FINAL reports + summaries; those carry the narrative.

case_id:
  The URL carries no official case number, so the discover-time staging key is
  derived from the asset path (year + filename slug):  mzisi-YYYY-<slug>.
  The official document number — shape '37200-N/YYYY[-2430-NN]' (with a SLASH) —
  lives INSIDE the PDF text and is extracted at parse time into source_event_id.
  source_event_id stays RAW (slash preserved); the prod projector slugifies it
  for the URL but keys the article-join on the raw number.
"""
import html as _html
import re
from pathlib import Path

BASE = "https://www.gov.si"
INDEX_URL = (
    BASE
    + "/drzavni-organi/ministrstva/ministrstvo-za-infrastrukturo-in-energetiko/"
    "o-ministrstvu/sluzbe-za-preiskovanje-letalskih-pomorskih-in-zelezniskih-"
    "nesrec-in-incidentov/sluzba-za-preiskovanje-letalskih-nesrec-in-incidentov/"
)
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
# Compiled regexes
# ──────────────────────────────────────────────

# Any PDF asset under the aviation-reports tree, with the year captured.
_ASSET_PDF_RE = re.compile(
    r'href="(/assets/ministrstva/MzI/porocila-o-letalskih-nesrecah/'
    r'(\d{4})/[^"]+?\.pdf)"',
    re.IGNORECASE,
)

# FINAL report / summary keywords (final narrative carriers).
_FINAL_RE = re.compile(r"(koncn|kon[čc]n|povzetek)", re.IGNORECASE)
# Preliminary / closure-notice keywords (skip).
_SKIP_RE = re.compile(r"(uvodno|obvest)", re.IGNORECASE)

# Official document number inside the PDF text: 3720X-N/YYYY[-2430-NN]
# (keeps the slash; optional trailing -NNNN-NN segments).
_CASE_NO_RE = re.compile(r"\b(3720\d-\d+/\d{4}(?:-\d+){0,2})\b")

# Slovenian/foreign registration mark in a filename or text.
_REG_RE = re.compile(r"\b([A-Z0-9]{1,2}-[A-Z0-9]{3,5})\b")

# Whitespace-collapse helpers for case-number normalisation.
_WS = re.compile(r"\s+")
_NONSLUG = re.compile(r"[^a-z0-9]+")


# ──────────────────────────────────────────────
# Client factory
# ──────────────────────────────────────────────

def make_client():
    """Return an httpx.Client configured with browser UA + Referer."""
    import httpx
    return httpx.Client(headers=HEADERS, follow_redirects=True, timeout=60.0)


# ──────────────────────────────────────────────
# case_id / case-number helpers
# ──────────────────────────────────────────────

def _normalize_case_id(s: str | None) -> str | None:
    """
    Normalise an official MzI document number.

    Collapses whitespace and removes spaces immediately around '-' and '/'
    separators so e.g. '37200 - 6 / 2016' → '37200-6/2016'.  The slash is
    PRESERVED.  Returns None for falsy input.
    """
    if not s:
        return None
    s = _WS.sub(" ", str(s)).strip()
    # remove spaces around '-' and '/'
    s = re.sub(r"\s*([-/])\s*", r"\1", s)
    return s or None


def extract_case_no(text: str | None) -> str | None:
    """
    Pull the official document number (e.g. '37200-6/2016' or
    '37201-1/2025-2430-58') out of the extracted PDF text, normalised.

    Returns None when no such number is present (e.g. summary-only PDFs).
    """
    if not text:
        return None
    m = _CASE_NO_RE.search(text)
    if not m:
        return None
    return _normalize_case_id(m.group(1))


def make_case_id(asset_path: str, year: str) -> str:
    """
    Build the discover-time staging case_id from the asset path.

    Shape: 'mzisi-YYYY-<filename-slug>'.  The filename slug is the PDF basename
    (sans extension) lowercased with non-[a-z0-9] runs collapsed to '-'.
    Stable and unique per asset, so it is a safe PRIMARY KEY.
    """
    name = asset_path.rsplit("/", 1)[-1]
    name = re.sub(r"\.pdf$", "", name, flags=re.IGNORECASE)
    slug = _NONSLUG.sub("-", name.lower()).strip("-")
    return f"mzisi-{year}-{slug}"


def _guess_registration(asset_path: str) -> str | None:
    """Best-effort registration from the filename (S5-XXX, OE-XXX, D-XXXX…)."""
    name = asset_path.rsplit("/", 1)[-1]
    m = _REG_RE.search(name.upper())
    return m.group(1) if m else None


# ──────────────────────────────────────────────
# Listing parser
# ──────────────────────────────────────────────

def parse_index(html: str) -> list[dict]:
    """
    Parse the MzI aviation-reports index page → list of FINAL-report dicts.

    Keeps only final reports / summaries (koncn|povzetek), excluding
    preliminary (uvodno) reports and closure notices (obvestilo).

    Each dict:
      case_id        str   staging key 'mzisi-YYYY-<slug>'
      pdf_url        str   absolute https URL
      year           str   'YYYY'
      registration   str|None  best-effort from filename
      title          str   PDF basename (human-ish)
      report_type    str   'Final report' | 'Summary'

    De-duplicates on pdf_url, preserves first-seen order.
    """
    rows: list[dict] = []
    seen: set[str] = set()
    for m in _ASSET_PDF_RE.finditer(html):
        path = _html.unescape(m.group(1))
        year = m.group(2)
        fname = path.rsplit("/", 1)[-1]

        # Final-report filter
        if _SKIP_RE.search(fname):
            continue
        if not _FINAL_RE.search(fname):
            continue

        pdf_url = BASE + path
        if pdf_url in seen:
            continue
        seen.add(pdf_url)

        report_type = "Summary" if re.search(r"povzetek", fname, re.IGNORECASE) else "Final report"

        rows.append({
            "case_id": make_case_id(path, year),
            "pdf_url": pdf_url,
            "year": year,
            "registration": _guess_registration(path),
            "title": re.sub(r"\.pdf$", "", fname, flags=re.IGNORECASE),
            "report_type": report_type,
        })
    return rows


# ──────────────────────────────────────────────
# Language heuristic
# ──────────────────────────────────────────────

_SL_TOKENS = re.compile(
    r"(kon[čc]no poro|nesre|preiska|zrakoplov|letalsk|povzetek|dejstva)",
    re.IGNORECASE,
)
_EN_TOKENS = re.compile(
    r"\b(final report|the aircraft|investigation|conclusions|probable cause|"
    r"the accident|runway)\b",
    re.IGNORECASE,
)


def detect_lang(text: str | None) -> str:
    """
    Heuristic language tag for an extracted narrative: 'sl' (default) or 'en'.

    MzI reports are predominantly Slovenian; a minority are English.  We tag
    'en' only when English markers clearly outweigh Slovenian ones.
    """
    if not text:
        return "sl"
    sl = len(_SL_TOKENS.findall(text))
    en = len(_EN_TOKENS.findall(text))
    return "en" if en > sl else "sl"


# ──────────────────────────────────────────────
# Event-date / location / aircraft extraction (from PDF text)
# ──────────────────────────────────────────────
#
# The gov.si index is a flat PDF-link list with NO structured row fields, so
# event_date / location / aircraft must come from the PDF TEXT.  Slovenian MzI
# reports carry the event details in the cover/title block:
#
#     Številka:   37200-6/2016-2430-39
#     Datum:      25. 1. 2020          <- PUBLICATION date (NOT the event date)
#     ...
#     v bližini letališča BOVEC – LJBO
#     1. septembra 2016                 <- EVENT date (word-month, declined)
#
# The publication "Datum:" line carries a DIFFERENT (later) year than the event;
# the staging case_id year ('mzisi-YYYY-…', derived from the asset folder) IS the
# event year, so we disambiguate by preferring the date whose year matches it.

# Slovenian month names → month number.  Declined/inflected forms (genitive
# "januarja", locative "maju", etc.) all share the listed stems, so we match by
# stem prefix.  Order longest-first where stems overlap (avgust before … none here).
_SL_MONTH_STEMS = [
    ("januar", 1), ("februar", 2), ("marec", 3), ("marc", 3), ("april", 4),
    ("maj", 5), ("junij", 6), ("julij", 7), ("avgust", 8),
    ("september", 9), ("septemb", 9), ("oktober", 10), ("oktob", 10),
    ("november", 11), ("novemb", 11), ("december", 12), ("decemb", 12),
]

# Numeric date: DD.MM.YYYY or "DD. MM. YYYY" (spaces around dots tolerated).
_NUM_DATE_RE = re.compile(r"\b(\d{1,2})\s*\.\s*(\d{1,2})\s*\.\s*(\d{4})\b")

# Word-month date: "1. septembra 2016", "03. MAJA 2008", "11. maj 2024".
_WORD_DATE_RE = re.compile(
    r"\b(\d{1,2})\s*\.\s*([A-Za-zČčŠšŽžĆćĐđ]+)\s+(\d{4})\b"
)


def _event_year_from_case_id(case_id: str | None) -> int | None:
    """Pull the event year out of a staging case_id 'mzisi-YYYY-…'."""
    if not case_id:
        return None
    m = re.match(r"mzisi-(\d{4})-", case_id)
    return int(m.group(1)) if m else None


def _month_from_word(word: str) -> int | None:
    w = word.lower()
    for stem, num in _SL_MONTH_STEMS:
        if w.startswith(stem):
            return num
    return None


def _iso(day: int, month: int, year: int) -> str | None:
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    return f"{year:04d}-{month:02d}-{day:02d}"


# 'Datum:' label marks the PUBLICATION date — the date that immediately follows
# it (same or next line) is NOT the event date and must be excluded.
_DATUM_LABEL_RE = re.compile(r"Datum\s*:", re.IGNORECASE)


def _iter_dates(text: str):
    """Yield (start_offset, year, iso_string) for every date in text, in order."""
    out = []
    for m in _NUM_DATE_RE.finditer(text):
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        iso = _iso(d, mo, y)
        if iso:
            out.append((m.start(), y, iso))
    for m in _WORD_DATE_RE.finditer(text):
        d, mo, y = int(m.group(1)), _month_from_word(m.group(2)), int(m.group(3))
        if mo is None:
            continue
        iso = _iso(d, mo, y)
        if iso:
            out.append((m.start(), y, iso))
    out.sort(key=lambda t: t[0])
    return out


def _publication_offsets(text: str) -> set[int]:
    """
    Offsets of dates that are the PUBLICATION date — i.e. the FIRST date whose
    start falls after a 'Datum:' label (within a short window).  Excluded from
    event-date selection.
    """
    pub = set()
    dates = _iter_dates(text)
    for lm in _DATUM_LABEL_RE.finditer(text):
        end = lm.end()
        for off, _y, _iso in dates:
            # next date after the label, within ~40 chars (label + a blank line)
            if off >= end and off - end <= 40:
                pub.add(off)
                break
    return pub


def extract_event_date(text: str | None, case_id: str | None = None) -> str | None:
    """
    Extract the ISO 'YYYY-MM-DD' event date from the PDF text.

    Strategy: collect every numeric ('DD.MM.YYYY') and word-month
    ('1. septembra 2016') date in the cover block, EXCLUDING the date that
    follows a 'Datum:' label (the publication date).  When the staging case_id
    supplies an event year, prefer the first remaining date whose year matches it
    (excludes review/revision dates too).  Otherwise fall back to the first
    remaining date, then — if everything was excluded — the first date seen.

    Returns None when no parseable date is present (older scanned reports).
    """
    if not text:
        return None
    # Scan only the cover/title block — the event date lives near the top, and
    # later body text is full of unrelated dates (meteorology, licences, etc.).
    head = text[:2500]
    dates = _iter_dates(head)
    if not dates:
        return None

    pub = _publication_offsets(head)
    candidates = [(y, iso) for off, y, iso in dates if off not in pub]
    if not candidates:
        candidates = [(y, iso) for _off, y, iso in dates]

    want = _event_year_from_case_id(case_id)
    if want is not None:
        for y, iso in candidates:
            if y == want:
                return iso
    return candidates[0][1]


# Location: a place line in the cover block.  Captures the text after the
# locative connector up to a line break / comma / date.
_LOCATION_RE = re.compile(
    r"\b(?:v\s+bli[žz]ini|na\s+letali[šs][čc]u|na\s+vzleti[šs][čc]u|"
    r"v\s+kraju|na\s+obmo[čc]ju|v\s+vasi)\s+"
    r"([^\n,;]+?)(?:\s*[\n,;]|\s+\d{1,2}\s*\.|$)",
    re.IGNORECASE,
)


def extract_location(text: str | None) -> str | None:
    """
    Best-effort event location from the PDF cover block (e.g. 'BOVEC – LJBO',
    'Mengeš', 'letališče Celje').  Returns None when no place line is found.
    """
    if not text:
        return None
    head = text[:2500]
    m = _LOCATION_RE.search(head)
    if not m:
        return None
    loc = _WS.sub(" ", m.group(1)).strip(" .–-")
    return loc or None


# Aircraft: a make-model line in the cover block.  Captures the text after a
# 'letala/zrakoplova' connector up to a comma / 'reg' / line break.
_AIRCRAFT_RE = re.compile(
    r"\b(?:motornega\s+letala|jadralnega\s+zmaja|letala|zrakoplova|"
    r"helikopterja|tipa)\s+"
    r"([A-Z0-9][^\n,;]*?)"
    r"(?:\s*,|\s+reg\b|\s+z\s+reg|\s+registr|\s*[\n;]|$)",
    re.IGNORECASE,
)


def extract_aircraft(text: str | None) -> str | None:
    """
    Best-effort aircraft make-model from the PDF cover block (e.g.
    'PIPER PA-28-161', 'DR 400/180', 'Aquila AT01').  Returns None when absent.
    """
    if not text:
        return None
    head = text[:2500]
    m = _AIRCRAFT_RE.search(head)
    if not m:
        return None
    ac = _WS.sub(" ", m.group(1)).strip(" .–-")
    # Guard against runaway captures (a sentence rather than a model name) and
    # against date/time text leaking in when the cover lacks a real model line
    # (e.g. 'letalo S5-PIJ ... 8.7.2010 ob 20:00').
    if not ac or len(ac) > 60:
        return None
    if re.search(r"\d{1,2}\s*\.\s*\d{1,2}\s*\.\s*\d{2,4}", ac) or re.search(
        r"\bob\b|\bpo\s+lokal", ac, re.IGNORECASE
    ):
        return None
    return ac


# Operator: best-effort 'Operator: X' field (rare in MzI reports).
_OPERATOR_RE = re.compile(r"\bOperator\s*:?\s*([^\n,;]+)", re.IGNORECASE)


def extract_operator(text: str | None) -> str | None:
    """Best-effort operator from an 'Operator:' field; None when absent."""
    if not text:
        return None
    head = text[:6000]
    m = _OPERATOR_RE.search(head)
    if not m:
        return None
    op = _WS.sub(" ", m.group(1)).strip(" .–-")
    if not op or len(op) > 80:
        return None
    return op


# ──────────────────────────────────────────────
# Download
# ──────────────────────────────────────────────

def download(client, pdf_url: str, dest: str | Path) -> None:
    """
    GET pdf_url with a directory Referer and write bytes to dest.

    gov.si serves the assets without hotlink protection, but we send a Referer
    pointing at the asset directory for politeness/robustness.
    Raises httpx.HTTPStatusError on non-2xx responses.
    """
    parent = pdf_url.rsplit("/", 1)[0] + "/"
    resp = client.get(pdf_url, headers={"Referer": parent})
    resp.raise_for_status()
    with open(dest, "wb") as fh:
        fh.write(resp.content)
