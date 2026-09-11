# beacg_ingest/beacg.py
"""BEA Congo (Bureau d'Enquetes et d'Analyses, Congo-Brazzaville) HTML scraper.

Source: https://www.bea.cg/enquete-finalisee-et-rapports/
- ONE static, server-rendered WordPress page (French) listing every
  finalised/incident investigation as rows of a single HTML <table>.
- Each <tr> is one occurrence with structured cells (Reference, Aeronef,
  Date de l'evenement, Localisation, Categorie, ... Statut) and up to three
  document columns: "Declaration intermediaire", "Compte rendu prelim" and
  "Rapport" (the FINAL report).  Only the final-report PDF (last column) is
  the canonical document for an occurrence; the interim documents are ignored.
- ~19 rows spanning 2007-2023.  The final PDFs are absolute hrefs under
  /wp-content/uploads/.  Reports 2007-2011 are scanned image PDFs (they
  extract below the narrative floor and are skipped, like gcaagy 'scanned');
  the 2014+ born-digital French reports (46K-95K chars) build.

case_id model
-------------
The final-report PDF filename is the one INTRINSIC, stable identifier present
for every buildable row, so:

    case_id = make_case_id(pdf_href)        # 'beacg-<filename-slug>'

The official file reference ("BEA-NN-YYYY" or "No NN-YYYY") appears in the
listing's Reference cell (and in the PDF text) only for some rows.  When
present it is normalised by _normalize_case_id() and stored in column
report_url, which the build step surfaces as report_type.  case_id itself
stays intrinsic and stable; no encounter-order suffixes are ever appended.
"""
import html as _html
import re
import urllib.parse
from pathlib import Path

BASE = "https://www.bea.cg"
INDEX_URL = BASE + "/enquete-finalisee-et-rapports/"
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

# --------------------------------------------------
# Compiled regexes
# --------------------------------------------------

# Whole table rows.
_ROW_RE = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
# Cells (header <th> or data <td>) with their class attribute.
_CELL_RE = re.compile(
    r"<t[hd]\b[^>]*\bclass=\"(column-\d+)\"[^>]*>(.*?)</t[hd]>",
    re.IGNORECASE | re.DOTALL,
)
# First absolute .pdf href inside a cell.
_PDF_HREF_RE = re.compile(
    r'href="(https?://[^"]+?\.pdf)"',
    re.IGNORECASE,
)

# Inner tags stripped from cell text.
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_NONSLUG_RE = re.compile(r"[^a-z0-9]+")

# Official BEA file reference: "BEA-03-2023", "BEA 03 2023", or the legacy
# "No 01-2018" / "No01-2018" forms (the page uses the U+2116 NUMERO SIGN).
_BEA_REF_RE = re.compile(
    r"(?:BEA[\s\-]*(\d{1,2})[\s\-]+(\d{4}))"
    r"|(?:(?:N[oº°]\.?|№)\s*(\d{1,2})[\s\-]+(\d{4}))",
    re.IGNORECASE,
)

# Aircraft registration mark: Congo TN-XXX, plus common foreign forms
# (5N-XXX Nigeria, etc.) and bare N-numbers.
_REG_RE = re.compile(
    r"\b(TN-?[A-Z]{2,4}|[0-9][A-Z]-?[A-Z]{2,4}|N\d{2,5}[A-Z]{0,2})\b"
)


# --------------------------------------------------
# Client factory
# --------------------------------------------------

def make_client():
    """Return an httpx.Client configured with browser UA and cookie jar."""
    import httpx
    return httpx.Client(
        headers=HEADERS,
        follow_redirects=True,
        timeout=60.0,
    )


# --------------------------------------------------
# slug / case_id helpers
# --------------------------------------------------

def _slugify(s: str) -> str:
    if not s:
        return ""
    return _NONSLUG_RE.sub("-", s.lower()).strip("-")


def make_case_id(href: str) -> str:
    """
    Build the INTRINSIC, stable case_id from a final-report PDF href.

    The filename stem is the only identifier present for every buildable row,
    so it is the primary key.  e.g.
        '.../Rapport-final-_-BEA-03-2023_INCID-_Boeing-737-36N-TN-AKC-1.pdf'
            -> 'beacg-rapport-final-bea-03-2023-incid-boeing-737-36n-tn-akc-1'
        '.../Rapport-final-n%C2%B001-2018-du-Skyranger-Swift-le-02.09.18.pdf'
            -> 'beacg-rapport-final-n-01-2018-du-skyranger-swift-le-02-09-18'

    No encounter-order suffix is ever appended; the same href always yields the
    same case_id.
    """
    href = _html.unescape(href or "")
    name = href.split("/")[-1]
    name = re.sub(r"\.pdf$", "", name, flags=re.IGNORECASE)
    name = urllib.parse.unquote(name)
    slug = _slugify(name)
    return f"beacg-{slug}" if slug else "beacg"


def _normalize_case_id(raw: str) -> str | None:
    """
    Normalise an official BEA file reference to a canonical slug.

        'BEA-03-2023'   -> 'bea-03-2023'
        'BEA 02 2023'   -> 'bea-02-2023'
        'No01-2018'     -> 'bea-01-2018'
        '№01-2018' -> 'bea-01-2018'
    Returns None when no recognisable reference is present.
    """
    if not raw:
        return None
    m = _BEA_REF_RE.search(raw)
    if not m:
        return None
    if m.group(1) is not None:
        num, year = m.group(1), m.group(2)
    else:
        num, year = m.group(3), m.group(4)
    if not (num and year):
        return None
    return f"bea-{int(num):02d}-{year}"


def find_bea_ref(text: str) -> str | None:
    """Return the normalised BEA file reference found in text, or None."""
    return _normalize_case_id(text or "")


def find_registration(text: str) -> str | None:
    """Return the first aircraft registration mark found in text, or None."""
    if not text:
        return None
    m = _REG_RE.search(text)
    if not m:
        return None
    reg = m.group(1).upper()
    # canonicalise TNXXX -> TN-XXX
    if reg.startswith("TN") and not reg.startswith("TN-"):
        reg = "TN-" + reg[2:]
    return reg


# -- cover-block field extraction -----------------------------------
#
# Born-digital BEA reports open with a uniform French prose cover block, e.g.
#
#     Incident survenu le 17 decembre 2023
#     a Brazzaville
#     a l'aeronef Boeing B737-36N
#     immatricule TN-AKC
#     exploite par Africa Airlines
#
# Some reports instead use labelled cover lines ("Date de l'accident",
# "Lieu", "Aeronef", "Exploitant").  All extraction is best-effort and scoped
# to the cover block (text[:2500]); None when the field is absent.  In
# practice the structured listing table supplies these fields directly, so
# these extractors act as a fallback.

_HEAD_CHARS = 2500

# French month names -> month number (accent-insensitive; callers strip
# accents before lookup).
_MONTHS = {
    "janvier": 1, "fevrier": 2, "mars": 3, "avril": 4, "mai": 5, "juin": 6,
    "juillet": 7, "aout": 8, "septembre": 9, "octobre": 10, "novembre": 11,
    "decembre": 12,
}

# French word-month date: "17 decembre 2023", "2 septembre 2018".
# (accents already stripped by _deaccent before matching)
_WORD_DATE_RE = re.compile(
    r"\b(\d{1,2})\s*(?:er)?\s+"          # day (+ optional 'er' ordinal)
    r"([a-z]+)\s+"                       # month word
    r"(\d{4})\b",
    re.IGNORECASE,
)

# Numeric dd/mm/yyyy (or dd.mm.yyyy / dd-mm-yyyy).
_NUM_DATE_RE = re.compile(
    r"\b(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4})\b"
)

# Labels that anchor an event date in labelled cover blocks.
_DATE_LABEL_RE = re.compile(
    r"Date\s+de\s+l[’']?\s*(?:accident|evenement|incident|occurrence)",
    re.IGNORECASE,
)
# Prose anchor: "... survenu(e) le <date>".
_SURVENU_RE = re.compile(r"survenue?\s+le\b", re.IGNORECASE)


_DEACCENT = {
    "é": "e", "è": "e", "ê": "e", "ë": "e",
    "à": "a", "â": "a", "ä": "a",
    "ô": "o", "ö": "o",
    "û": "u", "ü": "u", "ù": "u",
    "î": "i", "ï": "i",
    "ç": "c",
    "’": "'",
}
_DEACCENT_TABLE = str.maketrans(_DEACCENT)


def _deaccent(s: str) -> str:
    if not s:
        return s
    return s.translate(_DEACCENT_TABLE)


def _iso(day: int, month: int, year: int) -> str | None:
    if not (1 <= month <= 12 and 1 <= day <= 31 and 1900 <= year <= 2100):
        return None
    return f"{year:04d}-{month:02d}-{day:02d}"


def _word_date_at(text: str) -> str | None:
    """Return the first French word-month date in text as ISO, or None."""
    for m in _WORD_DATE_RE.finditer(text):
        month = _MONTHS.get(m.group(2).lower())
        if month is None:
            continue
        iso = _iso(int(m.group(1)), month, int(m.group(3)))
        if iso:
            return iso
    return None


def _num_date_at(text: str) -> str | None:
    """Return the first dd/mm/yyyy numeric date in text as ISO, or None."""
    m = _NUM_DATE_RE.search(text)
    if not m:
        return None
    return _iso(int(m.group(1)), int(m.group(2)), int(m.group(3)))


def extract_event_date(text: str | None) -> str | None:
    """
    Extract the ISO 'YYYY-MM-DD' event date from a report cover block or a
    listing date cell.

    Strategy (best-effort, scoped to text[:2500]):
      1. a French word-month date following a "survenu le" prose anchor;
      2. a French word-month date following a "Date de l'accident/evenement"
         label;
      3. the first French word-month date in the head;
      4. a numeric dd/mm/yyyy date (e.g. straight from the listing cell).

    Tolerates French accents and case.  Returns None when nothing parseable
    is present (older scanned reports).
    """
    if not text:
        return None
    head = _deaccent(text[:_HEAD_CHARS])

    anchor = _SURVENU_RE.search(head)
    if anchor:
        window = head[anchor.end():anchor.end() + 60]
        iso = _word_date_at(window)
        if iso:
            return iso

    label = _DATE_LABEL_RE.search(head)
    if label:
        window = head[label.end():label.end() + 120]
        iso = _word_date_at(window) or _num_date_at(window)
        if iso:
            return iso

    iso = _word_date_at(head)
    if iso:
        return iso
    return _num_date_at(head)


def _extract_labelled(text: str, label_re: re.Pattern) -> str | None:
    """
    Return the value following a labelled cover-block field.

    The value may sit on the same line after a '-'/':' separator, or on the
    following non-blank line.  Captures a single line, trimmed of separators
    and surrounding whitespace.  None when the label is absent or empty.
    """
    if not text:
        return None
    head = text[:_HEAD_CHARS]
    m = label_re.search(head)
    if not m:
        return None
    rest = head[m.end():]
    vm = re.match(
        r"\s*[-:–—]?\s*(?:\n\s*)*([^\n]+)",
        rest,
    )
    if not vm:
        return None
    val = vm.group(1)
    val = re.sub(r"^[\s\-:–—]+", "", val)
    val = _WS_RE.sub(" ", val).strip(" -:–—\t")
    return val or None


# Prose-form extractors anchored on the French cover sentence.
_PROSE_AIRCRAFT_RE = re.compile(
    r"l[’']?\s*a[ée]ronef\s+([^\n]+)", re.IGNORECASE
)
_PROSE_OPERATOR_RE = re.compile(
    r"exploit[ée]\s+par\s+([^\n]+)", re.IGNORECASE
)
_PROSE_LOCATION_RE = re.compile(
    r"survenue?\s+le\s+[^\n]*\n\s*[àa]\s+([^\n]+)", re.IGNORECASE
)

# Labelled-form fallbacks.
_AIRCRAFT_LABEL_RE = re.compile(r"A[ée]ronef", re.IGNORECASE)
_LOCATION_LABEL_RE = re.compile(r"Lieu|Localisation", re.IGNORECASE)
_OPERATOR_LABEL_RE = re.compile(r"Exploitant|Op[ée]rateur", re.IGNORECASE)


def _clean(val: str | None) -> str | None:
    if not val:
        return None
    val = _WS_RE.sub(" ", val).strip(" -:–—\t")
    return val or None


def extract_aircraft(text: str | None) -> str | None:
    """Aircraft make/model from the cover block (prose or labelled form)."""
    if not text:
        return None
    head = text[:_HEAD_CHARS]
    m = _PROSE_AIRCRAFT_RE.search(head)
    if m:
        return _clean(m.group(1))
    return _extract_labelled(head, _AIRCRAFT_LABEL_RE)


def extract_location(text: str | None) -> str | None:
    """Event location from the cover block (prose or labelled form)."""
    if not text:
        return None
    head = text[:_HEAD_CHARS]
    m = _PROSE_LOCATION_RE.search(head)
    if m:
        return _clean(m.group(1))
    return _extract_labelled(head, _LOCATION_LABEL_RE)


def extract_operator(text: str | None) -> str | None:
    """Operator from the cover block (prose or labelled form)."""
    if not text:
        return None
    head = text[:_HEAD_CHARS]
    m = _PROSE_OPERATOR_RE.search(head)
    if m:
        return _clean(m.group(1))
    return _extract_labelled(head, _OPERATOR_LABEL_RE)


# --------------------------------------------------
# Discovery
# --------------------------------------------------

def _cell_text(raw_html: str) -> str:
    return _WS_RE.sub(" ", _html.unescape(_TAG_RE.sub("", raw_html))).strip()


def parse_listing(index_html: str) -> list[dict]:
    """
    Parse the BEA Congo index table -> list of report dicts.

    One dict per occurrence row that has a FINAL-report PDF (last column).
    Each dict has:
      case_id      str   intrinsic 'beacg-<slug>' from the final PDF filename
      pdf_url      str   absolute final-report URL (href kept verbatim; the
                         downloader percent-encodes it at request time)
      title        str   reference + aircraft cell text (best-effort)
      bea_ref      str|None  normalised 'bea-NN-YYYY' from the Reference cell
      aircraft     str|None  Aeronef cell
      event_date   str|None  ISO date from the dd/mm/yyyy listing cell
      location     str|None  Localisation cell
      event_class  str|None  Categorie cell (Accident / Incident / ...)

    Rows whose final-report column is empty (investigation still "En cours")
    are skipped -- they have no canonical document yet.  Order preserved;
    de-duplicated by case_id.
    """
    seen: set[str] = set()
    rows: list[dict] = []
    for rm in _ROW_RE.finditer(index_html):
        body = rm.group(1)
        cells: dict[str, str] = {}
        for cm in _CELL_RE.finditer(body):
            cells[cm.group(1)] = cm.group(2)
        # header row has <th>; skip rows without a final-report column.
        final_cell = cells.get("column-11", "")
        pdf_m = _PDF_HREF_RE.search(final_cell)
        if not pdf_m:
            continue
        href = _html.unescape(pdf_m.group(1).strip())
        case_id = make_case_id(href)
        if case_id in seen:
            continue
        seen.add(case_id)

        ref_text = _cell_text(cells.get("column-1", ""))
        aircraft = _cell_text(cells.get("column-2", "")) or None
        date_cell = _cell_text(cells.get("column-3", ""))
        location = _cell_text(cells.get("column-4", "")) or None
        event_class = _cell_text(cells.get("column-5", "")) or None

        bea_ref = _normalize_case_id(ref_text)
        event_date = _num_date_at(date_cell) if date_cell else None

        title_bits = [b for b in (ref_text, aircraft) if b]
        title = " ".join(title_bits).strip()

        rows.append({
            "case_id": case_id,
            "pdf_url": href,
            "title": title or None,
            "bea_ref": bea_ref,
            "aircraft": aircraft,
            "event_date": event_date,
            "location": location,
            "event_class": event_class,
        })
    return rows


# --------------------------------------------------
# Download
# --------------------------------------------------

def download(client, pdf_url: str, dest: str | Path) -> None:
    """
    GET pdf_url with Referer header and write bytes to dest.

    The path component is percent-encoded so hrefs containing spaces or
    already-encoded sequences resolve correctly while the listing href is
    stored verbatim.  Raises httpx.HTTPStatusError on non-2xx responses.
    """
    parts = urllib.parse.urlsplit(pdf_url)
    safe_path = urllib.parse.quote(parts.path, safe="/%")
    encoded = urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, safe_path, parts.query, parts.fragment)
    )
    resp = client.get(encoded, headers={"Referer": REFERER})
    resp.raise_for_status()
    with open(dest, "wb") as fh:
        fh.write(resp.content)
