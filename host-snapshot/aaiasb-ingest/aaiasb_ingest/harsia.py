# aaiasb_ingest/harsia.py
"""Transport + HTML parsing for HARSIA (Greece successor to AAIASB, post-2023).

Cloudflare-protected: uses patchright browser transport copied from cenipa.
URL: https://www.harsia.gr/index.php/category/porismata-ektheseis/

case_id convention: NN-YYYY (same as aaiasb.eu, e.g. "02-2023", "01-2024").
Special prefixes: "e" (serious incident) and "e-e-p" (interim report) are kept.
"""

import re
import time

BASE = "https://www.harsia.gr"
LISTING_PAGE_TPL = BASE + "/index.php/category/porismata-ektheseis/page/{n}/"
LISTING_PAGE_1 = BASE + "/index.php/category/porismata-ektheseis/"
MAX_PAGES = 5  # grow as needed; currently 3 pages
DELAY = 2.0
CF_TIMEOUT_MS = 25_000

# Match post URLs: /index.php/YYYY/MM/DD/slug/
POST_RE = re.compile(
    r"https://www\.harsia\.gr/index\.php/\d{4}/\d{2}/\d{2}/([^\"'\s/]+)/?"
)
PDF_RE = re.compile(r'href="([^"]+\.pdf[^"]*)"', re.I)

# Case ID normalization: extract NN/YYYY or NN-YYYY from post title/slug.
# Handles: "02/2023", "01/2024", "e01-2022", "e-e-p-03-2024", "04-2025", etc.
_CID_SLASH_RE = re.compile(
    r"\b(e[-.]e[-.]p[-.]?\s*)?(\d{1,3})\s*/\s*(\d{4})\b", re.I
)
_CID_DASH_RE = re.compile(
    r"\b(e[-.]e[-.]p[-.]?\s*|e)?(\d{1,3})[-](\d{4})\b", re.I
)
_DATE_META_RE = re.compile(r'(?:date|published)[^>]*>\s*(\d{4})-(\d{2})-(\d{2})')
_TITLE_RE = re.compile(r'<h1[^>]*>(.*?)</h1>', re.S | re.I)
_TAG_RE = re.compile(r'<[^>]+>')
_WS_RE = re.compile(r'\s+')


def _strip(s):
    s = _TAG_RE.sub(" ", s)
    import html
    s = html.unescape(s)
    return _WS_RE.sub(" ", s).strip()


def normalize_harsia_case_id(text):
    """Extract and normalise case_id from post title or slug.

    Returns a string like "02-2023", "e01-2022", "e-e-p-03-2024", or ""."""
    # Prefer slash form from title: "02/2023"
    m = _CID_SLASH_RE.search(text)
    if m:
        prefix = (m.group(1) or "").strip().lower().replace(".", "-").replace(" ", "")
        num = m.group(2).zfill(2)
        year = m.group(3)
        return f"{prefix}{num}-{year}"

    # Try dash form: "01-2024" or "e01-2022"
    m = _CID_DASH_RE.search(text)
    if m:
        prefix = (m.group(1) or "").strip().lower().replace(".", "-").replace(" ", "")
        num = m.group(2).zfill(2)
        year = m.group(3)
        return f"{prefix}{num}-{year}"

    return ""


def classify_pdf_lang(url):
    """Return 'en' or 'el' based on URL/filename cues."""
    low = url.lower()
    # Explicit EN indicators
    if re.search(r'[-_.]en[_.\-]|[-_/]english|final[-_]report[-_]|final[-_ ]report[_ ]|_en\.pdf$|-en\.pdf$', low):
        return "en"
    if re.search(r'final.report', low, re.I) and "porisma" not in low and "teliko" not in low:
        return "en"
    return "el"


def pick_pdf(pdfs):
    """Given list of PDF URLs, return (chosen_url, lang). Prefer EN."""
    en_pdfs = [p for p in pdfs if classify_pdf_lang(p) == "en"]
    if en_pdfs:
        return en_pdfs[0], "en"
    if pdfs:
        return pdfs[0], "el"
    return None, None


# ─── HarsiaBrowser ─────────────────────────────────────────────────────────────

class HarsiaBrowser:
    """Headed patchright browser that passes Cloudflare on www.harsia.gr.

    Pattern copied verbatim from CenipaBrowser in cenipa_ingest/cenipa.py.
    Use xvfb-run -a on the mini-PC.
    """

    CF_TIMEOUT_MS = CF_TIMEOUT_MS

    def __init__(self, headless=False, user_data_dir=None):
        self._headless = headless
        self._user_data_dir = user_data_dir
        self._pw = None
        self._context = None
        self._page = None

    def start(self):
        import tempfile
        try:
            from patchright.sync_api import sync_playwright
        except ImportError:
            from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().__enter__()
        if self._user_data_dir is None:
            self._user_data_dir = tempfile.mkdtemp(prefix="harsia_chrome_")

        self._context = self._pw.chromium.launch_persistent_context(
            self._user_data_dir,
            headless=self._headless,
            args=["--disable-dev-shm-usage"],
        )
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()

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

    def _wait_for_cf(self, selector="article,h1,h2,div.entry-content", timeout=None):
        """Wait until CF challenge clears (page title no longer 'just a moment')."""
        ms = timeout or self.CF_TIMEOUT_MS
        deadline = time.monotonic() + ms / 1000
        while time.monotonic() < deadline:
            title = self._page.title().lower()
            if "just a moment" not in title:
                try:
                    self._page.wait_for_selector(selector, timeout=3000)
                    return
                except Exception:
                    pass
            time.sleep(1)
        # last attempt
        self._page.wait_for_selector(selector, timeout=5000)

    def get_listing_html(self, page_num=1):
        """Fetch listing page N, return HTML."""
        if page_num == 1:
            url = LISTING_PAGE_1
        else:
            url = LISTING_PAGE_TPL.format(n=page_num)
        self._page.goto(url, wait_until="domcontentloaded", timeout=30000)
        self._wait_for_cf()
        time.sleep(DELAY)
        return self._page.content()

    def get_post_html(self, url):
        """Fetch a single post page, return HTML."""
        self._page.goto(url, wait_until="domcontentloaded", timeout=30000)
        self._wait_for_cf()
        time.sleep(DELAY)
        return self._page.content()

    def download_pdf(self, url, dest):
        """Download PDF from inside CF-cleared page context (in-page fetch).

        Re-warms on 403 (expired cf_clearance) exactly like CenipaBrowser.
        """
        import base64

        _FETCH_JS = """async (u) => {
            try {
                const r = await fetch(u, { credentials: 'include' });
                if (!r.ok) return { ok: false, status: r.status };
                const buf = await r.arrayBuffer();
                const bytes = new Uint8Array(buf);
                let bin = '';
                const chunk = 0x8000;
                for (let i = 0; i < bytes.length; i += chunk) {
                    bin += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
                }
                return { ok: true, b64: btoa(bin) };
            } catch (e) { return { ok: false, status: 'fetch-threw:' + e }; }
        }"""

        result = self._page.evaluate(_FETCH_JS, url)
        if not result.get("ok"):
            # Re-warm: navigate to listing page 1 to refresh cf_clearance
            try:
                self.get_listing_html(1)
            except Exception:
                pass
            result = self._page.evaluate(_FETCH_JS, url)
        if not result.get("ok"):
            raise RuntimeError(
                f"[harsia download] HTTP {result.get('status')} for {url}"
            )
        with open(dest, "wb") as fh:
            fh.write(base64.b64decode(result["b64"]))


# ─── Parsing helpers ─────────────────────────────────────────────────────────

def parse_listing_posts(html):
    """Extract unique full post URLs from a listing page."""
    # Use href= pattern to get full URLs only (POST_RE captures slug group, not full URL)
    urls = re.findall(
        r'href="(https://www\.harsia\.gr/index\.php/\d{4}/\d{2}/\d{2}/[^"]+)"',
        html, re.I
    )
    return list(dict.fromkeys(urls))


def parse_post(html, post_url):
    """Extract metadata from a single post page.

    Returns dict with keys: case_id, title, pdf_urls, pdf_url, pdf_url_en,
    pdf_url_el, lang, report_type.
    """
    # Title
    tm = _TITLE_RE.search(html)
    title_raw = _strip(tm.group(1)) if tm else ""

    # PDF links
    pdfs = list(dict.fromkeys(PDF_RE.findall(html)))
    # Filter to harsia.gr uploads only (skip external)
    pdfs = [p for p in pdfs if "harsia.gr" in p or p.startswith("/wp-content")]
    # Make absolute
    pdfs = [p if p.startswith("http") else BASE + p for p in pdfs]

    chosen, lang = pick_pdf(pdfs)

    # Build pdf_url_en / pdf_url_el
    pdf_url_en = next((p for p in pdfs if classify_pdf_lang(p) == "en"), None)
    pdf_url_el = next((p for p in pdfs if classify_pdf_lang(p) == "el"), None)

    # case_id: try title first, then URL slug
    case_id = normalize_harsia_case_id(title_raw)
    if not case_id:
        slug = post_url.rstrip("/").split("/")[-1]
        case_id = normalize_harsia_case_id(slug.replace("-", "/"))
        if not case_id:
            # last fallback: use slug prefix NN-YYYY literally
            m = re.search(r"(\d{2})-(\d{4})", slug)
            if m:
                case_id = m.group(0)

    # Report type: "Interim" for ΕΕΠ / endiamesi, else "Final"
    rt = "Final report"
    if re.search(r"(endiamesi|interim|ε\.ε\.π|e-e-p|ενδιάμεση)", title_raw + post_url, re.I):
        rt = "Interim report"

    return {
        "case_id": case_id,
        "title": title_raw,
        "report_url": post_url,
        "pdf_urls": pdfs,
        "pdf_url": chosen,
        "pdf_url_en": pdf_url_en,
        "pdf_url_el": pdf_url_el,
        "lang": lang,
        "report_type": rt,
    }
