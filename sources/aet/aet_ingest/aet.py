# aet_ingest/aet.py
"""Luxembourg AET (Administration des enquêtes techniques) HTML scraper.

Source: https://aet.gouvernement.lu/fr/l-administration/aviation-civile.html
- ONE static, server-rendered HTML page (~169 KB) listing the AET's published
  reports under several headings:
    * "Évaluations préliminaires"        -> preliminary  (DROPPED)
    * "Enquêtes de sécurité ouvertes"    -> open         (DROPPED)
    * "Rapports finaux à destination du public"          -> KEPT
        - "Rapports publiés par l'AET ..."               -> final-aet
        - "Rapports publiés par les autorités étrangères -> final-foreign
          où l'AET a été associée à l'enquête"              (KEPT + FLAGGED)
    * "Rapports historiques ..."         -> historical   (KEPT)
- PDF hrefs appear in two equivalent shapes:
    //aet.gouvernement.lu/dam-assets/...                       (CDN path)
    http://aet.gouvernement.lu/content/dam/gouv2024_aet/...    (legacy alias)
  The legacy /content/dam/gouv2024_aet/ alias 301-redirects to the same file,
  but some legacy URLs return an empty body on GET while the /dam-assets/ twin
  serves bytes; the downloader therefore normalises content/dam -> dam-assets.

EXCLUSIONS
----------
* '/bulletin/' paths              -> preliminary bulletins, not final reports.
* preliminary / open sections     -> communiqués and preliminary reports.
* '/formulaires/' (ASR-*) forms   -> reporting forms, not reports.
Only the final (AET + foreign) and historical report PDFs are kept.

FOREIGN-AUTHORITY FLAG
----------------------
Docs under the explicit "autorités étrangères" sub-heading are reports issued
by other states' investigation bodies where the AET was only a party.  They are
KEPT but flagged: report_type carries a "foreign-authority" marker so the
prod-side dedup can act later.  case_id stays intrinsic and stable.

case_id model
-------------
    case_id = make_case_id(href)            # 'aet-<filename-slug>'
The PDF filename stem is the one INTRINSIC, stable identifier present for every
row.  No encounter-order suffix is ever appended; the same href always yields
the same case_id.  The Luxembourg / foreign registration mark (LX-..., OE-...,
OO-..., F-..., N-number, ...) is harvested from the text/filename when present.

Languages: MIXED (French / English / German).  Detected per-document from the
filename suffix (-fr/-en/-de) or a stopword text heuristic and stored per row.
"""
import html as _html
import re
import urllib.parse
from pathlib import Path

BASE = "https://aet.gouvernement.lu"
INDEX_URL = BASE + "/fr/l-administration/aviation-civile.html"
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

# Foreign-authority marker stored in report_type (see build()).
FOREIGN_AUTHORITY = "foreign-authority"

# ──────────────────────────────────────────────
# Section anchors (heading text -> kept/dropped)
# ──────────────────────────────────────────────
# The page has no machine-readable section ids, so sections are delimited by
# their (stable) French heading text.  Each PDF href is classified by the last
# anchor that precedes it.

_ANCHORS = [
    ("preliminary",   "Évaluations préliminaires"),
    ("open",          "Enquêtes de sécurité ouvertes"),
    ("final-aet",     "Rapports finaux à destination du public"),
    ("final-foreign", "publiés par les autorités étrangères"),
    ("historical",    "Rapports historiques"),
    ("other",         "Notification d"),
]

# Sections whose PDFs are kept.
_KEEP_SECTIONS = {"final-aet", "final-foreign", "historical"}
# Sections whose PDFs are foreign-authority issued (kept + flagged).
_FOREIGN_SECTIONS = {"final-foreign"}


# ──────────────────────────────────────────────
# Compiled regexes
# ──────────────────────────────────────────────

# Any aet.gouvernement.lu PDF href, protocol-relative (//) or absolute http(s).
_PDF_LINK_RE = re.compile(
    r'<a\b[^>]*\bhref="((?:https?:)?//aet\.gouvernement\.lu/[^"]+?\.pdf)"[^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_NONSLUG_RE = re.compile(r"[^a-z0-9]+")

# Registration marks: European hyphenated prefixes + US N-number.
_REG_RE = re.compile(
    r"\b((?:LX|OE|OO|F|D|G|HB|PH|EI|EC|SP|OY|SE|I)-[A-Z]{3,4}|N\d{1,5}[A-Z]{0,2})\b"
)

# Filename language suffix hints, e.g. 'oo-elf-communique-fr', 'aifn-...-en'.
_LANG_SUFFIX_RE = re.compile(r"(?:^|[-_])(fr|en|de)(?:[-_]|$)", re.IGNORECASE)
# Digit-glued trailing language code, e.g. 'BEA2020-0237en' / 'report2019de'.
_LANG_GLUED_RE = re.compile(r"\d(fr|en|de)$", re.IGNORECASE)


# ──────────────────────────────────────────────
# Client factory
# ──────────────────────────────────────────────

def make_client():
    """Return an httpx.Client configured with browser UA and cookie jar."""
    import httpx
    return httpx.Client(
        headers=HEADERS,
        follow_redirects=True,
        timeout=60.0,
    )


# ──────────────────────────────────────────────
# URL helpers
# ──────────────────────────────────────────────

def absolutize(href: str) -> str:
    """
    Turn a listing href into a downloadable absolute https URL.

    * protocol-relative '//aet...'          -> 'https://aet...'
    * legacy 'content/dam/gouv2024_aet/...'  -> 'dam-assets/...'  (the working
      CDN twin; the legacy alias can return an empty body on GET).
    """
    href = _html.unescape((href or "").strip())
    if href.startswith("//"):
        href = "https:" + href
    elif href.startswith("http://"):
        href = "https://" + href[len("http://"):]
    href = href.replace(
        "/content/dam/gouv2024_aet/", "/dam-assets/"
    )
    return href


# ──────────────────────────────────────────────
# slug / case_id helpers
# ──────────────────────────────────────────────

def _slugify(s: str) -> str:
    if not s:
        return ""
    return _NONSLUG_RE.sub("-", s.lower()).strip("-")


def make_case_id(href: str) -> str:
    """
    Build the INTRINSIC, stable case_id from a PDF href.

    The filename stem is the only identifier present for every listing row, so
    it is the primary key.  e.g.
        '.../CESSNA-C177-factual-report-FINAL.pdf' -> 'aet-cessna-c177-factual-report-final'
        '.../acc-ellx-b742-01-11-1992.pdf'         -> 'aet-acc-ellx-b742-01-11-1992'

    No encounter-order suffix is ever appended; the same href always yields the
    same case_id.
    """
    href = _html.unescape(href or "")
    name = href.split("/")[-1]
    name = re.sub(r"\.pdf$", "", name, flags=re.IGNORECASE)
    name = urllib.parse.unquote(name)
    slug = _slugify(name)
    return f"aet-{slug}" if slug else "aet"


def _filename_stem(href: str) -> str:
    href = _html.unescape(href or "")
    name = href.split("/")[-1]
    name = re.sub(r"\.pdf$", "", name, flags=re.IGNORECASE)
    return urllib.parse.unquote(name)


# ──────────────────────────────────────────────
# Language detection (per-document)
# ──────────────────────────────────────────────

# Stopword sets for the cheap text heuristic.
_FR_WORDS = {"le", "la", "les", "des", "une", "est", "aux", "que", "qui",
             "avec", "pour", "dans", "enquête", "accident", "aéronef",
             "survenu", "rapport"}
_DE_WORDS = {"der", "die", "das", "und", "mit", "ein", "eine", "nicht",
             "untersuchungsbericht", "unfall", "flugzeug", "wurde", "von"}
_EN_WORDS = {"the", "and", "was", "were", "aircraft", "report", "final",
             "accident", "incident", "investigation", "with", "from"}

_WORD_TOKEN_RE = re.compile(r"[A-Za-zÀ-ÿ]+")


def detect_lang(href: str, text: str | None = None) -> str:
    """
    Detect the per-document language ('fr' | 'en' | 'de').

    1. Filename suffix hint ('-en'/'-fr'/'-de', '-e-ref' counts as en) wins.
    2. Otherwise a stopword count over the report text decides.
    3. Default 'fr' (Luxembourg's primary administrative language).
    """
    stem = _filename_stem(href).lower()
    # explicit '-e-ref' style english marker
    if re.search(r"(?:^|[-_])e[-_]ref(?:[-_]|$)", stem):
        return "en"
    m = _LANG_SUFFIX_RE.search(stem)
    if m:
        return m.group(1).lower()
    m = _LANG_GLUED_RE.search(stem)
    if m:
        return m.group(1).lower()

    if text:
        toks = [t.lower() for t in _WORD_TOKEN_RE.findall(text[:4000])]
        if toks:
            counts = {
                "fr": sum(t in _FR_WORDS for t in toks),
                "en": sum(t in _EN_WORDS for t in toks),
                "de": sum(t in _DE_WORDS for t in toks),
            }
            best = max(counts, key=counts.get)
            if counts[best] > 0:
                return best
    return "fr"


# ──────────────────────────────────────────────
# Registration
# ──────────────────────────────────────────────

def find_registration(text: str | None) -> str | None:
    """Return the first aircraft registration mark found in text, or None."""
    if not text:
        return None
    m = _REG_RE.search(text)
    if not m:
        return None
    return m.group(1).upper()


def registration_from_filename(href: str) -> str | None:
    """Best-effort registration pulled from the filename stem (LX-... etc.)."""
    stem = _filename_stem(href).upper()
    m = _REG_RE.search(stem)
    return m.group(1).upper() if m else None


# ── cover-block field extraction ───────────────────────────────────
#
# AET reports do NOT share the uniform labelled cover block of some agencies;
# the title block carries the event inline (e.g. "... ON 18 JANUARY 2015",
# "Accident survenu le 6 novembre 2002 ...").  Extraction is best-effort and
# scoped to the cover block (text[:2500]); None when unsure.  Month maps cover
# English, French and German.

_HEAD_CHARS = 2500

_MONTHS = {
    # English
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
    # French
    "janvier": 1, "février": 2, "fevrier": 2, "mars": 3, "avril": 4, "mai": 5,
    "juin": 6, "juillet": 7, "août": 8, "aout": 8, "septembre": 9,
    "octobre": 10, "novembre": 11, "décembre": 12, "decembre": 12,
    # German
    "januar": 1, "februar": 2, "märz": 3, "maerz": 3, "april ": 4,
    "juni": 6, "juli": 7, "oktober": 10, "dezember": 12,
}

# Word-month date with ordinal-suffix tolerance and optional comma before year:
#   "18 JANUARY 2015", "6 novembre 2002", "1er novembre 1992",
#   "30. September 2015", "9TH NOVEMBER 2018".
_WORD_DATE_RE = re.compile(
    r"\b(\d{1,2})\s*(?:st|nd|rd|th|er|e|\.)?\.?\s+"   # day (+ optional ordinal)
    r"([A-Za-zÀ-ÿ]+)\.?\s*,?\s+"                      # month word (+ comma)
    r"(\d{4})\b",                                     # year
    re.IGNORECASE,
)

# Labels (multilingual) that anchor the event date.
_DATE_LABEL_RE = re.compile(
    r"(?:Date\s+of\s+(?:Accident|Occurrence|Incident|Event)"
    r"|survenu(?:e)?\s+le|du\s+|am\s+)",
    re.IGNORECASE,
)


def _iso(day: int, month: int, year: int) -> str | None:
    if not (1 <= month <= 12 and 1 <= day <= 31 and 1900 <= year <= 2100):
        return None
    return f"{year:04d}-{month:02d}-{day:02d}"


def _word_date_at(text: str) -> str | None:
    """Return the first word-month date in text as ISO, or None."""
    for m in _WORD_DATE_RE.finditer(text):
        month = _MONTHS.get(m.group(2).lower())
        if month is None:
            continue
        iso = _iso(int(m.group(1)), month, int(m.group(3)))
        if iso:
            return iso
    return None


def extract_event_date(text: str | None) -> str | None:
    """
    Extract the ISO 'YYYY-MM-DD' event date from the report cover block.

    Strategy: scan the cover/title block (text[:2500]).  Prefer the first
    word-month date that follows a date label ("Date of Accident", "survenu
    le", "du", "am"); otherwise fall back to the first word-month date in the
    head.  Tolerates ordinal suffixes (1st/1er/30.), an optional comma before
    the year and EN/FR/DE month names.  None when no parseable date is present.
    """
    if not text:
        return None
    head = text[:_HEAD_CHARS]

    label = _DATE_LABEL_RE.search(head)
    if label:
        window = head[label.end():label.end() + 120]
        iso = _word_date_at(window)
        if iso:
            return iso

    return _word_date_at(head)


# Possessive throughout. The original was
#     \s*[-:–—]?\s*(?:\n\s*)*([^\n]+)
# and \s already matches \n, so \s* and (?:\n\s*)* could divide the same
# run of newlines exponentially many ways. On input that never reaches
# [^\n]+ — a scanned PDF whose text layer is blank lines — match time
# doubled per newline: 24 newlines took 0.9s. Possessive quantifiers give
# nothing back, so there is no backtracking to blow up. Python 3.11+, which
# this tree already requires.
_VALUE_RE = re.compile(
    r"[^\S\n]*+(?:\n[^\S\n]*+)*+[-:–—]?[^\S\n]*+(?:\n[^\S\n]*+)*+([^\n]+)"
)


def _extract_labelled(text: str, label_re: re.Pattern) -> str | None:
    """
    Return the value following a cover-block label.

    The value may sit on the same line after a '-'/':' separator, or on the
    following non-blank line.  Best-effort; None when the label is absent.
    (AET reports rarely carry a uniform labelled cover block, so most calls
    legitimately return None.)
    """
    if not text:
        return None
    head = text[:_HEAD_CHARS]
    m = label_re.search(head)
    if not m:
        return None
    rest = head[m.end():]
    vm = _VALUE_RE.match(rest)
    if not vm:
        return None
    val = vm.group(1)
    val = re.sub(r"^[\s\-:–—]+", "", val)
    val = _WS_RE.sub(" ", val).strip(" -:–—\t")
    return val or None


_AIRCRAFT_LABEL_RE = re.compile(
    r"Aircraft\s+(?:Model|Make|Type)\s*/?\s*(?:Type)?",
    re.IGNORECASE,
)
_LOCATION_LABEL_RE = re.compile(
    r"(?:Place\s+of\s+(?:Accident|Occurrence|Incident)|Lieu\s+de\s+l['’]?\w+)"
    r"\s*/?\s*(?:Region)?",
    re.IGNORECASE,
)
_OPERATOR_LABEL_RE = re.compile(
    r"(?:Registered\s+Owner\s*/?\s*Operator|Name\s+of\s+Operator|Operator"
    r"|Exploitant)",
    re.IGNORECASE,
)


def extract_aircraft(text: str | None) -> str | None:
    """Aircraft make/model from a labelled cover block, when present."""
    return _extract_labelled(text or "", _AIRCRAFT_LABEL_RE)


def extract_location(text: str | None) -> str | None:
    """Event location from a labelled cover block, when present."""
    return _extract_labelled(text or "", _LOCATION_LABEL_RE)


def extract_operator(text: str | None) -> str | None:
    """Operator from a labelled cover block, when present."""
    return _extract_labelled(text or "", _OPERATOR_LABEL_RE)


def date_from_filename(href: str) -> str | None:
    """
    Best-effort *occurrence* date from the filename.  AET filenames encode the
    genuine occurrence date as 'DD-MM-YYYY' / 'DD.MM.YYYY'
    (acc-ellx-b742-01-11-1992).  Returns ISO or None.

    NOTE: a leading 'YYYYMMDD-' prefix (e.g. 20150801-...-embraer-145) is the
    report PUBLICATION date in this corpus, NOT the occurrence date, so it is
    deliberately NOT recognised here.
    """
    stem = _filename_stem(href)
    # DD-MM-YYYY / DD.MM.YYYY (the genuine occurrence date)
    m = re.search(r"\b(\d{2})[-.](\d{2})[-.](\d{4})\b", stem)
    if m:
        iso = _iso(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if iso:
            return iso
    return None


# ──────────────────────────────────────────────
# Discovery
# ──────────────────────────────────────────────

def _section_at(offset: int, anchor_offsets: list[tuple[str, int]]) -> str:
    """Return the name of the last anchor section that precedes `offset`."""
    cur = "pre"
    for name, start in anchor_offsets:
        if start >= 0 and offset >= start:
            cur = name
        elif start >= 0 and offset < start:
            break
    return cur


def _anchor_offsets(index_html: str) -> list[tuple[str, int]]:
    return [(name, index_html.find(txt)) for name, txt in _ANCHORS]


def parse_listing(index_html: str) -> list[dict]:
    """
    Parse the AET aviation-civile index page → list of kept report dicts.

    Each dict has:
      case_id   str   intrinsic 'aet-<filename-slug>' from the PDF filename
      pdf_url   str   absolute https URL (content/dam normalised to dam-assets)
      title     str   cleaned link text
      section   str   'final-aet' | 'final-foreign' | 'historical'
      foreign   bool  True when issued by a foreign authority (kept + flagged)
      lang      str   per-document language ('fr' | 'en' | 'de'), filename-based

    DROPPED: preliminary / open sections, '/bulletin/' paths, '/formulaires/'
    forms, and anything outside the kept sections.  Order preserved;
    de-duplicated by case_id.
    """
    anchors = _anchor_offsets(index_html)
    seen: set[str] = set()
    rows: list[dict] = []
    for m in _PDF_LINK_RE.finditer(index_html):
        raw_href = _html.unescape(m.group(1).strip())
        section = _section_at(m.start(), anchors)
        if section not in _KEEP_SECTIONS:
            continue
        # belt-and-suspenders path exclusions
        low = raw_href.lower()
        if "/bulletin/" in low or "/formulaires/" in low:
            continue

        case_id = make_case_id(raw_href)
        if case_id in seen:
            continue
        seen.add(case_id)

        title = _WS_RE.sub(" ", _html.unescape(_TAG_RE.sub("", m.group(2)))).strip()
        if not title:
            # icon-only / empty anchor: fall back to the filename stem
            title = _filename_stem(raw_href)
        pdf_url = absolutize(raw_href)
        rows.append({
            "case_id": case_id,
            "pdf_url": pdf_url,
            "title": title,
            "section": section,
            "foreign": section in _FOREIGN_SECTIONS,
            "lang": detect_lang(raw_href),
        })
    return rows


# ──────────────────────────────────────────────
# Download
# ──────────────────────────────────────────────

def download(client, pdf_url: str, dest: str | Path) -> None:
    """
    GET pdf_url with Referer header and write bytes to dest.

    The URL is normalised (content/dam -> dam-assets, https) and the path
    component percent-encoded so hrefs containing spaces resolve correctly.
    Raises httpx.HTTPStatusError on non-2xx responses.
    """
    url = absolutize(pdf_url)
    parts = urllib.parse.urlsplit(url)
    safe_path = urllib.parse.quote(parts.path, safe="/%")
    encoded = urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, safe_path, parts.query, parts.fragment)
    )
    resp = client.get(encoded, headers={"Referer": REFERER})
    resp.raise_for_status()
    with open(dest, "wb") as fh:
        fh.write(resp.content)
