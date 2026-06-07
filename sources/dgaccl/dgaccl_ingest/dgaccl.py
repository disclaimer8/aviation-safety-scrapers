# dgaccl_ingest/dgaccl.py
"""
DGAC Chile (Dirección General de Aeronáutica Civil, dgac.gob.cl) per-year
accident-report listing + staged-PDF preference parser.

The catalogue is a fixed set of per-year pages (NO hub to crawl):
    https://www.dgac.gob.cl/informes-2025/
                            /informes-2024/
                            /informe-2023/    ⚠️ SINGULAR slug for 2023
                            /informes-2022/
                            /informes-2021/
                            /informes-2020/
                            /informes-2019/
The slug list is HARDCODED (never constructed) because 2023 breaks the
'informes-{year}' pattern. Each page is a server-rendered <table class="table">
with columns: Suceso (case number, NNNN) | Fecha ('15 ENE 2024', Spanish month
abbrevs) | Tipo aeronave | Lugar | Estado (the cell that holds the PDF link(s)).

PDFs live under /wp-content/uploads/YYYY/MM/… , are SPANISH with clean text
layers (45-121K chars). ⚠️ Filenames are human-typed and drift:
'Informe-final' vs 'Informe-Final', suffixes -II/-1/i/a, and a report may be
published in stages: 'Informe-Preliminar-30-dias' → '-12-meses' → '-24-meses'
→ '-36-meses' → 'Informe-Final'. Per case we keep ONE href, preferring
Final > latest Preliminar-NN-meses > Preliminar(any) > 30-dias.

⚠️ Each year page also carries ~9 site-chrome PDFs (budget/policy/privacy)
that are NOT reports; we keep only hrefs whose filename mentions
Informe/Preliminar/Final OR embeds the case number.

⚠️ Registration (CC-XXX) is NOT in the listing — it lives only inside the PDF
text and is extracted best-effort (regex CC-[A-Z0-9]{2,4}); foreign-registered
aircraft legitimately yield None.

case_id = '{caseNumber}-{YY}' (e.g. '2044-24'); YY derived from the date /
year page. Collision suffix '-2', '-3', …
"""
import html as _html
import re
from urllib.parse import urljoin

BASE = "https://www.dgac.gob.cl"
DELAY = 1.5

# HARDCODED per-year pages (⚠️ 2023 is SINGULAR 'informe-2023'). Newest first.
YEAR_PAGES = [
    "https://www.dgac.gob.cl/informes-2025/",
    "https://www.dgac.gob.cl/informes-2024/",
    "https://www.dgac.gob.cl/informe-2023/",   # ⚠️ SINGULAR
    "https://www.dgac.gob.cl/informes-2022/",
    "https://www.dgac.gob.cl/informes-2021/",
    "https://www.dgac.gob.cl/informes-2020/",
    "https://www.dgac.gob.cl/informes-2019/",
]

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Accept-Language": "es-CL,es;q=0.9",
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
}

# Spanish month abbreviations → month number.
_MONTHS = {
    "ENE": 1, "FEB": 2, "MAR": 3, "ABR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AGO": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DIC": 12,
}
_DATE_RE = re.compile(r"(\d{1,2})\s+([A-Za-zÁÉÍÓÚ]{3})\s+(\d{4})")

_TR_RE = re.compile(r"(?is)<tr[>\s].*?</tr>")
_TD_RE = re.compile(r"(?is)<td[^>]*>(.*?)</td>")
_PDF_HREF_RE = re.compile(r'href="([^"]+\.pdf)"', re.IGNORECASE)
_CASE_NUM_RE = re.compile(r"\b(\d{3,4})\b")
# Registration inside the PDF text layer: CC-XXX (Chilean civil prefix).
_REG_RE = re.compile(r"\bCC-[A-Z0-9]{2,4}\b")
# Months-stage in a preliminar filename: -12-meses / -24-meses / -36-meses /
# -30-dias.
_STAGE_MESES_RE = re.compile(r"(\d{1,3})[\s_-]?meses", re.IGNORECASE)
_STAGE_DIAS_RE = re.compile(r"(\d{1,3})[\s_-]?d[ií]as", re.IGNORECASE)
_NONSLUG = re.compile(r"[^a-z0-9]+")


def _strip(fragment):
    text = re.sub(r"<[^>]+>", " ", fragment or "")
    text = _html.unescape(text).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def year_from_url(url):
    """The 4-digit year embedded in a year-page URL ('informe-2023' → '2023')."""
    m = re.search(r"informes?-(\d{4})", url or "")
    return m.group(1) if m else None


def parse_spanish_date(text):
    """
    '15 ENE 2024' → '2024-01-15' (ISO). Returns None when unparseable.
    Month abbrev is matched case-insensitively against ENE..DIC.
    """
    if not text:
        return None
    m = _DATE_RE.search(text)
    if not m:
        return None
    day, mon, year = m.group(1), m.group(2).upper()[:3], m.group(3)
    month = _MONTHS.get(mon)
    if not month:
        return None
    return f"{year}-{month:02d}-{int(day):02d}"


def _is_report_pdf(filename, case_number=None):
    """
    True when a PDF filename is a real accident report (not site chrome).
    Keep iff the filename mentions Informe/Preliminar/Final, OR embeds the
    case number from its row.
    """
    low = (filename or "").lower()
    if "informe" in low or "preliminar" in low or "final" in low:
        return True
    if case_number and case_number in (filename or ""):
        return True
    return False


def _stage_rank(filename):
    """
    Preference rank for staged PDFs of one case (higher wins):
        Final                       = 1000
        Preliminar-NN-meses         = 100 + NN   (latest stage wins)
        Preliminar-NN-dias          = 10  + NN/30 (early stage)
        Preliminar (unqualified)    = 50
    """
    low = (filename or "").lower()
    if "final" in low:
        return 1000
    m = _STAGE_MESES_RE.search(low)
    if m:
        return 100 + int(m.group(1))
    d = _STAGE_DIAS_RE.search(low)
    if d:
        return 10 + int(d.group(1)) / 30.0
    if "preliminar" in low:
        return 50
    return 0


def _report_kind(filename):
    low = (filename or "").lower()
    if "final" in low:
        return "Final"
    if "preliminar" in low:
        return "Preliminar"
    return None


def parse_year_page(year_html, year_url):
    """
    Parse one year page → list of case dicts in document order. Each dict:
        case_number, year, yy, event_date (ISO|None), aircraft, location,
        pdf_url (PREFERRED stage|None), filename, report_kind.
    Rows are the data <tr>s of <table class="table"> (the first <tr> is the
    header). A row with no usable report PDF still yields a dict with
    pdf_url=None (so its metadata is recorded), but the pipeline only inserts
    rows that have a pdf_url.

    When a row (or several rows sharing a case number) offers multiple report
    PDFs, the PREFERRED stage is chosen via _stage_rank.
    """
    year = year_from_url(year_url)
    yy = year[2:] if year else None
    out = []
    by_case = {}  # case_number → index into out (merge staged duplicates)

    for tr in _TR_RE.findall(year_html or ""):
        tds = _TD_RE.findall(tr)
        if len(tds) < 4:
            continue
        case_number = _strip(tds[0])
        if not re.fullmatch(r"\d{3,4}", case_number):
            continue  # header / non-data row

        event_date = parse_spanish_date(_strip(tds[1]))
        aircraft = _strip(tds[2]) or None
        location = _strip(tds[3]) or None

        # Collect this row's report PDFs (chrome filtered out).
        candidates = []
        for raw in _PDF_HREF_RE.findall(tr):
            href = _html.unescape(raw)
            filename = href.rsplit("/", 1)[-1]
            if filename.lower().endswith(".pdf"):
                fn_stem = filename[:-4]
            else:
                fn_stem = filename
            if not _is_report_pdf(filename, case_number):
                continue
            candidates.append((urljoin(BASE, href), fn_stem))

        best = None  # (rank, url, filename)
        for url, fn_stem in candidates:
            rank = _stage_rank(fn_stem)
            if best is None or rank > best[0]:
                best = (rank, url, fn_stem)

        rec = {
            "case_number": case_number,
            "year": year,
            "yy": yy,
            "event_date": event_date,
            "aircraft": aircraft,
            "location": location,
            "pdf_url": best[1] if best else None,
            "filename": best[2] if best else None,
            "report_kind": _report_kind(best[2]) if best else None,
        }

        if case_number in by_case:
            # Staged duplicate across rows: keep the higher-ranked PDF.
            idx = by_case[case_number]
            prev = out[idx]
            prev_rank = _stage_rank(prev["filename"]) if prev["pdf_url"] else -1
            new_rank = best[0] if best else -1
            if new_rank > prev_rank:
                out[idx] = rec
        else:
            by_case[case_number] = len(out)
            out.append(rec)

    return out


def make_case_id(case_number, yy, taken=None):
    """
    '{caseNumber}-{YY}' (e.g. '2044-24'). Collision suffix '-2', '-3', …
    guarantees uniqueness within `taken`.
    """
    base = f"{case_number}-{yy}" if yy else str(case_number)
    if taken is None:
        return base
    cand = base
    n = 2
    while cand in taken:
        cand = f"{base}-{n}"
        n += 1
    return cand


def extract_registration(text):
    """Best-effort CC- registration from PDF text; None when absent (foreign)."""
    m = _REG_RE.search(text or "")
    return m.group(0).upper() if m else None


# ──────────────────────────────────────────────────────────────────────────────
# HTTP helpers (live network; not exercised in offline tests)
# ──────────────────────────────────────────────────────────────────────────────


def fetch_page(client, url):
    resp = client.get(url)
    resp.raise_for_status()
    return resp.text


def download_pdf(client, url, dest_path):
    resp = client.get(url)
    resp.raise_for_status()
    with open(dest_path, "wb") as f:
        f.write(resp.content)
    return dest_path
