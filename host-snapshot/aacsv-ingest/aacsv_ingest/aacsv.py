# aacsv_ingest/aacsv.py
"""El Salvador AAC (Autoridad de Aviación Civil) accident-investigation scraper.

Source: https://www.aac.gob.sv/informes-de-accidentes/  (page_id=1006)
  - One WordPress Download Manager (WPDM) datatable; each <tr> is one report
    package: a <strong> label (often the AAC reference), a Spanish description
    ("Informe final del accidente de la aeronave <REG> ocurrido ..."), a publish
    date, an <embed src=...pdf> preview path and a Descargar anchor whose
    data-downloadurl is /download/<slug>/?wpdmdl=<id>&refresh=<token>.
  - The &refresh=<token> param is droppable; /download/<slug>/?wpdmdl=<id>
    returns the PDF directly (octet-stream).
  - SPANISH text-layer PDFs.  One row (informe-final-8) is a SCAN (broken XRef)
    → 0 chars from pdftotext → skipped by the scanned gate.

case_id (intrinsic, deduped): AAC-AIG-<NNN>-<REG>-<YYYY> where derivable from the
  AAC reference + registration + year; FINAL reports are preferred over
  PRELIMINARY ones for the same occurrence (same case base).  The per-row WPDM
  slug is the always-unique aacsv_reports key; case_id is the deduped projection
  key for aacsv_accidents.
"""
import html as _html
import re
from pathlib import Path

BASE = "https://www.aac.gob.sv"
INDEX_URL = BASE + "/informes-de-accidentes/"
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

_MONTHS_ES = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12,
}

# ── compiled regexes ──────────────────────────────────────────────────────────

# One package row carries a Descargar anchor with a /download/<slug>/?wpdmdl=<id>
_ROW_SPLIT_RE = re.compile(r"<tr[ >]")
_DOWNLOADURL_RE = re.compile(
    r'data-downloadurl="(https?://[^"]*?/download/([^/]+)/\?wpdmdl=(\d+)[^"]*)"',
    re.IGNORECASE,
)
_EMBED_RE = re.compile(r'<embed\s+src="([^"]+?\.pdf)"', re.IGNORECASE)
_LABEL_RE = re.compile(r"<strong>(.*?)</strong>", re.DOTALL)
_DESC_RE = re.compile(
    r'__dt_col_description[^>]*>\s*<p>(.*?)</p>', re.DOTALL | re.IGNORECASE
)
_PUBDATE_RE = re.compile(
    r"__dt_publish_date[^>]*>(.*?)</span>", re.DOTALL | re.IGNORECASE
)

# Registration marks: Salvadoran YS-..., foreign N..., OM-H..., etc.
_REG_RE = re.compile(
    r"\b("
    r"YS[\s_-]?\d{2,4}[\s_-]?[A-Z]{0,2}"   # YS-289PE, YS 331 PE, YS05P
    r"|N[\s_-]?\d{3,5}[A-Z]{0,2}"          # N9417T, N 95207, N7381G
    r"|OM[\s_-]?H?\d+[A-Z]?"               # OM-H747
    r")\b",
    re.IGNORECASE,
)

# AAC reference variants in the <strong> label, e.g.:
#   AAC-05/22, AAC-009/21, AAC- 004/21, AAC-002/19, ACC 001 2019,
#   AAC 003 2016, AAC-ACCID-001-2016, AAC-INCID-091-2014, AAC-INCD-002-2005,
#   AAC-003/21 RI, AAC-ACCID-YS-125PE
_AACREF_RE = re.compile(
    r"\bA[AC]C[\s_-]*"
    r"(?:(ACCID|INCID|INCD|AIG)[\s_-]*)?"
    r"(\d{1,3})"
    r"[\s_/-]+"
    r"(\d{2,4})\b",
    re.IGNORECASE,
)

# Occurrence date inside the description ("ocurrido en fecha 25/10/2025" or
# "ocurrido el 19 de febrero del 2025" or "ocurrido 16 octubre 2024")
_DATE_NUM_RE = re.compile(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{4})\b")
_DATE_ES_RE = re.compile(
    r"\b(\d{1,2})\s+(?:de\s+)?([a-záéíóúñ]+)\s+(?:de\s+|del\s+)?(\d{4})\b",
    re.IGNORECASE,
)


# ── client factory ────────────────────────────────────────────────────────────

def make_client():
    import httpx
    return httpx.Client(
        headers=HEADERS,
        follow_redirects=True,
        timeout=60.0,
    )


# ── normalisation helpers ─────────────────────────────────────────────────────

def _normalize_reg(raw):
    """Normalise a registration mark to e.g. 'YS-289PE', 'N9417T', 'OM-H747'."""
    if not raw:
        return None
    s = re.sub(r"[\s_]+", "", raw.strip().upper())
    # YS / OM marks use a dash before the numeric block
    m = re.match(r"^(YS|OM)-?(.+)$", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    return s  # N-marks stay solid (N9417T)


def _reg_key(reg):
    """Collapse a registration to a comparison key (strip dashes, suffix-tolerant).

    YS-289P, YS-289PE, YS 289 PE all collapse to 'YS289' (digits+leading prefix)
    so a FINAL and its PRELIMINARY for the same airframe map to one occurrence.
    """
    if not reg:
        return ""
    s = re.sub(r"[^A-Z0-9]", "", reg.upper())
    m = re.match(r"^([A-Z]+)(\d+)", s)
    return f"{m.group(1)}{m.group(2)}" if m else s


def _parse_date(text):
    """Extract ISO YYYY-MM-DD from a Spanish description, or None."""
    if not text:
        return None
    m = _DATE_NUM_RE.search(text)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            return f"{y:04d}-{mo:02d}-{d:02d}"
        except ValueError:
            return None
    m = _DATE_ES_RE.search(text)
    if m:
        d = int(m.group(1))
        mo = _MONTHS_ES.get(m.group(2).lower())
        y = int(m.group(3))
        if mo:
            return f"{y:04d}-{mo:02d}-{d:02d}"
    return None


def _norm_year(y2):
    """Normalise a 2- or 4-digit year string to a 4-digit int."""
    y = int(y2)
    if y < 100:
        y = 2000 + y if y <= 30 else 1900 + y
    return y


def _classify_type(label, desc, slug):
    """Return ('final'|'preliminary', event_class) for the row.

    FINAL  : 'informe final' / 'reporte final' / a bare AAC-NNN/YY reference
             label whose description says 'Informe final'.
    PRELIM : declaración provisional / informe preliminar / reporte inicial /
             informe inicial / reporte preliminar / RI suffix.
    """
    hay = f"{label} {desc} {slug}".lower()
    prelim_tokens = (
        "provisional", "preliminar", "inicial", "reporte inicia",
        "reporte-inicia", "declaracion", "declaración", "reporte preliminar",
    )
    if any(t in hay for t in prelim_tokens) or re.search(r"\bri\b", label.lower()):
        report_type = "preliminary"
    elif "final" in hay:
        report_type = "final"
    else:
        report_type = "final"  # bare AAC-ref final reports

    # event class from accident / incident wording
    if "incid" in hay:
        event_class = "Incident"
    else:
        event_class = "Accident"
    return report_type, event_class


def make_case_id(aacref, reg, year, report_type):
    """Construct an intrinsic case_id AAC-AIG-<NNN>-<REG>-<YYYY>.

    aacref: (kind, num, yr) tuple from the AAC label reference, or None.
    Falls back to AAC-AIG-<REG>-<YYYY> when no numeric reference is present.
    """
    reg_part = _normalize_reg(reg) or "UNREG"
    if aacref:
        _kind, num, yr = aacref
        nnn = f"{int(num):03d}"
        yyyy = _norm_year(yr)
        return f"AAC-AIG-{nnn}-{reg_part}-{yyyy:04d}"
    if year:
        return f"AAC-AIG-{reg_part}-{int(year):04d}"
    return f"AAC-AIG-{reg_part}"


def _normalize_case_id(raw):
    """Idempotent normaliser: upper-case, single dashes, collapse whitespace."""
    if not raw:
        return None
    s = re.sub(r"\s+", "-", raw.strip().upper())
    s = re.sub(r"-{2,}", "-", s)
    return s.strip("-")


# ── listing parser ────────────────────────────────────────────────────────────

def parse_listing(html_text):
    """Parse the WPDM datatable → list of row dicts (one per report package).

    Each dict:
      slug, case_id, pdf_url, report_url, title, description, event_class,
      registration, date_of_occurrence, report_type
    Rows without a download anchor are skipped.  case_id at this stage is the
    raw intrinsic id; final-over-prelim dedup happens at projection (build).
    """
    rows = []
    seen_slugs = set()
    for chunk in _ROW_SPLIT_RE.split(html_text):
        dm = _DOWNLOADURL_RE.search(chunk)
        if not dm:
            continue
        raw_url = _html.unescape(dm.group(1))
        slug = dm.group(2)
        wpdmdl = dm.group(3)
        if slug in seen_slugs:
            continue
        seen_slugs.add(slug)

        # drop the &refresh=<token> param — it is not required
        pdf_url = re.sub(r"&refresh=[^&]*", "", raw_url)

        lm = _LABEL_RE.search(chunk)
        label = strip_inline(lm.group(1)) if lm else ""
        dscm = _DESC_RE.search(chunk)
        desc = strip_inline(dscm.group(1)) if dscm else ""
        em = _EMBED_RE.search(chunk)
        embed_url = _html.unescape(em.group(1)) if em else None

        report_type, event_class = _classify_type(label, desc, slug)

        # registration: prefer description, then label, then embed filename
        reg = None
        for hay in (desc, label, (embed_url or "")):
            rm = _REG_RE.search(hay)
            if rm:
                reg = _normalize_reg(rm.group(1))
                break

        # AAC reference (kind, num, yr) from label, else from embed filename
        aacref = None
        for hay in (label, (embed_url or "").split("/")[-1]):
            am = _AACREF_RE.search(hay)
            if am:
                aacref = (am.group(1), am.group(2), am.group(3))
                break

        date_iso = _parse_date(desc) or _parse_date(label)
        year = None
        if date_iso:
            year = int(date_iso[:4])
        elif aacref:
            year = _norm_year(aacref[2])

        case_id = _normalize_case_id(make_case_id(aacref, reg, year, report_type))

        title = label or desc or slug
        rows.append({
            "slug": slug,
            "case_id": case_id,
            "wpdmdl": wpdmdl,
            "pdf_url": pdf_url,
            "report_url": INDEX_URL,
            "title": title,
            "description": desc,
            "event_class": event_class,
            "registration": reg,
            "date_of_occurrence": date_iso,
            "report_type": report_type,
        })
    return rows


def strip_inline(s):
    if not s:
        return ""
    s = re.sub(r"<[^>]+>", " ", s)
    s = _html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


# ── download ──────────────────────────────────────────────────────────────────

def download(client, pdf_url, dest):
    """GET pdf_url with Referer and write bytes to dest. Raises on non-2xx."""
    resp = client.get(pdf_url, headers={"Referer": REFERER})
    resp.raise_for_status()
    with open(dest, "wb") as fh:
        fh.write(resp.content)
