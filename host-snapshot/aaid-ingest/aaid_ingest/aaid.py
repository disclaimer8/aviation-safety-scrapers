# aaid_ingest/aaid.py
"""Kenya AAID (Aircraft Accident Investigation Department) HTML scraper.

Source: https://aaid.transport.go.ke/final-reports
- ONE server-rendered Drupal page (a Views table) listing every Final Report.
- Each <tr> has 5 cells:
    0 Counter
    1 "Reference Number"  -> actually a DD/MM/YYYY date (NOT a usable reference)
    2 "Date of Occurrence" -> <time datetime="YYYY-MM-DDThh:mm:ssZ"> ISO date
    3 "Aircraft Reg No."  -> e.g. 5Y-LOL, 5H-AAI, ZS-OYI, F-GHJE (foreign too)
    4 "Investigation Report Title" -> <a href="/sites/default/files/.../Final Report-XXX.pdf">
- ENGLISH text-layer PDFs.  narrative_text stays English.
- There is NO real report reference number, so case_id is INTRINSIC:
  registration + ISO event-date (order-independent, NO encounter-order suffix).

⚠️ TLS: the site serves an EXPIRED certificate (real eMudhra chain, notAfter
   2025-09-22).  We PIN the exact captured chain (aaid_ca_bundle.pem in this
   repo) AND disable ONLY the notBefore/notAfter time check via the OpenSSL
   X509_V_FLAG_NO_CHECK_TIME flag.  The cert must still chain to the pinned
   bundle and match the hostname, so this is NOT insecure blanket
   verify=False — an MITM presenting a different cert is still rejected.
   RE-CHECK each cycle: if the host renews to a valid cert the pin will keep
   working (still in the chain) until the leaf rotates, at which point
   make_ssl_context() will raise loudly and the bundle must be re-captured:
     openssl s_client -showcerts -connect aaid.transport.go.ke:443 \
       -servername aaid.transport.go.ke </dev/null > raw.txt
     awk '/BEGIN CERT/,/END CERT/' raw.txt > aaid_ca_bundle.pem
   The cycle MUST error loudly on pin failure, never silently fall back.
"""
import datetime
import html as _html
import re
import ssl
from pathlib import Path

BASE = "https://aaid.transport.go.ke"
INDEX_URL = BASE + "/final-reports"
REFERER = INDEX_URL
DELAY = 1.8  # throttle between requests (seconds)

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0 Safari/537.36"
)

HEADERS = {
    "User-Agent": UA,
    "Referer": REFERER,
}

# Path to the pinned CA bundle (captured chain), shipped IN THE REPO.
CA_BUNDLE = str(Path(__file__).resolve().parent.parent / "aaid_ca_bundle.pem")

# OpenSSL verify flag: ignore certificate notBefore/notAfter ONLY.
# https://www.openssl.org/docs/man1.1.1/man3/X509_VERIFY_PARAM_set_flags.html
_X509_V_FLAG_NO_CHECK_TIME = 0x200000


# ──────────────────────────────────────────────
# Compiled regexes
# ──────────────────────────────────────────────

# A single data row in the Views table.
_ROW_RE = re.compile(r"<tr>(.*?)</tr>", re.DOTALL)

# ISO datetime on the "Date of Occurrence" <time> element.
_DATETIME_RE = re.compile(r'datetime="(\d{4}-\d{2}-\d{2})')

# Aircraft Reg No. cell content.
_REG_CELL_RE = re.compile(
    r'air-craft-reg-no[^>]*>(.*?)</td>', re.DOTALL
)

# PDF anchor inside the Investigation Report Title cell.
_PDF_HREF_RE = re.compile(r'href="(/sites/default/files/[^"]+\.pdf)"', re.IGNORECASE)

# Anchor link text (the visible filename / title).
_PDF_TEXT_RE = re.compile(
    r'href="/sites/default/files/[^"]+\.pdf"[^>]*>(.*?)</a>', re.DOTALL | re.IGNORECASE
)

# Strip leading "Final Report" + separators from a title to leave the descriptive part.
_FINAL_REPORT_PREFIX_RE = re.compile(r"^final\s*report\s*[-:_ ]*", re.IGNORECASE)


# ──────────────────────────────────────────────
# SSL / client
# ──────────────────────────────────────────────

def make_ssl_context(ca_bundle: str = CA_BUNDLE) -> ssl.SSLContext:
    """
    Build an SSLContext that PINS the captured AAID chain (ca_bundle) and
    disables ONLY the certificate expiry (notBefore/notAfter) check.

    Hostname verification and chain-of-trust verification against the pinned
    bundle remain ENABLED, so a substituted/MITM certificate is still rejected.

    Raises loudly (ssl.SSLError / OSError) if the bundle cannot be loaded — the
    caller MUST NOT fall back to insecure verification.
    """
    ctx = ssl.create_default_context(cafile=ca_bundle)
    ctx.verify_flags |= _X509_V_FLAG_NO_CHECK_TIME
    return ctx


def make_client():
    """Return an httpx.Client pinned to the AAID chain (scoped to this host)."""
    import httpx
    return httpx.Client(
        headers=HEADERS,
        follow_redirects=True,
        timeout=60.0,
        verify=make_ssl_context(),
    )


# ──────────────────────────────────────────────
# case_id (intrinsic: registration + ISO event-date)
# ──────────────────────────────────────────────

def _normalize_case_id(case_id: str) -> str:
    """
    Canonicalise a case_id: uppercase, collapse any run of non-alnum chars to a
    single '-', strip leading/trailing '-'.  Order-independent and stable.
    """
    if not case_id:
        return ""
    s = re.sub(r"[^A-Za-z0-9]+", "-", case_id).strip("-")
    return s.upper()


def make_case_id(registration: str | None, event_date: str | None) -> str | None:
    """
    Build the INTRINSIC case_id from registration + ISO event-date.

    No encounter-order suffix: the key depends only on the row's own data, so
    discovery order does not matter and re-runs are idempotent.

    Returns None when neither registration nor event_date is available.
    """
    reg = (registration or "").strip()
    date = (event_date or "").strip()
    if not reg and not date:
        return None
    base = "-".join(p for p in (reg, date) if p)
    return _normalize_case_id(base)


# ──────────────────────────────────────────────
# Cell helpers
# ──────────────────────────────────────────────

def _cell_text(cell_html: str) -> str:
    """Strip tags + unescape entities + collapse whitespace from a cell body."""
    txt = re.sub(r"<[^>]+>", " ", cell_html)
    txt = _html.unescape(txt)
    return re.sub(r"\s+", " ", txt).strip()


def _title_from_pdf_text(pdf_text: str, registration: str | None) -> str:
    """
    Derive a human title from the PDF link text.

    The link text is typically 'Final Report-5Y-LOL.pdf'.  We strip the
    'Final Report' prefix and '.pdf' suffix; if nothing descriptive remains we
    fall back to a 'Final Report <reg>' style label.
    """
    t = (pdf_text or "").strip()
    t = re.sub(r"\.pdf$", "", t, flags=re.IGNORECASE)
    stripped = _FINAL_REPORT_PREFIX_RE.sub("", t).strip()
    if stripped:
        return f"Final Report {stripped}"
    if registration:
        return f"Final Report {registration}"
    return t or "Final Report"


# ──────────────────────────────────────────────
# Discovery / parsing
# ──────────────────────────────────────────────

def parse_listing(html: str, index_url: str = INDEX_URL) -> list[dict]:
    """
    Parse the AAID /final-reports table → list of report dicts.

    Each dict has:
      case_id            str   INTRINSIC reg+date, e.g. '5Y-LOL-2024-12-07'
      report_url         str   the listing page URL (no per-case detail page)
      pdf_url            str   absolute PDF URL (href taken EXACTLY, not built)
      pdf_url_es         None  (no Spanish; column kept for schema parity)
      pdf_url_en         str   same as pdf_url (English source)
      event_class        str   'Accident' (AAID publishes accident final reports)
      aircraft           None  (not in the listing; left to downstream/PDF)
      registration       str   e.g. '5Y-LOL'
      date_of_occurrence str|None  ISO YYYY-MM-DD from <time datetime>
      location           None  (not in the listing)
      title              str   derived from the PDF link text
      lang               str   'en'

    Rows without a parseable case_id (no reg AND no date) or no PDF href are
    skipped.  Hrefs are taken verbatim from the page (dash-spacing in filenames
    is inconsistent), only made absolute.
    """
    rows: list[dict] = []
    # Restrict to <tbody> so the <th> header row never produces a phantom row.
    tb_start = html.find("<tbody>")
    tb_end = html.find("</tbody>")
    scope = html[tb_start:tb_end] if (tb_start != -1 and tb_end != -1) else html

    seen: set[str] = set()
    for row_m in _ROW_RE.finditer(scope):
        row = row_m.group(1)

        href_m = _PDF_HREF_RE.search(row)
        if not href_m:
            continue  # header row or non-report row

        pdf_path = _html.unescape(href_m.group(1))
        pdf_url = BASE + pdf_path  # absolute; href taken EXACTLY (don't construct)

        dt_m = _DATETIME_RE.search(row)
        date_iso = dt_m.group(1) if dt_m else None

        reg_m = _REG_CELL_RE.search(row)
        registration = _cell_text(reg_m.group(1)) if reg_m else None
        if not registration:
            registration = None

        case_id = make_case_id(registration, date_iso)
        if not case_id:
            continue
        if case_id in seen:
            continue  # intrinsic key dedupe (order-independent)
        seen.add(case_id)

        text_m = _PDF_TEXT_RE.search(row)
        pdf_text = _cell_text(text_m.group(1)) if text_m else ""
        title = _title_from_pdf_text(pdf_text, registration)

        rows.append({
            "case_id": case_id,
            "report_url": index_url,
            "pdf_url": pdf_url,
            "pdf_url_es": None,
            "pdf_url_en": pdf_url,
            "event_class": "Accident",
            "aircraft": None,
            "registration": registration,
            "date_of_occurrence": date_iso,
            "location": None,
            "title": title,
            "lang": "en",
        })

    return rows


# ──────────────────────────────────────────────
# Download
# ──────────────────────────────────────────────

def download(client, pdf_url: str, dest: str | Path) -> None:
    """
    GET pdf_url (with Referer) over the pinned-cert client and write to dest.

    Raises httpx.HTTPStatusError on non-2xx.
    """
    resp = client.get(pdf_url, headers={"Referer": REFERER})
    resp.raise_for_status()
    with open(dest, "wb") as fh:
        fh.write(resp.content)
