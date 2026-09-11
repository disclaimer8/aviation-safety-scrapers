# aaicth_ingest/aaicth.py
"""Thailand AAIC (Aircraft Accident Investigation Committee / กบร) HTML scraper.

Source pages (ops.mot.go.th — NOT www.mot.go.th which returns WAF 418):
  https://ops.mot.go.th/aaic.html?dsfm_lang=EN&id=7   — Final accident reports
  https://ops.mot.go.th/aaic.html?dsfm_lang=EN&id=75  — Interim reports
  https://ops.mot.go.th/aaic.html?dsfm_lang=EN&id=76  — Serious incident reports

PDFs live on motdrive.mot.go.th as NextCloud shared links:
  https://motdrive.mot.go.th/index.php/s/{token}          — viewer page
  https://motdrive.mot.go.th/index.php/s/{token}/download — direct download

Listing structure (server-rendered HTML, one <li> per investigation):
  Each <li> contains a line like:
    File No. NN/YYYY (F?) AircraftType, Registration
    <a href="...Thai">Thai</a> / <a href="...English">English</a>
  Year sections are preceded by <strong>YYYY</strong> headers.
  File No format: NN/YYYY  (sequence number / CE year of occurrence)
  Note: 'File' sometimes written without 'No.', token after is always NN/YYYY.

Language:
  ~26 reports are bilingual (Thai + English PDF links); rest are Thai-only.
  When both: prefer English PDF; store pdf_url_en + pdf_url_th.
  When Thai-only: lang='th'; Phase 3 will translate TH→EN.

Date handling:
  The listing uses CE years (2001, 2006, ...2024) — NOT Buddhist Era.
  The File No YYYY component is CE year of occurrence (year-level precision).
  PDFs may contain exact BE dates (พ.ศ. XXXX = CE - 543) but we only store
  year-level date from listing: YYYY-01-01 as a best-effort anchor.
  NOTE: if the PDF text can yield an exact date, parse() will update event_date.

Superseded_by:
  Cases appearing on BOTH id=7 (final) and id=75 (interim) — the final
  report supersedes the interim. build() deletes the superseded interim
  accident row, keeping only the final.

SSL:
  ops.mot.go.th presents a valid Sectigo chain for *.mot.go.th and verifies
  against the system store — checked 2026-09-11, openssl reports "Verify
  return code: 0 (ok)". This module used to claim a chain issue made
  verify=False necessary and passed it in two places; it has been fixed
  upstream. Verification is ON.

robots.txt:
  The host disallows everything for every agent. This source takes a
  documented exemption — see _make_client in cli.py.
"""
import html as _html
import re
from pathlib import Path

# NOT PROMOTED TO sources/ — robots.txt forbids it.
#
# https://ops.mot.go.th/robots.txt is, in full:
#     User-agent: *
#     Disallow: /
# A blanket ban on the whole host for every agent. Checked 2026-09-11.
#
# This package is deployed and running, so it is crawling a site that has
# said no. sources/ enforces Disallow (the single exemption, bfu, is
# documented and slated for removal), so promoting this one would mean
# either violating that policy or adding a second exemption. Neither was
# mine to choose, so it stays here, unpromoted, with the reason recorded.
#
# Routes forward, none of them code: ask ICAO/the Thai AAIC for the reports
# directly, or ask the site operator whether the ban is intended — a blanket
# Disallow on a public register is often a default nobody revisited.
BASE = "https://ops.mot.go.th"
MOTDRIVE = "https://motdrive.mot.go.th"
DELAY = 2.0

LISTING_URLS = {
    "final":            BASE + "/aaic.html?dsfm_lang=EN&id=7",
    "interim":          BASE + "/aaic.html?dsfm_lang=EN&id=75",
    "serious_incident": BASE + "/aaic.html?dsfm_lang=EN&id=76",
}

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0 Safari/537.36"
)

HEADERS = {
    "User-Agent": UA,
}

# ──────────────────────────────────────────────────────────────────────────────
# Regexes
# ──────────────────────────────────────────────────────────────────────────────

# Match File No / File  NN/YYYY in listing line (captures seq + CE year)
# Handles: "File No. 01/2001", "File No.\xa008/2010", "File 57/2019"
_FILE_NO_RE = re.compile(
    r"File\s+(?:No\.?\s*)?\s*(\d{1,4})\s*/\s*(20\d{2})",
    re.IGNORECASE,
)

# NextCloud shared-link token (path segment after /s/)
_NC_TOKEN_RE = re.compile(
    r'href="(https://motdrive\.mot\.go\.th/index\.php/s/([A-Za-z0-9_-]+))"',
    re.IGNORECASE,
)

# Anchor text (Thai / English / Thai  / English)
_ANCHOR_LANG_RE = re.compile(
    r'<a[^>]*href="(https://motdrive\.mot\.go\.th/index\.php/s/[A-Za-z0-9_-]+)"[^>]*>\s*(Thai|English)\s*</a>',
    re.IGNORECASE,
)

# year section header <strong>YYYY</strong>
_YEAR_HEADER_RE = re.compile(r"<strong>\s*(20\d{2})\s*</strong>")

# registration: HS-XXX or U-XXX (Thai civil regs) or other G-/N-/VT-/9V- etc.
_REG_RE = re.compile(r"\b(HS-[A-Z0-9]+|U-[A-Z0-9]+|[A-Z]{1,2}-[A-Z0-9]{2,})\b")

# BE (Buddhist Era) year pattern for text extraction: พ.ศ. XXXX or ปี XXXX
# BE year 2544 = CE 2001; BE > 2400 always; CE year never > 2200
_BE_YEAR_RE = re.compile(r"พ\.ศ\.\s*(\d{4})")
_BE_DATE_RE = re.compile(
    r"(\d{1,2})\s+(มกราคม|กุมภาพันธ์|มีนาคม|เมษายน|พฤษภาคม|มิถุนายน|"
    r"กรกฎาคม|สิงหาคม|กันยายน|ตุลาคม|พฤศจิกายน|ธันวาคม)\s+(?:พ\.ศ\.)?\s*(\d{4})"
)

_TH_MONTHS = {
    "มกราคม": 1, "กุมภาพันธ์": 2, "มีนาคม": 3, "เมษายน": 4,
    "พฤษภาคม": 5, "มิถุนายน": 6, "กรกฎาคม": 7, "สิงหาคม": 8,
    "กันยายน": 9, "ตุลาคม": 10, "พฤศจิกายน": 11, "ธันวาคม": 12,
}

# English date in EN PDFs: "4 August 2009", "ON 4 AUGUST 2009"
_EN_DATE_RE = re.compile(
    r"\b(\d{1,2})\s+(January|February|March|April|May|June|July|August|"
    r"September|October|November|December)\s+(20\d{2}|19\d{2})\b",
    re.IGNORECASE,
)
_EN_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}

# ──────────────────────────────────────────────────────────────────────────────
# Thai Buddhist-Era calendar helpers
# ──────────────────────────────────────────────────────────────────────────────

def be_to_ce(be_year: int) -> int:
    """Convert Thai Buddhist Era year to Common Era.
    พ.ศ. 2544 → 2001.  BE years > 2400 are safe to convert; smaller values
    are treated as already-CE (shouldn't appear in accident dates).
    """
    if be_year > 2400:
        return be_year - 543
    return be_year  # already CE


def parse_thai_date(text: str) -> str | None:
    """Extract first Thai date from text and return ISO YYYY-MM-DD.

    Handles both Thai text dates (วันที่ DD MONTH พ.ศ. YYYY) and
    English dates (D Month YYYY) in the same text block.
    Returns None if no date found.
    """
    # Try Thai full date first
    m = _BE_DATE_RE.search(text)
    if m:
        day = int(m.group(1))
        month = _TH_MONTHS.get(m.group(2), 0)
        year_raw = int(m.group(3))
        ce_year = be_to_ce(year_raw)
        if month and 1990 <= ce_year <= 2030:
            return f"{ce_year:04d}-{month:02d}-{day:02d}"
    # Try English date
    m = _EN_DATE_RE.search(text)
    if m:
        day = int(m.group(1))
        month = _EN_MONTHS.get(m.group(2).lower(), 0)
        year = int(m.group(3))
        if month and 1990 <= year <= 2030:
            return f"{year:04d}-{month:02d}-{day:02d}"
    return None


# ──────────────────────────────────────────────────────────────────────────────
# Client factory
# ──────────────────────────────────────────────────────────────────────────────

def make_client(proxy=None, **_kw):
    """Return the client this source should use.

    See cli._make_client for the robots.txt exemption and the note on why
    verify is no longer disabled.
    """
    from . import httpc
    return httpc.make_client(
        headers=HEADERS, proxy=proxy, timeout=120, delay=DELAY,
        obey_robots=False,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Listing parser
# ──────────────────────────────────────────────────────────────────────────────

def _strip_html(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s)
    s = _html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def _download_url(token: str) -> str:
    """Build direct download URL for a NextCloud shared-link token."""
    return f"{MOTDRIVE}/index.php/s/{token}/download"


def parse_listing(html: str, report_type: str) -> list[dict]:
    """Parse a AAIC listing page → list of investigation dicts.

    Each dict has:
      case_id           str   e.g. 'aaicth-07/2014'
      seq_no            str   e.g. '07'
      year              int   CE year from File No
      report_type       str   'final' | 'interim' | 'serious_incident'
      report_url        None  (no per-case HTML page)
      pdf_url           str|None  preferred URL (EN > TH)
      pdf_url_th        str|None  Thai PDF direct download URL
      pdf_url_en        str|None  English PDF direct download URL
      aircraft          str|None  aircraft type from listing text
      registration      str|None  registration from listing text
      event_date        str   YYYY-01-01 (year precision; parse() may refine)
      title             str   full line text
      lang              'en' | 'th'
    """
    rows: list[dict] = []
    seen: set[str] = set()

    # Walk <li> elements that contain File No entries
    li_re = re.compile(r"<li>(.*?)</li>", re.DOTALL | re.IGNORECASE)
    current_year = None

    for li_m in li_re.finditer(html):
        li_html = li_m.group(1)

        # Track year from <strong> header in surrounding context (but also
        # derive from File No YYYY directly — more robust)
        m_year = _YEAR_HEADER_RE.search(li_html)
        if m_year:
            current_year = int(m_year.group(1))

        m_file = _FILE_NO_RE.search(li_html)
        if not m_file:
            continue

        seq_no = m_file.group(1).lstrip("0") or "0"
        year = int(m_file.group(2))

        # Build canonical case_id (source-prefixed, seq/year, unambiguous)
        # Build case_id: interim/SI get a report_type suffix to avoid collision
        # when the same File No has both interim and final on different pages.
        suffix = f"-{report_type}" if report_type != "final" else ""
        case_id = f"aaicth-{seq_no}/{year}{suffix}"
        if case_id in seen:
            continue

        # Extract language-tagged PDF links
        pdf_th = None
        pdf_en = None
        for anchor_m in _ANCHOR_LANG_RE.finditer(li_html):
            url = anchor_m.group(1)
            lang_label = anchor_m.group(2).lower()
            token_m = re.search(r"/s/([A-Za-z0-9_-]+)", url)
            if not token_m:
                continue
            token = token_m.group(1)
            dl_url = _download_url(token)
            if lang_label == "english" and pdf_en is None:
                pdf_en = dl_url
            elif lang_label == "thai" and pdf_th is None:
                pdf_th = dl_url

        # If no anchors found at all, skip — no document
        if not pdf_th and not pdf_en:
            continue

        # Preferred PDF: EN > TH
        pdf_url = pdf_en or pdf_th
        lang = "en" if pdf_en else "th"

        # Extract aircraft + registration from line text
        line_text = _strip_html(li_html)
        reg_m = _REG_RE.search(line_text)
        registration = reg_m.group(1) if reg_m else None

        # Aircraft type: text between File No marker and registration
        # Strip HTML tags, then clean up Thai/English link labels and NBSP noise.
        aircraft = None
        file_end = m_file.end()
        raw_line = re.sub(r"<[^>]+>", " ", li_html[file_end:])
        raw_line = _html.unescape(raw_line).strip()
        # Strip (F) marker
        raw_line = re.sub(r"^\s*\(F\)\s*", "", raw_line)
        # Strip "Thai" / "English" link text residue and "/" separators
        raw_line = re.sub(r"\b(Thai|English)\b", "", raw_line, flags=re.IGNORECASE)
        raw_line = re.sub(r"\s*/\s*", " ", raw_line)
        if registration:
            raw_line = raw_line.replace(registration, "").strip()
        # Collapse whitespace and strip trailing commas/spaces
        raw_line = re.sub(r"[\xa0\s]+", " ", raw_line).strip()
        raw_line = re.sub(r"[,\s]+$", "", raw_line).strip()
        if raw_line:
            aircraft = raw_line[:100]

        seen.add(case_id)
        rows.append({
            "case_id": case_id,
            "seq_no": seq_no,
            "year": year,
            "report_type": report_type,
            "report_url": None,
            "pdf_url": pdf_url,
            "pdf_url_th": pdf_th,
            "pdf_url_en": pdf_en,
            "aircraft": aircraft,
            "registration": registration,
            "event_date": f"{year}-01-01",
            "title": line_text[:300],
            "lang": lang,
        })

    return rows


# ──────────────────────────────────────────────────────────────────────────────
# Download
# ──────────────────────────────────────────────────────────────────────────────

def download(client, url: str, dest: str | Path) -> None:
    """GET url and write bytes to dest.  Raises on non-2xx responses."""
    resp = client.get(url)
    resp.raise_for_status()
    with open(dest, "wb") as fh:
        fh.write(resp.content)
