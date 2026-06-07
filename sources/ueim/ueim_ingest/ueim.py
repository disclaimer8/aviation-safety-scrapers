# ueim_ingest/ueim.py
"""
UEIM Turkey (Ulaşım Emniyeti İnceleme Merkezi — Turkish Transport Safety
Investigation Center, aviation section; successor of KAIK) listing + report-PDF
parser.

The catalogue is ONE server-rendered listing page (no pagination, no hub to
crawl). Next.js SSR behind nginx + Google PageSpeed — the report links ARE in
the raw curl HTML (no JS needed):

    Turkish (canonical, richest):  https://ulasimemniyeti.uab.gov.tr/hava-araci
    English (secondary, sparser):  https://ulasimemniyeti.uab.gov.tr/en/aircraft

The TR page is canonical. The EN page (when it carries report links at all) is
only used to ADD PDFs not already seen on the TR page (those get lang='en');
everything is deduped by PDF URL.

The page is a set of server-rendered tables whose rows carry the metadata in
labelled cells (the same `aria-label` set repeats across each paginated table):

    KAZA TARİHİ   (accident date, DD.MM.YYYY)  → event_date
    TESCİL İŞARETİ(registration, e.g. TC-ERA)  → registration
    KAZA YERİ     (accident location)          → location
    KAZA TÜRÜ     (occurrence type KAZA/…)     → (kept in title only)
    RAPOR TARİHİ  (report date + the PDF <a>)  → pdf_url

⚠️ There is NO aircraft-type column in the listing — aircraft stays None unless
best-effort recovered from the PDF text downstream.

PDFs live under a FLAT path:
    https://ulasimemniyeti.uab.gov.tr/uploads/pages/hava-araci/{slug}.pdf
Hrefs are HARVESTED from the page, never constructed. PDFs are TURKISH with
clean text layers.

⚠️ Filename suffixes are inconsistent and carry the report type:
    -nihai-rapor / -nihai-raporu / -nihai / -final-raporu /
    -nihai-rapor-karar-sayili / …-doc-1   → FINAL report
    -on-rapor                              → PRELIMINARY report
    (no keyword, e.g. tc-cck.pdf)          → UNKNOWN
report_type is derived from those filename keywords ('final'/'preliminary'/
'unknown').

⚠️ Registration is encoded as the filename PREFIX (also present in the listing's
TESCİL İŞARETİ cell): `tc-ajc-…` → TC-AJC. Foreign-registered aircraft appear
too (`9h-dfs` → 9H-DFS, `ep-mnp` → EP-MNP). Extracted via a country-prefix
regex and uppercased-with-dash; best-effort re-verified from the PDF text.

case_id = the PDF slug (filename stem, e.g.
'tc-ajc-hava-araci-kazasi-nihai-raporuu') — unique + permanent in the uploads
path. ⚠️ The SAME registration can appear on two DIFFERENT accidents (e.g.
TC-ERA: 2023 ISPARTA vs 2021), each with its own slug — so dedupe by PDF URL /
slug, NEVER by registration.
"""
import html as _html
import re
from urllib.parse import urljoin

BASE = "https://ulasimemniyeti.uab.gov.tr"
TR_LISTING = "https://ulasimemniyeti.uab.gov.tr/hava-araci"
EN_LISTING = "https://ulasimemniyeti.uab.gov.tr/en/aircraft"
DELAY = 1.0

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
}

# Only PDFs under this flat uploads path are real reports; anything else
# (static site chrome, policy PDFs) is ignored.
_REPORT_PATH = "/uploads/pages/hava-araci/"

_TR_RE = re.compile(r"(?is)<tr[>\s].*?</tr>")
# Labelled cell: <td aria-label="KAZA TARİHİ">27.02.2023</td>
_CELL_RE = re.compile(
    r'(?is)<td[^>]*aria-label="([^"]*)"[^>]*>(.*?)</td>'
)
_PDF_HREF_RE = re.compile(
    r'href="([^"]+/uploads/pages/hava-araci/[^"]+\.pdf)"', re.IGNORECASE
)

# DD.MM.YYYY numeric date (the table form).
_NUM_DATE_RE = re.compile(r"\b(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{4})\b")
# Turkish month-name date ('15 Şubat 2023') — fallback if a textual date shows.
_TR_NAME_DATE_RE = re.compile(
    r"\b(\d{1,2})\s+([A-Za-zÇĞİÖŞÜçğıöşü]+)\s+(\d{4})\b"
)
_TR_MONTHS = {
    "ocak": 1, "subat": 2, "şubat": 2, "mart": 3, "nisan": 4,
    "mayis": 5, "mayıs": 5, "haziran": 6, "temmuz": 7,
    "agustos": 8, "ağustos": 8, "eylul": 9, "eylül": 9,
    "ekim": 10, "kasim": 11, "kasım": 11, "aralik": 12, "aralık": 12,
}
_TR_FOLD = str.maketrans("çğıöşüÇĞİÖŞÜ", "cgiosuCGIOSU")

# Registration as a filename prefix: 1-2 char country prefix + dash + 3-4 alnum.
# Anchored at the start of the slug so a secondary reg later in the name
# (e.g. tc-jmm-HL-7792-…) does NOT win over the primary prefix.
_REG_PREFIX_RE = re.compile(r"^([a-z0-9]{1,2})-([a-z0-9]{3,4})\b", re.IGNORECASE)
# Turkish-civil registration inside the PDF text layer (best-effort verify).
_REG_TEXT_RE = re.compile(r"\bTC-[A-Z]{3}\b")

# Filename keywords → report type. 'on-rapor' (preliminary) is checked BEFORE
# the final keywords so '…-on-rapor' is never mis-read as a final via 'rapor'.
_PRELIM_KEYS = ("on-rapor", "ön-rapor", "on-raporu")
_FINAL_KEYS = ("nihai", "final")


def _strip(fragment):
    text = re.sub(r"<[^>]+>", " ", fragment or "")
    text = _html.unescape(text).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def slug_from_url(pdf_url):
    """The filename stem of a report PDF URL → case_id."""
    if not pdf_url:
        return None
    name = pdf_url.rsplit("/", 1)[-1]
    if name.lower().endswith(".pdf"):
        name = name[:-4]
    return name or None


def report_type_from_filename(slug):
    """
    'final' / 'preliminary' / 'unknown' from the filename keywords.
    ⚠️ 'on-rapor' (preliminary) must be detected before the final keywords,
    because the substring 'rapor' is shared by both.
    """
    low = (slug or "").lower()
    if any(k in low for k in _PRELIM_KEYS):
        return "preliminary"
    if any(k in low for k in _FINAL_KEYS):
        return "final"
    return "unknown"


def registration_from_slug(slug):
    """
    Registration encoded as the slug PREFIX → 'TC-AJC' / '9H-DFS' / 'EP-MNP'.
    Returns uppercase-with-dash, or None when the prefix isn't reg-shaped.
    """
    m = _REG_PREFIX_RE.match(slug or "")
    if not m:
        return None
    return f"{m.group(1).upper()}-{m.group(2).upper()}"


def parse_date(text):
    """
    'DD.MM.YYYY' → ISO 'YYYY-MM-DD'. Falls back to a Turkish month-name date
    ('15 Şubat 2023'). Returns None when unparseable.
    """
    if not text:
        return None
    m = _NUM_DATE_RE.search(text)
    if m:
        day, mon, year = int(m.group(1)), int(m.group(2)), m.group(3)
        if 1 <= mon <= 12 and 1 <= day <= 31:
            return f"{year}-{mon:02d}-{day:02d}"
    n = _TR_NAME_DATE_RE.search(text)
    if n:
        day = int(n.group(1))
        mon = _TR_MONTHS.get(n.group(2).translate(_TR_FOLD).lower())
        year = n.group(3)
        if mon and 1 <= day <= 31:
            return f"{year}-{mon:02d}-{day:02d}"
    return None


def extract_registration_from_text(text):
    """Best-effort TC- registration from the PDF text layer; None if absent."""
    m = _REG_TEXT_RE.search(text or "")
    return m.group(0).upper() if m else None


def parse_listing(page_html, page_url, lang="tr"):
    """
    Parse a listing page → list of report dicts in document order. Each dict:
        case_id (slug), pdf_url, page_url, lang, report_type, registration,
        event_date (ISO|None), location, title.

    Rows are the table <tr>s whose labelled cells (`aria-label`) hold the
    accident metadata; the report PDF href is harvested from the row. Rows
    without a report PDF (e.g. 'report pending') are skipped. Site-chrome PDFs
    (outside /uploads/pages/hava-araci/) never match and are dropped.
    """
    out = []
    seen = set()
    for tr in _TR_RE.findall(page_html or ""):
        hrefs = _PDF_HREF_RE.findall(tr)
        if not hrefs:
            continue
        pdf_url = urljoin(BASE, _html.unescape(hrefs[0]))
        if pdf_url in seen:
            continue
        seen.add(pdf_url)

        cells = {label.strip(): _strip(val)
                 for label, val in _CELL_RE.findall(tr)}
        slug = slug_from_url(pdf_url)
        # Registration: prefer the listing's TESCİL İŞARETİ cell, fall back to
        # the slug prefix.
        reg_cell = cells.get("TESCİL İŞARETİ") or ""
        registration = reg_cell.upper().strip() or registration_from_slug(slug)
        location = cells.get("KAZA YERİ") or None
        kind = cells.get("KAZA TÜRÜ") or None
        event_date = parse_date(cells.get("KAZA TARİHİ"))

        title_bits = [b for b in (registration, location, kind) if b]
        title = " — ".join(title_bits) if title_bits else slug

        out.append({
            "case_id": slug,
            "pdf_url": pdf_url,
            "page_url": page_url,
            "lang": lang,
            "report_type": report_type_from_filename(slug),
            "registration": registration,
            "event_date": event_date,
            "location": location,
            "title": title,
        })
    return out


# ──────────────────────────────────────────────────────────────────────────────
# HTTP helpers (live network; not exercised in offline tests)
# ──────────────────────────────────────────────────────────────────────────────


def fetch_page(client, url, retries=2):
    """GET a page; retry on transient connection blips (observed on burst)."""
    last = None
    for attempt in range(retries + 1):
        try:
            resp = client.get(url)
            resp.raise_for_status()
            return resp.text
        except Exception as e:  # noqa: BLE001
            last = e
            if attempt < retries:
                import time
                time.sleep(DELAY * (attempt + 1))
    raise last


def download_pdf(client, url, dest_path, retries=2):
    last = None
    for attempt in range(retries + 1):
        try:
            resp = client.get(url)
            resp.raise_for_status()
            with open(dest_path, "wb") as f:
                f.write(resp.content)
            return dest_path
        except Exception as e:  # noqa: BLE001
            last = e
            if attempt < retries:
                import time
                time.sleep(DELAY * (attempt + 1))
    raise last
