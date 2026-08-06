# ciaiauy_ingest/ciaiauy.py
"""CIAIA (Uruguay) HTML scraper: anchor-driven listing discovery + PDF download.

Source: gub.uy, Ministerio de Defensa Nacional, Comisión Investigadora de
Accidentes e Incidentes de Aviación (CIAIA).

The reports are NOT exposed through one tidy listing.  They are spread across a
handful of static, server-rendered <table>-based pages under
/ministerio-defensa-nacional/{politicas-y-gestion,tematica}/...  Each report is
a single <a href="...pdf">Informe Final CX-XXX</a> anchor whose link text carries
the registration and (loosely) the event class.

Per the live scout we key off the ANCHORS exactly as they appear — we never
construct PDF URLs by guessing.  We crawl a fixed seed list, union every distinct
PDF anchor, and derive metadata from the anchor text + filename:

  * registration  — CX-XXX / LV-XXX / N… / F-… etc. parsed from the anchor text
  * event_class   — "Incidente Grave"/SINCID -> Serious incident, plain
                    "Incidente"/INCID -> Incident, else Accident
  * caso number   — leading/dated NNN in the filename when unambiguous
  * case_id       — caso-NNN when a Caso number is present; else, intrinsic and
                    order-independent: {reg}-{event-date} (date from filename),
                    {reg}-{urlhash} when no date, or ciaiauy-{urlhash} when no
                    registration.  NEVER encounter-order suffixed.

PDFs are Spanish text-layer reports; the scanned gate (pdf.py) handles the rare
image-only exception.
"""
import datetime as _datetime
import hashlib as _hashlib
import html as _html
import re
import sys as _sys
import urllib.parse as _urlparse
from pathlib import Path

BASE = "https://www.gub.uy"
# The CIAIA accidents page whose URL contains '.../accidentes'.
INDEX_URL = BASE + "/ministerio-defensa-nacional/politicas-y-gestion/accidentes"
REFERER = INDEX_URL
DELAY = 2.0

# Fixed seed listing pages (anchor-driven; union of all PDF anchors).  These are
# the server-rendered CIAIA report tables on gub.uy as of the 2026-06-07 scout.
SEED_PATHS = [
    "/ministerio-defensa-nacional/politicas-y-gestion/accidentes",
    "/ministerio-defensa-nacional/politicas-y-gestion/incidentes-graves",
    "/ministerio-defensa-nacional/tematica/informes-accidentes-incidentes-aviacion-civil",
    "/ministerio-defensa-nacional/tematica/informes-finales",
    "/ministerio-defensa-nacional/tematica/informes-incidentes",
    "/ministerio-defensa-nacional/tematica/informes-incidentes-graves",
    "/ministerio-defensa-nacional/politicas-y-gestion/informes-finales-incidentes",
    "/ministerio-defensa-nacional/politicas-y-gestion/informes-finales-incidentes-graves",
    "/ministerio-defensa-nacional/politicas-y-gestion/"
    "informes-finales-incidentes-incidentes-graves-accidentes-aviacion-civil",
]

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

# PDF anchor: <a ... href="....pdf...">link text</a>
_PDF_ANCHOR_RE = re.compile(
    r'<a\b[^>]*\bhref="([^"]+\.pdf[^"]*)"[^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)

# Registration marks seen in UY reports: Uruguay CX-XXX(-R/-X), Argentina LV-XXX,
# US N-numbers, France F-XXXX, Paraguay ZP-XXX, Spain EC-XXX, etc.
_REG_RE = re.compile(
    r"\b("
    r"CX-[A-Z]{2,4}(?:-[A-Z])?"      # Uruguay  CX-MGP, CX-BJT-R, CX-JLI-X
    r"|LV-[A-Z]{2,4}(?:-[A-Z])?"      # Argentina LV-WIZ, LV-BZP
    r"|ZP-[A-Z]{2,4}"                 # Paraguay ZP-BJV
    r"|EC-[A-Z]{3}"                   # Spain    EC-GPB
    r"|PR-[A-Z]{3}|PT-[A-Z]{3}"       # Brazil
    r"|C-[A-Z]{4}"                    # Canada   C-GSGV
    r"|M-[A-Z]{4}"                    # Isle of Man M-FALZ
    r"|F-[A-Z]{4}"                    # France   F-GSPA
    r"|N\d{1,5}[A-Z]{0,2}"            # US       N3024N, N496, N527K
    r")\b",
    re.IGNORECASE,
)

# Caso number in the PDF *filename*, restricted to forms that cannot be confused
# with an aircraft model number (e.g. PA32RT-300T, PA34-200T, 208B):
#   - leading "NNN" at the very start of the filename stem      (611 CX-OTA-R)
#   - "no.-NNN" / "no NNN"                                       (no.-582-n3024n)
#   - after an 8-digit date:  informefinal-YYYYMMDD-NNN…        (...-20120228-538…)
#   - after "informe-NNN" / "informe-final-NNN"                 (informe-final-567…)
_CASO_RES = [
    re.compile(r"^\s*(\d{3})(?=[ \-_a-zA-Z]|$)"),
    re.compile(r"\bno\.?\s*-?\s*(\d{3})(?=[ \-_a-zA-Z]|$)", re.IGNORECASE),
    re.compile(r"-\d{8}-(\d{3})(?=[ \-_a-zA-Z]|$)"),
    re.compile(r"informe(?:-?final)?-(\d{3})(?=[ \-_a-zA-Z]|$)", re.IGNORECASE),
]

# YYYYMMDD anywhere in the filename (e.g. informefinal-20120228-538…).
_DATE_FN_RE = re.compile(r"\b(20\d{2})(\d{2})(\d{2})\b")

# Trailing DD-MM-YY or DD-MM-YYYY at/near the END of the filename stem
# (e.g. ...-cx-agg-29-04-14, ...-cx-bgy-18-10-2014, ...-24-03-16).  The
# (?<!\d) lookbehind and (?!\d) lookahead stop it eating into a registration
# digit run (n3024n) or an aircraft model number (pa32rt-300t, pa34-200t).
_DATE_DMY_RE = re.compile(
    r"(?<!\d)(\d{1,2})[.\-_ ](\d{1,2})[.\-_ ](\d{4}|\d{2})(?!\d)"
)


# ──────────────────────────────────────────────
# Client factory
# ──────────────────────────────────────────────

def make_client():
    """Return an httpx.Client configured with browser UA."""
    import httpx
    return httpx.Client(
        headers=HEADERS,
        follow_redirects=True,
        timeout=60.0,
    )


# ──────────────────────────────────────────────
# case_id helpers
# ──────────────────────────────────────────────

def _normalize_case_id(raw: str) -> str:
    """
    Collapse whitespace and stray separators in a case id.

    Trims, lower-cases, collapses internal whitespace runs, and removes spaces
    sitting immediately around '-' and '/' so 'caso - 611' / 'CX - MGP' both
    normalise cleanly (e.g. 'caso-611', 'cx-mgp').
    """
    s = (raw or "").strip().lower()
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"\s*-\s*", "-", s)
    s = re.sub(r"\s*/\s*", "/", s)
    return s.strip("-/ ")


def _url_hash(pdf_url: str) -> str:
    """Stable 6-hex-char discriminator derived from the report's pdf_url.

    Intrinsic to the physical report (the pdf_url is the per-report natural id),
    so the same report always yields the same hash regardless of discovery order
    or a fresh re-seed into an empty DB.
    """
    return _hashlib.sha1((pdf_url or "").encode("utf-8")).hexdigest()[:6]


def make_case_id(caso, registration, pdf_url="", date_iso=None):
    """
    Build a STABLE, order-independent case_id for a UY report.

    The key is intrinsic to the physical report — identical inputs always
    produce an identical id, no matter the encounter order or a fresh re-seed:

      1. 'caso-NNN'                when a Caso number was reliably extracted.
      2. '{reg}-{YYYY-MM-DD}'      no caso but a registration AND an event date
                                   parsed from the filename (e.g. zp-bjv-2015-08-14).
      3. '{reg}-{urlhash}'         no caso, registration but no date
                                   (e.g. cx-byk-r-<sha1[:6]>).
      4. 'ciaiauy-{urlhash}'       no caso and no registration.

    There is intentionally NO bare-'{reg}' branch and NO encounter-order
    collision suffix: two distinct accidents of the same registration are
    disambiguated by their own date or pdf_url hash, never by which anchor was
    seen first.  This prevents two ids from swapping which PDF they point to if
    gub.uy reorders anchors or a run reorders pages.
    """
    if caso:
        return _normalize_case_id(f"caso-{caso}")
    if registration:
        reg = _normalize_case_id(registration)
        if date_iso:
            return f"{reg}-{date_iso}"
        return f"{reg}-{_url_hash(pdf_url)}"
    return f"ciaiauy-{_url_hash(pdf_url)}"


def extract_registration(anchor_text: str, filename: str = "") -> str | None:
    """Best-effort registration from anchor text, falling back to filename."""
    for src in (anchor_text or "", filename or ""):
        m = _REG_RE.search(src.replace("_", "-"))
        if m:
            return m.group(1).upper()
    return None


def extract_caso(filename: str) -> str | None:
    """Extract a Caso number from a PDF filename, or None when ambiguous/absent."""
    stem = filename.rsplit(".pdf", 1)[0]
    for rx in _CASO_RES:
        m = rx.search(stem)
        if m:
            return m.group(1)
    return None


def _norm_year(yy: str) -> int:
    """2-digit-year pivot, shared with the caso-year discipline.

    A 2-digit year maps to 20xx unless that lands in the future
    (current year + 1), in which case it is 19xx.  4-digit years pass through.
    """
    y = int(yy)
    if len(yy) == 4:
        return y
    cur = _datetime.date.today().year
    return 2000 + y if (2000 + y) <= cur + 1 else 1900 + y


def _date_from_filename(filename: str) -> str | None:
    """Parse an event date from the filename -> ISO date (YYYY-MM-DD), else None.

    Tries YYYYMMDD first (e.g. informefinal-20120228-538…), then a trailing
    DD-MM-YY / DD-MM-YYYY (e.g. ...-cx-agg-29-04-14, ...-cx-bgy-18-10-2014).
    The DD-MM-YY form takes the LAST match in the stem so a leading caso/model
    number cannot be mistaken for a day.  Out-of-range day/month rejects the
    candidate (so model numbers like pa34-200t never become a date).
    """
    stem = filename.rsplit(".pdf", 1)[0]

    m = _DATE_FN_RE.search(stem)
    if m:
        y, mo, d = m.group(1), m.group(2), m.group(3)
        if 1 <= int(mo) <= 12 and 1 <= int(d) <= 31:
            return f"{y}-{mo}-{d}"

    last = None
    for cand in _DATE_DMY_RE.finditer(stem):
        last = cand
    if last:
        d, mo = int(last.group(1)), int(last.group(2))
        if 1 <= d <= 31 and 1 <= mo <= 12:
            y = _norm_year(last.group(3))
            return f"{y:04d}-{mo:02d}-{d:02d}"
    return None


# ──────────────────────────────────────────────
# Listing parsing
# ──────────────────────────────────────────────

def _clean_anchor_text(raw: str) -> str:
    """Strip inner tags / nbsp / entities from an anchor's inner HTML."""
    s = re.sub(r"<[^>]+>", " ", raw)
    s = _html.unescape(s).replace("\xa0", " ")
    return re.sub(r"\s+", " ", s).strip()


def _event_class_from_text(text: str) -> str:
    """Map UY anchor/filename hints to the program event_class vocabulary.

    'Incidente Grave' / 'SINCID'   -> 'Serious incident'
    plain 'Incidente' / 'INCID'    -> 'Incident'
    everything else                -> 'Accident'

    The grave/sincid test runs FIRST so a serious incident is never downgraded
    to a plain Incident.  'Incident' is a real class in the shared accidents
    vocabulary (same as the CIAIAC sibling: Accident | Serious incident |
    Incident), so a plain Incidente is no longer mislabelled Serious.
    """
    low = text.lower()
    if "incidente grave" in low or "sincid" in low:
        return "Serious incident"
    if "incidente" in low or "incid" in low:
        return "Incident"
    return "Accident"


def parse_listing(html: str) -> list[dict]:
    """
    Parse one CIAIA listing page -> list of report dicts (one per PDF anchor).

    Each dict has:
      pdf_url            str   absolute URL, scraped exactly from the anchor href
      title              str   cleaned anchor text
      registration       str|None
      event_class        str   'Accident' | 'Serious incident' | 'Incident'
      caso               str|None
      date_of_occurrence str|None  (YYYY-MM-DD when a date is in the filename)

    Anchors are de-duplicated by absolute pdf_url within the page.
    """
    rows: list[dict] = []
    seen: set[str] = set()

    for m in _PDF_ANCHOR_RE.finditer(html):
        href = _html.unescape(m.group(1)).strip()
        if not href:
            continue
        pdf_url = href if href.startswith("http") else BASE + href
        if pdf_url in seen:
            continue
        seen.add(pdf_url)

        text = _clean_anchor_text(m.group(2))
        # Decode the filename for metadata extraction.
        filename = _urlparse.unquote(pdf_url.rsplit("/", 1)[-1])

        registration = extract_registration(text, filename)
        event_class = _event_class_from_text(text + " " + filename)
        caso = extract_caso(filename)
        date_iso = _date_from_filename(filename)

        rows.append({
            "pdf_url": pdf_url,
            "title": text or filename,
            "registration": registration,
            "event_class": event_class,
            "caso": caso,
            "date_of_occurrence": date_iso,
        })

    return rows


# ──────────────────────────────────────────────
# Download
# ──────────────────────────────────────────────

def download(client, pdf_url: str, dest: str | Path) -> None:
    """GET pdf_url with Referer header and write bytes to dest."""
    resp = client.get(pdf_url, headers={"Referer": REFERER})
    resp.raise_for_status()
    with open(dest, "wb") as fh:
        fh.write(resp.content)
