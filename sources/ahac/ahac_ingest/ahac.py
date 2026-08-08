# ahac_ingest/ahac.py
"""Honduras AHAC (Agencia Hondureña de Aeronáutica Civil) HTML scraper.

Source: https://ahac.gob.hn/Accidentes_Incidentes
Static HTML listing of ~38 PDF links in a Bootstrap table.
Each row has a direct PDF URL under /Documentos/ACCIDENTES E INCIDENTES/DOCUMENTO/.

case_id is derived from the PDF filename (URL-decoded stem), prefixed AHAC-:
  ahac-informe-final-del-accidente-de-la-aeronave-con-matricula-hr-aqs
  etc.

Spanish-language source; ICAO Annex 13 structure; 2017-2026 date range.
"""
import html as _html
import re
import urllib.parse
from pathlib import Path

BASE = "https://ahac.gob.hn"
INDEX_URL = BASE + "/Accidentes_Incidentes"
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

# Match PDF links under the DOCUMENTO/ subdir only (skip GUIAS/ and NOTIFICACION/)
_PDF_RE = re.compile(
    r'href="(Documentos/ACCIDENTES%20E%20INCIDENTES/DOCUMENTO/[^"]+\.pdf[^"]*)"',
    re.IGNORECASE,
)

_SLUG_BAD = re.compile(r"[^a-z0-9]+")


def _pdf_stem_to_slug(decoded_filename: str) -> str:
    """Convert decoded PDF filename (no extension) to URL-safe slug, max 80 chars."""
    s = decoded_filename.lower()
    # Remove .pdf suffix if present
    if s.endswith(".pdf"):
        s = s[:-4]
    s = _SLUG_BAD.sub("-", s).strip("-")
    return s[:80]


def _classify_event(filename: str) -> str:
    f = filename.upper()
    if "ACCID" in f or "ACCIDENTE" in f:
        return "Accident"
    if "INCID" in f or "INCIDENTE" in f:
        return "Incident"
    return "Accident"


def make_case_id(pdf_filename_stem: str) -> str:
    """Build intrinsic case_id from PDF filename stem."""
    slug = _pdf_stem_to_slug(pdf_filename_stem)
    return f"ahac-{slug}"


def make_client():
    import httpx
    return httpx.Client(
        headers=HEADERS,
        follow_redirects=True,
        timeout=60.0,
    )


def parse_listing(html_content: str) -> list:
    """Parse AHAC index HTML -> list of report dicts.

    Each dict: {case_id, pdf_url, title, event_class, aircraft, registration,
                date_of_occurrence, location}
    Skips GUIAS/ and NOTIFICACION/ paths. Deduplicates by case_id.
    """
    rows = []
    seen = set()

    for m in _PDF_RE.finditer(html_content):
        href = m.group(1)
        # Skip non-DOCUMENTO subdirs (double-check)
        if "/GU%C3%ADAS/" in href.upper() or "GUIAS" in href.upper() or "NOTIFICACI" in href.upper():
            continue

        decoded_path = urllib.parse.unquote(href)
        # decoded_path: "Documentos/ACCIDENTES E INCIDENTES/DOCUMENTO/<filename>.pdf"
        filename_with_ext = decoded_path.split("/")[-1]
        # Handle double .pdf.pdf case
        stem = filename_with_ext
        if stem.lower().endswith(".pdf.pdf"):
            stem = stem[:-8]
        elif stem.lower().endswith(".pdf"):
            stem = stem[:-4]

        case_id = make_case_id(stem)
        if not case_id or case_id in seen:
            continue
        seen.add(case_id)

        pdf_url = BASE + "/" + href
        title = _html.unescape(filename_with_ext)
        event_class = _classify_event(stem)

        rows.append({
            "case_id": case_id,
            "pdf_url": pdf_url,
            "title": title,
            "event_class": event_class,
            "aircraft": None,
            "registration": None,
            "date_of_occurrence": None,
            "location": None,
        })

    return rows


def download(client, pdf_url: str, dest) -> None:
    """GET pdf_url and write bytes to dest. Raises on non-2xx."""
    resp = client.get(pdf_url, headers={"Referer": REFERER})
    resp.raise_for_status()
    with open(dest, "wb") as fh:
        fh.write(resp.content)
