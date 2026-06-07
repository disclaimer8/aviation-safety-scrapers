# otkes_ingest/otkes.py
"""
OTKES Finland (Onnettomuustutkintakeskus / Safety Investigation Authority,
turvallisuustutkinta.fi) — aviation investigation report scraper.

This is a BROWSER-RENDER source. The CMS (a Vue SPA backed by ``.srv``
polling) injects the report list AND the labelled detail metadata client-side;
they are absent from raw httpx HTML. So:

  * discover() drives a Playwright Chromium page to render each year/topic
    listing and harvest the report DETAIL urls (children of a year-page path).
  * fetch() renders each detail page (metadata + Finnish summary + PDF href are
    all render-injected) AND, when a report PDF is present, downloads it with
    PLAIN httpx (PDFs are static files, no cookies / no JS needed).

Part A — pure helpers (testable without a browser).
Part B — OtkesBrowser: Playwright transport (lazy import so unit tests that
         never launch a browser still import this module).

⚠️ networkidle NEVER settles on this CMS (continuous .srv polling). Always
   navigate with wait_until="domcontentloaded" then POLL the DOM for the
   expected nodes (report links / metadata labels) with a hard ceiling.
"""

import hashlib
import re
from urllib.parse import urljoin, urlparse

BASE = "https://turvallisuustutkinta.fi"
# Aviation root — carries the year-page + topic-page links (and, because it is
# the shared "tutkintaselostukset" hub, also OTHER transport modes; we keep
# ONLY aviation links, see AVIATION_SEGMENT).
ROOT_URL = (
    BASE
    + "/fi/index/tutkintaselostukset/ilmailuonnettomuuksientutkinta.html"
)
# Every aviation listing/detail URL contains this path segment. Used to reject
# the rail/marine year pages the root page also links to.
AVIATION_SEGMENT = "/ilmailuonnettomuuksientutkinta/"

DELAY = 1.0

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Accept-Language": "fi,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/pdf,*/*;q=0.8",
}

# ─────────────────────────────────────────────────────────────────────────────
# Part A — pure helpers
# ─────────────────────────────────────────────────────────────────────────────

# A year-page URL is the per-year listing. Two URL shapes exist:
#   ≥2014:  .../tutkintaselostuksetvuosittain/{year}.html        (or {year}_N)
#   ≤2013:  .../tutkintaselostuksetvuosittain/ilmailu{year}.html
# ⚠️ Some years carry a suffix (2023 → 2023_1.html). We NEVER construct these —
# they are harvested from the rendered root page; this matcher only recognises
# them.
_YEAR_PAGE_RE = re.compile(
    r"/tutkintaselostuksetvuosittain/"
    r"(?:ilmailu)?(\d{4})(?:_\d+)?\.html$",
    re.IGNORECASE,
)

# Topic / collection listing pages (also enumerated from the root). These hold
# more detail links and must be walked too.
_TOPIC_KEYWORDS = (
    "teematutkinnat",
    "vanhemmattutkinnat",
    "liikenneilmailu",
    "liikelennot",
    "sotilasilmailu",
    "kuumailmapallot",
)

# ``Tutkintanumero`` (case number) shapes seen in the wild:
#   modern   L2024-01, B2010-01            ({Letter}{YYYY}-{NN})
#   legacy   C9/2003L, B 4/1996, C12/2003  ({Letter}{NN}/{YYYY}[{Letter}])
# We normalise the recognised forms to a compact lowercase id (l2024-01,
# c2003-09). The legacy form carries an OPTIONAL trailing class letter
# (C9/2003L) which we drop. Order matters: try modern first.
_CASE_MODERN_RE = re.compile(r"([A-Z])\s?(\d{4})-(\d{1,3})\b")
_CASE_LEGACY_RE = re.compile(r"([A-Z])\s?(\d{1,3})\s?/\s?(\d{4})\s?[A-Z]?")

# Finnish occurrence date on the detail page: DD.MM.YYYY.
_FI_DATE_RE = re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b")

# Finnish aircraft registration (Helsinki prefix OH-, military/foreign best
# effort). OH-XXX is the only national prefix.
_REG_RE = re.compile(r"\bOH-[A-Z0-9]{2,4}\b")

# Detail-page metadata labels (rendered innerText) → field name. Values follow
# on the next non-empty line.
_DETAIL_LABELS = {
    "Tutkintanumero": "case_number",
    "Onnettomuustyyppi": "occurrence_type",
    "Onnettomuuspäivä": "event_date_raw",
    "Julkaisupäivä": "publish_date_raw",
}


def year_from_year_url(url):
    """Return the 4-digit year of a year-page URL, or None if not one."""
    m = _YEAR_PAGE_RE.search(url or "")
    return m.group(1) if m else None


def is_year_page(url):
    """True iff ``url`` is an aviation per-year listing page."""
    if not url or AVIATION_SEGMENT not in url:
        return False
    return _YEAR_PAGE_RE.search(url) is not None


def is_topic_page(url):
    """True iff ``url`` is an aviation topic/collection listing page."""
    if not url or AVIATION_SEGMENT not in url:
        return False
    low = url.lower()
    return any(k in low for k in _TOPIC_KEYWORDS)


def harvest_listing_urls(links):
    """From an iterable of hrefs (rendered root page) → ordered, de-duped list
    of aviation year-page + topic-page listing URLs.

    Newest-year-first within the year group; topic pages appended after.
    ⚠️ Rejects rail/marine year pages (no AVIATION_SEGMENT) and constructs
    nothing — suffix variants like 2023_1.html are taken verbatim.
    """
    years = []   # (year_int, url)
    topics = []
    seen = set()
    for href in links:
        if not href or href in seen:
            continue
        if is_year_page(href):
            seen.add(href)
            y = year_from_year_url(href)
            years.append((int(y) if y else 0, href))
        elif is_topic_page(href):
            seen.add(href)
            topics.append(href)
    years.sort(key=lambda t: -t[0])
    return [u for _, u in years] + topics


# A report DETAIL url is a link nested ONE level under any year/topic folder of
# the vuosittain path: .../tutkintaselostuksetvuosittain/{folder}/{slug}.html
#   ≥2014:  .../2024/{slug}.html
#   ≤2013:  .../ilmailu2003/{slug}.html
#   ⚠️ a year page may aggregate another year's reports under a suffixed folder
#       (2022's reports live under .../2023_1/l2022-_1.html), so we do NOT pin
#       the folder to the listing's own year — we accept any nested child.
_DETAIL_RE = re.compile(
    r"/tutkintaselostuksetvuosittain/[^/]+/[^/]+\.html$",
    re.IGNORECASE,
)
# Folders that are themselves listing pages, not report slugs (guards against a
# nested listing being mistaken for a detail).
_NOT_DETAIL_SLUGS = ("tutkintaselostuksetvuosittain.html",)


def is_detail_url(url, year=None):
    """True iff ``url`` is a report DETAIL page (a slug nested under any year/
    topic folder of the vuosittain path). ``year`` is accepted for API
    symmetry but NOT required to match (older pages aggregate cross-year)."""
    if not url or AVIATION_SEGMENT not in url:
        return False
    if any(url.endswith(s) for s in _NOT_DETAIL_SLUGS):
        return False
    return _DETAIL_RE.search(url) is not None


def harvest_detail_urls(links, year=None):
    """From rendered year-page hrefs → ordered de-duped report DETAIL urls."""
    out = []
    seen = set()
    for href in links:
        if href and href not in seen and is_detail_url(href, year):
            seen.add(href)
            out.append(href)
    return out


def normalize_case_number(raw):
    """Normalise a ``Tutkintanumero`` string to a compact lowercase case_id,
    or None when no recognised pattern is present.

        'L2024-01'   → 'l2024-01'
        'B 4/1996'   → 'b1996-04'   (legacy NN/YYYY → {letter}{year}-{NN})
        'C12/2003'   → 'c2003-12'
    """
    if not raw:
        return None
    s = raw.strip()
    m = _CASE_MODERN_RE.search(s)
    if m:
        letter, year, num = m.group(1), m.group(2), m.group(3)
        return f"{letter.lower()}{year}-{int(num):02d}"
    m = _CASE_LEGACY_RE.search(s)
    if m:
        letter, num, year = m.group(1), m.group(2), m.group(3)
        return f"{letter.lower()}{year}-{int(num):02d}"
    return None


def fallback_case_id(detail_url):
    """Deterministic case_id when no Tutkintanumero is published (lighter
    'selvitys' reports). 'otkes-{8-hex of the detail-url path}'."""
    path = urlparse(detail_url or "").path or (detail_url or "")
    h = hashlib.sha1(path.encode("utf-8")).hexdigest()[:8]
    return f"otkes-{h}"


def parse_fi_date(text):
    """'19.07.2024' → '2024-07-19' (ISO); None when unparseable."""
    if not text:
        return None
    m = _FI_DATE_RE.search(text)
    if not m:
        return None
    d, mo, y = m.group(1), m.group(2), m.group(3)
    return f"{y}-{int(mo):02d}-{int(d):02d}"


def parse_detail_text(inner_text):
    """Parse a rendered detail page's innerText into a metadata dict.

    The metadata block is rendered as ``Label:`` lines each followed by their
    value on the next non-empty line (an empty value line when absent, e.g. a
    'selvitys' with no Tutkintanumero). Everything after the last labelled
    field is the multi-paragraph Finnish summary.

    Returns dict with keys: case_number, occurrence_type, event_date (ISO),
    publish_date (ISO), summary.
    """
    lines = [ln.strip() for ln in (inner_text or "").splitlines()]
    fields = {}
    last_label_idx = -1
    i = 0
    while i < len(lines):
        line = lines[i]
        label = line[:-1].strip() if line.endswith(":") else None
        field = _DETAIL_LABELS.get(label) if label else None
        if field:
            # value = next non-empty line that is not itself a label
            value = ""
            j = i + 1
            while j < len(lines):
                nxt = lines[j]
                if nxt == "":
                    j += 1
                    continue
                if nxt.endswith(":") and nxt[:-1].strip() in _DETAIL_LABELS:
                    break  # empty value (e.g. blank Tutkintanumero)
                if nxt.endswith(":") and nxt[:-1].strip() == "Tutkinnan aloituspäivä":
                    break
                value = nxt
                break
            fields[field] = value or None
            last_label_idx = max(last_label_idx, j if value else i)
        i += 1

    # Summary: everything after the metadata block. We anchor on the last
    # labelled field's value line; skip the known trailing 'Tutkinnan
    # aloituspäivä' label+value pair if present.
    summary_lines = []
    started = last_label_idx >= 0
    k = last_label_idx + 1 if started else 0
    skip_aloitus = 0
    while k < len(lines):
        ln = lines[k]
        if skip_aloitus == 0 and ln.rstrip(":").strip() == "Tutkinnan aloituspäivä":
            skip_aloitus = 2  # skip this label and its value line
        if skip_aloitus > 0:
            skip_aloitus -= 1
            k += 1
            continue
        summary_lines.append(ln)
        k += 1
    summary = "\n".join(l for l in summary_lines).strip()
    summary = re.sub(r"\n{3,}", "\n\n", summary)

    return {
        "case_number": fields.get("case_number"),
        "occurrence_type": fields.get("occurrence_type"),
        "event_date": parse_fi_date(fields.get("event_date_raw")),
        "publish_date": parse_fi_date(fields.get("publish_date_raw")),
        "summary": summary or None,
    }


def pick_report_pdf(pdf_hrefs):
    """From a list of rendered PDF hrefs on a detail page, return the MAIN
    report PDF (``*_Tutkintaselostus.pdf``) absolute url, or None.

    ⚠️ Skip annexes: ``*_LIITE_N*.pdf`` / ``*_Liite*.pdf`` are appendices, not
    the report body. Prefer an explicit ``Tutkintaselostus`` filename; fall
    back to the first non-LIITE PDF.
    """
    main = None
    fallback = None
    for href in pdf_hrefs or []:
        if not href:
            continue
        fn = href.rsplit("/", 1)[-1]
        low = fn.lower()
        if not low.endswith(".pdf"):
            continue
        if re.search(r"_liite", low):
            continue  # annex
        absu = href if href.startswith("http") else urljoin(BASE, href)
        if "tutkintaselostus" in low and main is None:
            main = absu
        if fallback is None:
            fallback = absu
    return main or fallback


def extract_registration(text):
    """Best-effort OH- registration from title/summary/PDF text; None if absent."""
    m = _REG_RE.search(text or "")
    return m.group(0).upper() if m else None


def title_from_url(detail_url):
    """Last-resort human-ish title from the detail slug (rarely needed; the
    rendered <title>/<h1> is preferred upstream)."""
    slug = urlparse(detail_url or "").path.rsplit("/", 1)[-1]
    return slug[:-5] if slug.endswith(".html") else slug


# ─────────────────────────────────────────────────────────────────────────────
# HTTP helpers (plain httpx; PDFs are static — no browser/cookies needed)
# ─────────────────────────────────────────────────────────────────────────────


def download_pdf(client, url, dest_path):
    resp = client.get(url)
    resp.raise_for_status()
    with open(dest_path, "wb") as f:
        f.write(resp.content)
    return dest_path


# ─────────────────────────────────────────────────────────────────────────────
# Part B — Playwright transport (lazy import; not exercised in offline tests)
# ─────────────────────────────────────────────────────────────────────────────

class OtkesBrowser:
    """Playwright Chromium that renders the JS-injected listings + detail pages.

    No anti-bot is present, so this works HEADLESS on a normal box. The mini-PC
    runs it under ``xvfb-run`` for parity with our other browser sources;
    ``headless`` can be forced via the CLI flag / ``OTKES_HEADED`` env if a
    fingerprint block ever appears.

    Usage::

        with OtkesBrowser(headless=True) as br:
            year_urls = br.harvest_listings()          # from the root page
            detail_urls = br.get_detail_urls(year_url, "2024")
            meta = br.get_detail(detail_url)           # dict incl. pdf_url
    """

    #: Hard ceiling (s) for polling a rendered page for expected nodes.
    RENDER_TIMEOUT_S = 12
    #: Seconds between poll iterations while waiting for injected content.
    POLL_S = 1.0

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
            self._user_data_dir = tempfile.mkdtemp(prefix="otkes_chrome_")
        self._context = self._pw.chromium.launch_persistent_context(
            self._user_data_dir,
            headless=self._headless,
            user_agent=UA,
            locale="fi-FI",
            args=["--disable-dev-shm-usage"],
        )
        self._page = (
            self._context.pages[0]
            if self._context.pages
            else self._context.new_page()
        )

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
        # ⚠️ NEVER networkidle here (the CMS polls .srv forever).
        self._page.goto(url, wait_until="domcontentloaded", timeout=60_000)

    def _poll_links(self, predicate_js):
        """Poll the rendered <main> for anchors satisfying ``predicate_js``
        (a JS expression on the anchor href string). Returns list[str] hrefs."""
        import time

        js = (
            "() => Array.from((document.querySelector('main')||document.body)"
            ".querySelectorAll('a[href]')).map(a => a.href)"
        )
        deadline = time.monotonic() + self.RENDER_TIMEOUT_S
        last = []
        while time.monotonic() < deadline:
            hrefs = self._page.evaluate(js)
            matched = [h for h in hrefs if predicate_js(h)]
            if matched:
                return matched
            last = matched
            time.sleep(self.POLL_S)
        return last

    # -- public API -----------------------------------------------------------

    def harvest_listings(self):
        """Render the root page → list of year + topic listing URLs."""
        import time

        self._goto(ROOT_URL)
        js = (
            "() => Array.from(document.querySelectorAll('a[href]'))"
            ".map(a => a.href)"
        )
        deadline = time.monotonic() + self.RENDER_TIMEOUT_S
        hrefs = []
        while time.monotonic() < deadline:
            hrefs = self._page.evaluate(js)
            if harvest_listing_urls(hrefs):
                break
            time.sleep(self.POLL_S)
        return harvest_listing_urls(hrefs)

    def get_detail_urls(self, listing_url, year=None):
        """Render a year/topic listing → ordered de-duped report detail URLs.

        The same nested-slug predicate works for year pages (incl. the legacy
        ``ilmailu{year}/`` folder and cross-year aggregated folders) and topic
        pages. ``year`` is a hint only.
        """
        self._goto(listing_url)
        hrefs = self._poll_links(lambda h: is_detail_url(h))
        return harvest_detail_urls(hrefs, year)

    def get_detail(self, detail_url):
        """Render a detail page → metadata dict (parse_detail_text result plus
        ``title``, ``pdf_url``, ``registration``)."""
        import time

        self._goto(detail_url)
        # Poll until the metadata labels have rendered into <main>.
        deadline = time.monotonic() + self.RENDER_TIMEOUT_S
        inner = ""
        pdf_hrefs = []
        title = ""
        while time.monotonic() < deadline:
            data = self._page.evaluate(
                "() => {"
                " const m = document.querySelector('main') || document.body;"
                " const pdfs = Array.from(document.querySelectorAll('a[href]'))"
                "   .map(a => a.href).filter(h => /\\.pdf/i.test(h));"
                " return {text: m.innerText, pdfs, title: document.title};"
                "}"
            )
            inner = data.get("text") or ""
            pdf_hrefs = data.get("pdfs") or []
            title = data.get("title") or ""
            if "Onnettomuuspäivä" in inner or "Tutkintanumero" in inner:
                break
            time.sleep(self.POLL_S)

        meta = parse_detail_text(inner)
        clean_title = (title or "").split(" - ")[0].strip() or title_from_url(detail_url)
        meta["title"] = clean_title
        # ⚠️ Old reports omit the 'Onnettomuuspäivä' field — derive the event
        # date from the (date-bearing) title as a fallback.
        if not meta.get("event_date"):
            meta["event_date"] = parse_fi_date(clean_title)
        # If Tutkintanumero was absent but the title carries the case number
        # (C9/2003L Liikennelentokoneen…), recover it before the url fallback.
        if not meta.get("case_number"):
            recovered = normalize_case_number(clean_title)
            if recovered:
                meta["case_number"] = clean_title  # raw; normalised downstream
        meta["pdf_url"] = pick_report_pdf(pdf_hrefs)
        meta["registration"] = extract_registration(
            clean_title + " " + (meta.get("summary") or "")
        )
        return meta
