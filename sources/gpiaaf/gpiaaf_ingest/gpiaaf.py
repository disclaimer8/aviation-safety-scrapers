# gpiaaf_ingest/gpiaaf.py
"""
GPIAAF Portugal (Gabinete de Prevenção e Investigação de Acidentes com
Aeronaves e de Acidentes Ferroviários, gpiaaf.gov.pt) — CIVIL AVIATION
investigation report scraper.

This is a FULL-BROWSER source. The site is a Nuxt 2 SPA whose backend API
params are encrypted client-side (NOT curl-able); ``window.__NUXT__`` carries
no list. But the SPA renders a clean HTML ``<table>`` per year — so we
DOM-scrape it with Playwright.

  * discover() drives a Playwright Chromium page: renders the aviation listing
    root → harvests decade/year drill-down links → renders each populated year
    page → parses its report table into rows.
  * fetch() RE-USES a browser session to follow each report's opaque ``?v=``
    Documento route. The SPA redirects it to a presigned S3 PDF URL with a
    60-second expiry, surfaced as a browser ``download`` event. We capture that
    and stream the PDF straight to disk (``download.save_as``), then pdftotext.
    ⚠️ Plain httpx on the ``?v=`` link returns the SPA shell — useless.

Part A — pure helpers (testable without a browser / network).
Part B — GpiaafBrowser: Playwright transport (lazy import so unit tests that
         never launch a browser still import this module).

⚠️ networkidle is NEVER used. Navigate with wait_until="domcontentloaded" then
   POLL the DOM for the rendered table with a hard ceiling. Cold-loading a year
   URL sometimes hits the SPA-router fallback and renders the HOMEPAGE instead
   of the year table — detect (missing table / wrong heading) and retry.
⚠️ The #cookie-banner intercepts clicks — we navigate by URL only, never click
   through a link.
"""

import re
from urllib.parse import urljoin, urlparse, parse_qs

BASE = "https://www.gpiaaf.gov.pt"
# Aviation-only listing root. Carries the decade/year drill-down links. We
# crawl /aviacao-civil-reservado/ paths EXCLUSIVELY (the site also covers rail).
LISTING_ROOT = (
    BASE
    + "/aviacao-civil-reservado/investigacao-de-acidentes-e-incidentes/"
    + "investigacoes-concluidas-relatorios-e-outros-documentos"
)
# Every aviation listing/detail URL contains this segment. Used to reject any
# rail (transporte-ferroviario) links the shared chrome might surface.
AVIATION_SEGMENT = "/aviacao-civil-reservado/"

#: Throttle (s) between row actions (SPA + S3 — be gentle).
DELAY = 1.5

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Accept-Language": "pt-PT,pt;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/pdf,*/*;q=0.8",
}

# ─────────────────────────────────────────────────────────────────────────────
# Part A — pure helpers
# ─────────────────────────────────────────────────────────────────────────────

# A year-page URL is the LISTING_ROOT plus /{decade}/{year}, where {decade} is
# a 'de-YYYY-a-YYYY' slug. ⚠️ The current decade slug ends '-2026', NOT '-2029'.
# We HARVEST these from the rendered root rather than constructing them; this
# matcher only recognises them (and a year-page must carry a 4-digit final
# segment, rejecting junk siblings like '.../de-2020-a-2026/teste').
_DECADE_RE = re.compile(r"/de-(\d{4})-a-(\d{4})/(\d{4})(?:[/?#].*)?$")


def is_year_url(url):
    """True iff ``url`` is an aviation per-year listing page
    (``…/{decade}/{year}`` with a 4-digit year)."""
    if not url or AVIATION_SEGMENT not in url:
        return False
    return _DECADE_RE.search(url) is not None


def year_from_url(url):
    """Return the 4-digit year of a year-page URL, or None."""
    m = _DECADE_RE.search(url or "")
    return m.group(3) if m else None


def decade_bounds(url):
    """Return (lo, hi) ints of the decade slug, or None. Used only to validate
    the year falls inside its decade (the '-2026' tail is intentional)."""
    m = _DECADE_RE.search(url or "")
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def harvest_year_urls(links):
    """From an iterable of hrefs (the rendered listing root) → ordered,
    de-duped list of aviation year-page URLs, NEWEST-year-first.

    ⚠️ Constructs nothing — the decade/year links (incl. the '-2026' tail) are
    taken verbatim from the page. Junk siblings without a 4-digit final segment
    (e.g. '.../de-2020-a-2026/teste') are rejected by ``is_year_url``; a year
    outside its own decade bounds is also dropped.
    """
    out = []
    seen = set()
    for href in links:
        if not href or href in seen:
            continue
        if not is_year_url(href):
            continue
        bounds = decade_bounds(href)
        y = int(year_from_url(href))
        if bounds and not (bounds[0] <= y <= bounds[1]):
            continue
        seen.add(href)
        out.append((y, href))
    out.sort(key=lambda t: -t[0])
    return [u for _, u in out]


# Table columns in render order.
COLUMNS = ("Data", "Classificacao", "Tipo", "Matricula", "Local",
           "Documento", "Identificacao")

# case_id shape in the 'Identificação do Processo' column: NN/ACCID/YYYY,
# NN/INCID/YYYY, plus seen variants like YYYY/ACCID/NN and YYYY/AVAL/NN.
_CASE_RE = re.compile(
    r"\b(\d{1,4})\s*/\s*(ACCID|INCID|AVAL|INCIDENTE|ACIDENTE)\s*/\s*(\d{1,4})\b",
    re.IGNORECASE,
)
# Bulletin-only rows publish the literal 'evento registado' (event logged) as
# their process id instead of a case number — they carry no real report.
_NO_PROCESS_TOKENS = ("evento registado",)

# A Documento link whose visible text / title is a quarterly bulletin is NOT a
# report. Detect it so a row whose ONLY document is a bulletin is kept as
# metadata (status no_report) and skipped for fetch.
_BULLETIN_RE = re.compile(r"boletim\s+de\s+divulga", re.IGNORECASE)
_REPORT_LABEL_RE = re.compile(r"relat[óo]rio", re.IGNORECASE)

# YYYY/MM/DD occurrence date in the Data column.
_DATE_RE = re.compile(r"\b(\d{4})/(\d{1,2})/(\d{1,2})\b")

# Portuguese registration CS-xxx; we also surface foreign regs best-effort.
_CS_REG_RE = re.compile(r"\bCS-[A-Z0-9]{2,4}\b")
_FOREIGN_REG_RE = re.compile(r"\b[A-Z]{1,2}-[A-Z0-9]{2,5}\b")
# 'Sem registo' (no registration) → None.
_SEM_REGISTO_RE = re.compile(r"sem\s+registo", re.IGNORECASE)

# Stable PDF identifier: dNNNNNN in the presigned S3 key
# (…/upload/processos/dNNNNNN.pdf) or in the Documento link's title attr.
_DNUM_RE = re.compile(r"\b(d\d{4,})\.pdf\b", re.IGNORECASE)


def normalize_case_id(raw):
    """Normalise an 'Identificação do Processo' value to a lowercase slug,
    or None when no case number is present (bulletin-only rows).

        '08/ACCID/2017'   → '08-accid-2017'
        '2022/AVAL/13'    → '2022-aval-13'
        'evento registado'→ None
    """
    if not raw:
        return None
    low = raw.strip().lower()
    if any(tok in low for tok in _NO_PROCESS_TOKENS):
        return None
    m = _CASE_RE.search(raw)
    if not m:
        return None
    a, mid, b = m.group(1), m.group(2).lower(), m.group(3)
    return f"{a}-{mid}-{b}".lower().replace("/", "-")


def parse_event_date(text):
    """'2017/10/05' → '2017-10-05' (ISO); None when unparseable."""
    if not text:
        return None
    m = _DATE_RE.search(text)
    if not m:
        return None
    y, mo, d = m.group(1), m.group(2), m.group(3)
    return f"{y}-{int(mo):02d}-{int(d):02d}"


def parse_registration(text):
    """Registration from the Matrícula cell. 'Sem registo' → None.

    Prefers a Portuguese CS- prefix, else a best-effort foreign reg. Multi-
    aircraft cells ('G-EZDX ; EI-EBD') keep the first match.
    """
    if not text:
        return None
    if _SEM_REGISTO_RE.search(text):
        return None
    m = _CS_REG_RE.search(text)
    if m:
        return m.group(0).upper()
    m = _FOREIGN_REG_RE.search(text)
    return m.group(0).upper() if m else None


def is_bulletin_label(text):
    """True iff a Documento link label/title is a quarterly bulletin (not a
    report)."""
    return bool(_BULLETIN_RE.search(text or ""))


def is_report_label(text):
    """True iff a Documento link label/title is a final report ('Relatório')."""
    return bool(_REPORT_LABEL_RE.search(text or ""))


def pick_report_doc(doc_links):
    """From a row's Documento links (each a dict ``{label, href, title}``) pick
    the FINAL REPORT link, or None when the row's only document is a bulletin.

    A report link's label is 'Relatório'; its ``title`` is the PDF filename
    (e.g. 'd055927.pdf' / '01ACCID2017_RF.pdf'). Bulletin links
    ('Boletim de Divulgação Trimestral …') are skipped.
    """
    for link in doc_links or []:
        label = (link.get("label") or "")
        title = (link.get("title") or "")
        if is_bulletin_label(label) or is_bulletin_label(title):
            continue
        if is_report_label(label) or not is_bulletin_label(label):
            # a non-bulletin link (almost always the 'Relatório') is the report
            if link.get("href"):
                return link
    return None


def pdf_id_from(*candidates):
    """Extract the stable dNNNNNN id from any of the given strings (the
    Documento link title, the suggested download filename, or the S3 url).
    Returns the bare id (no .pdf), or None."""
    for c in candidates:
        if not c:
            continue
        m = _DNUM_RE.search(c)
        if m:
            return m.group(1).lower()
    return None


def is_homepage_fallback(heading, has_table):
    """SPA-router fallback guard. A correctly rendered year page shows the
    4-digit year as its <h1> AND has the report table. When the cold-load
    misfires we land on the HOMEPAGE — no table and/or a non-year heading.
    Returns True when the render looks like the fallback (caller should retry).
    """
    if not has_table:
        return True
    h = (heading or "").strip()
    return not re.fullmatch(r"\d{4}", h or "")


def parse_year_rows(table_rows, year=None):
    """Parse rendered table rows into report dicts.

    ``table_rows`` is a list of rows; each row is a list of CELLS. The FIRST
    row is the header (``Data | Classificação | … | Identificação do
    Processo``) and is skipped. Each data cell is a dict with::

        {"text": <innerText>, "links": [{"label","href","title"}, ...]}

    (the Documento cell carries links; others usually carry just text).

    Returns a list of dicts with keys: case_id, event_date, classification,
    aircraft, registration, location, doc_url, doc_title, pdf_id, has_report.
    Rows whose only Documento is a bulletin get has_report=False (status
    no_report downstream); rows with no case number AND no report are dropped.
    """
    out = []
    for row in table_rows or []:
        cells = list(row or [])
        if len(cells) < len(COLUMNS):
            continue
        # header detection: first cell text == 'Data'
        c_text = [(_cell_text(c)) for c in cells]
        if c_text[0].strip().lower() == "data":
            continue

        data, classif, tipo, matricula, local = c_text[:5]
        proc = c_text[6] if len(c_text) > 6 else ""
        doc_links = _cell_links(cells[5]) if len(cells) > 5 else []

        case_id = normalize_case_id(proc)
        report = pick_report_doc(doc_links)
        has_report = report is not None

        # Keep a row if it carries a real case number, a report, OR any
        # Documento link (a bulletin-only row IS kept as `no_report` metadata
        # per the brief). Drop only truly empty rows (no case, no documents,
        # and no occurrence date) — pure render noise.
        has_any_doc = bool(doc_links)
        if not case_id and not has_report and not has_any_doc \
                and not parse_event_date(data):
            continue

        doc_url = report.get("href") if report else None
        doc_title = report.get("title") if report else None
        pid = pdf_id_from(doc_title, doc_url) if report else None

        out.append({
            "case_id": case_id,
            "event_date": parse_event_date(data),
            "classification": (classif or "").strip() or None,
            "aircraft": (tipo or "").strip() or None,
            "registration": parse_registration(matricula),
            "location": (local or "").strip() or None,
            "doc_url": doc_url,
            "doc_title": doc_title,
            "pdf_id": pid,
            "has_report": has_report,
            "year": year,
        })
    return out


def _cell_text(cell):
    if isinstance(cell, dict):
        return cell.get("text") or ""
    return cell or ""


def _cell_links(cell):
    if isinstance(cell, dict):
        return cell.get("links") or []
    return []


def fallback_case_id(doc_url, pdf_id=None):
    """Deterministic case_id when no process number is published but a report
    PDF exists. Prefer the stable d-number; else hash the doc route."""
    if pdf_id:
        return f"gpiaaf-{pdf_id}"
    import hashlib
    path = urlparse(doc_url or "").path or (doc_url or "")
    # include the ?v= query so distinct documents on the same year page differ
    q = urlparse(doc_url or "").query
    h = hashlib.sha1((path + "?" + q).encode("utf-8")).hexdigest()[:8]
    return f"gpiaaf-{h}"


def extract_registration(text):
    """Best-effort CS- registration from narrative/PDF text; None if absent."""
    m = _CS_REG_RE.search(text or "")
    return m.group(0).upper() if m else None


# ─────────────────────────────────────────────────────────────────────────────
# Part B — Playwright transport (lazy import; not exercised in offline tests)
# ─────────────────────────────────────────────────────────────────────────────

class GpiaafBrowser:
    """Playwright Chromium that renders the Nuxt SPA listings + captures the
    presigned-S3 report PDFs.

    No anti-bot was observed (the scout ran headless on Mac with a real UA);
    the mini-PC runs it under ``xvfb-run`` for parity with our other browser
    sources. ``headless`` can be forced off via the CLI flag / ``GPIAAF_HEADED``
    env if a fingerprint block ever appears.

    Usage::

        with GpiaafBrowser(headless=True) as br:
            year_urls = br.harvest_year_urls()        # from the root page
            rows = br.get_year_rows(year_url, "2017")  # parsed report rows
            s3, dnum = br.capture_pdf(doc_url, dest)   # follow ?v= → save PDF
    """

    #: Hard ceiling (s) for polling a rendered page for the expected table.
    RENDER_TIMEOUT_S = 14
    #: Seconds between poll iterations while waiting for the table to render.
    POLL_S = 1.0
    #: How many times to retry a year page that rendered the homepage fallback.
    FALLBACK_RETRIES = 3
    #: Download capture ceiling (ms) — presigned S3 url expires in 60 s, so the
    #: download must fire well within that.
    DOWNLOAD_TIMEOUT_MS = 30_000

    def __init__(self, headless=True, user_data_dir=None):
        self._headless = headless
        self._user_data_dir = user_data_dir
        self._pw = None
        self._context = None
        self._page = None

    # -- lifecycle ------------------------------------------------------------

    def start(self):
        import tempfile
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().__enter__()
        if self._user_data_dir is None:
            self._user_data_dir = tempfile.mkdtemp(prefix="gpiaaf_chrome_")
        self._context = self._pw.chromium.launch_persistent_context(
            self._user_data_dir,
            headless=self._headless,
            user_agent=UA,
            locale="pt-PT",
            accept_downloads=True,
            args=["--disable-dev-shm-usage"],
        )
        self._page = (
            self._context.pages[0]
            if self._context.pages
            else self._context.new_page()
        )
        # Warm up the homepage so the SPA router/session is primed before we
        # start cold-loading year URLs (reduces the homepage-fallback misfire).
        try:
            self._page.goto(BASE + "/", wait_until="domcontentloaded",
                            timeout=60_000)
        except Exception:
            pass

    def stop(self):
        if self._context:
            try:
                self._context.close()
            except Exception:
                pass
            self._context = None
        if self._pw:
            try:
                self._pw.__exit__(None, None, None)
            except Exception:
                pass
            self._pw = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *_):
        self.stop()

    # -- rendering primitives -------------------------------------------------

    def _goto(self, url):
        # ⚠️ NEVER networkidle (Nuxt keeps connections open). domcontentloaded
        # then poll the DOM for the expected nodes.
        self._page.goto(url, wait_until="domcontentloaded", timeout=60_000)

    # -- public API -----------------------------------------------------------

    def harvest_year_urls(self):
        """Render the aviation listing root → ordered year-page URLs."""
        import time

        self._goto(LISTING_ROOT)
        js = (
            "() => Array.from(document.querySelectorAll('a[href]'))"
            ".map(a => a.href)"
        )
        deadline = time.monotonic() + self.RENDER_TIMEOUT_S
        urls = []
        while time.monotonic() < deadline:
            hrefs = self._page.evaluate(js)
            urls = harvest_year_urls(hrefs)
            if urls:
                break
            time.sleep(self.POLL_S)
        return urls

    def _read_table(self):
        """Evaluate the rendered page → (heading, table_rows). table_rows is a
        list of rows, each a list of cell dicts {text, links}. The site nests
        the real data table INSIDE a ``table.table-bordered`` wrapper, so we
        pick the INNERMOST table that has a 'Data' header cell."""
        return self._page.evaluate(
            """() => {
              const h = document.querySelector('h1');
              const heading = h ? h.innerText.trim() : '';
              // innermost tables (no nested <table> inside)
              const tables = Array.from(document.querySelectorAll('table'))
                .filter(t => !t.querySelector('table'));
              let chosen = null;
              for (const t of tables) {
                const first = t.querySelector('tr');
                if (first && /(^|\\s)Data(\\s|$)/.test(first.innerText)) {
                  chosen = t; break;
                }
              }
              if (!chosen) return {heading, rows: []};
              const rows = Array.from(chosen.querySelectorAll('tr')).map(tr =>
                Array.from(tr.querySelectorAll('td,th')).map(td => ({
                  text: td.innerText.trim(),
                  links: Array.from(td.querySelectorAll('a[href]')).map(a => ({
                    label: a.innerText.trim(),
                    href: a.href,
                    title: a.getAttribute('title') || ''
                  }))
                }))
              );
              return {heading, rows};
            }"""
        )

    def get_year_rows(self, year_url, year=None):
        """Render a year page → parsed report rows. Detects + retries the
        homepage-router fallback, and gracefully returns [] for empty/
        unpopulated years (no table)."""
        import time

        year = year or year_from_url(year_url)
        for attempt in range(self.FALLBACK_RETRIES):
            self._goto(year_url)
            deadline = time.monotonic() + self.RENDER_TIMEOUT_S
            data = {"heading": "", "rows": []}
            while time.monotonic() < deadline:
                data = self._read_table()
                if data.get("rows"):
                    break
                # an empty (populated-but-rendering) page: keep polling. A truly
                # empty year has no table and the heading IS the year — bail.
                if (not is_homepage_fallback(data.get("heading"), False)
                        and data.get("heading", "").strip() == str(year)):
                    # correct year heading, simply no rows → empty year
                    return []
                time.sleep(self.POLL_S)

            has_table = bool(data.get("rows"))
            if not is_homepage_fallback(data.get("heading"), has_table):
                return parse_year_rows(data["rows"], year)
            # homepage fallback — re-navigate from the listing root then retry
            if attempt < self.FALLBACK_RETRIES - 1:
                try:
                    self._goto(LISTING_ROOT)
                except Exception:
                    pass
                time.sleep(self.POLL_S)
        return parse_year_rows(data.get("rows", []), year)

    def capture_pdf(self, doc_url, dest_path):
        """Follow a report's opaque ``?v=`` Documento route IN-SESSION; the SPA
        redirects to a presigned S3 PDF surfaced as a browser ``download``
        event. Capture it and stream to ``dest_path`` (well within the 60-second
        S3 expiry). Returns (s3_url, pdf_id).

        ⚠️ The presigned URL expires in 60 s — we save immediately from the live
        download object rather than re-fetching it.
        """
        with self._page.expect_download(
            timeout=self.DOWNLOAD_TIMEOUT_MS
        ) as di:
            # assign location in-page (the SPA route handler kicks off the
            # presign + download). We do NOT click the anchor (cookie-banner
            # intercepts clicks).
            self._page.evaluate("(h) => { window.location.href = h; }", doc_url)
        download = di.value
        download.save_as(dest_path)
        s3_url = download.url
        pid = pdf_id_from(download.suggested_filename, s3_url)
        return s3_url, pid
