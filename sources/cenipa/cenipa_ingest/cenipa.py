# cenipa_ingest/cenipa.py
"""CENIPA (Brazil) — listing parser + Playwright CF transport.

Part A: Pure parsing functions (testable without a browser).
Part B: CenipaBrowser — Playwright-based transport (import is lazy so tests
        don't need a running browser).
"""

import re

BASE = "https://sistema.cenipa.fab.mil.br"
LISTING_URL = BASE + "/cenipa/paginas/relatorios/relatorios.php"
DELAY = 3.0
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# ─── pagination ──────────────────────────────────────────────────────────────

_PAG_RE = re.compile(r"[?&]pag=(\d+)")

_TR_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.DOTALL | re.IGNORECASE)
_TD_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.DOTALL | re.IGNORECASE)
_HREF_RE = re.compile(r'href="([^"]*)"', re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_DATE_RE = re.compile(r"^(\d{2})/(\d{2})/(\d{4})$")

_CASE_ID_RE = re.compile(r"^(A|IG|IN|ACIDENTE|INCIDENTE)-", re.IGNORECASE)


def page_url(n: int) -> str:
    """Return URL for listing page N (1-indexed)."""
    return f"{BASE}/cenipa/paginas/relatorios/relatorios?&?&pag={n}"


def _strip(s: str) -> str:
    """Strip tags and collapse whitespace."""
    s = _TAG_RE.sub(" ", s)
    return _WS_RE.sub(" ", s).strip()


_CASEID_WS_RE = re.compile(r"\s*([-/])\s*")


def _normalize_case_id(s: str) -> str:
    """Collapse whitespace around dashes/slashes in a case_id.

    The CENIPA listing carries the same report under two spellings
    (e.g. 'A - 013/CENIPA/2013' vs 'A-013/CENIPA/2013') which created
    duplicate rows and a UNIQUE slug collision downstream (2026-06-04).
    """
    return _CASEID_WS_RE.sub(r"\1", s)


def _date_to_iso(s: str) -> str | None:
    """Convert DD/MM/YYYY to ISO YYYY-MM-DD, or None on failure."""
    s = (s or "").strip()
    m = _DATE_RE.match(s)
    if not m:
        return None
    d, mo, y = m.groups()
    return f"{y}-{mo}-{d}"


def _abs_pdf(href: str) -> str:
    """Make a relative PDF href absolute."""
    href = href.strip()
    if href.startswith("http"):
        return href
    if href.startswith("/"):
        return BASE + href
    # relative path like "rf/pt/..."
    return BASE + "/cenipa/paginas/relatorios/" + href


def parse_listing(html: str) -> list[dict]:
    """Parse the CENIPA listing table HTML → list of report dicts.

    Each dict has keys:
        case_id, date_of_occurrence, registration, classificacao,
        occurrence_type, location, pdf_url_pt, pdf_url_en
    """
    rows = []
    for tr_m in _TR_RE.finditer(html):
        inner = tr_m.group(1)
        tds = _TD_RE.findall(inner)
        if len(tds) < 7:
            continue

        case_id = _normalize_case_id(_strip(tds[0]))
        if not case_id:
            continue
        # Skip rows that look like header repeats (contain <th>) or don't
        # have a recognisable case_id structure.
        if "<th" in inner.lower():
            continue
        # Accept any non-empty first cell as case_id (tolerant)
        # but skip if it exactly matches a known header keyword
        if case_id.upper() in {"NÚMERO", "NUMERO", "DATA DA OCORRÊNCIA"}:
            continue

        date_raw = _strip(tds[1])
        date_of_occurrence = _date_to_iso(date_raw) or date_raw or None

        registration = _strip(tds[2]) or None
        classificacao = _strip(tds[3]) or None
        occurrence_type = _strip(tds[4]) or None

        cidade = _strip(tds[5])
        estado = _strip(tds[6])
        location = f"{cidade}, {estado}" if cidade and estado else (cidade or estado or None)

        # PDF links in tds[7] (RELATÓRIO cell)
        pdf_url_pt = None
        pdf_url_en = None
        relatorio_td = tds[7] if len(tds) > 7 else ""
        for href_m in _HREF_RE.finditer(relatorio_td):
            href = href_m.group(1)
            if not href.lower().endswith(".pdf"):
                continue
            href_lower = href.lower()
            if "rf/en/" in href_lower:
                pdf_url_en = _abs_pdf(href)
            elif "rf/pt/" in href_lower:
                pdf_url_pt = _abs_pdf(href)

        rows.append(
            {
                "case_id": case_id,
                "date_of_occurrence": date_of_occurrence,
                "registration": registration,
                "classificacao": classificacao,
                "occurrence_type": occurrence_type,
                "location": location,
                "pdf_url_pt": pdf_url_pt,
                "pdf_url_en": pdf_url_en,
            }
        )
    return rows


def last_page(html: str) -> int:
    """Return the maximum pag=N found in pagination links, or 33 as fallback."""
    pages = [int(m.group(1)) for m in _PAG_RE.finditer(html)]
    return max(pages) if pages else 33


def make_pdf_choice(row: dict) -> tuple[str | None, str]:
    """Return (url, lang) preferring EN; fall back to PT; else (None, 'pt')."""
    if row.get("pdf_url_en"):
        return row["pdf_url_en"], "en"
    if row.get("pdf_url_pt"):
        return row["pdf_url_pt"], "pt"
    return None, "pt"


# ─── Playwright transport ────────────────────────────────────────────────────

class CenipaBrowser:
    """Headed Chromium browser that passes the Cloudflare JS challenge.

    Playwright is imported lazily so that ``import cenipa_ingest.cenipa``
    works even when playwright is not installed (unit tests run without it).

    Usage (context manager)::

        with CenipaBrowser(headless=False) as browser:
            html = browser.get_listing_html(1)
            rows = parse_listing(html)
            browser.download_pdf(rows[0]["pdf_url_pt"], "/tmp/report.pdf")

    On the mini-PC, wrap the call in ``xvfb-run`` so headed Chromium has a
    display::

        xvfb-run -a python -m cenipa_ingest.cli discover
    """

    #: Seconds to wait for the CF challenge to clear before giving up.
    CF_TIMEOUT_MS = 20_000

    def __init__(self, headless: bool = False, user_data_dir: str | None = None):
        self._headless = headless
        self._user_data_dir = user_data_dir
        self._playwright = None
        self._context = None
        self._page = None

    # -- lifecycle ------------------------------------------------------------

    def start(self):
        """Launch Chromium and open a persistent browser context."""
        import tempfile

        # patchright is a drop-in Playwright fork patched to pass Cloudflare's
        # JS challenge (vanilla Playwright Chromium stays stuck on "Just a
        # moment..." forever under Xvfb). Fall back to stock playwright only if
        # patchright is unavailable (e.g. unit envs that never launch a browser).
        try:
            from patchright.sync_api import sync_playwright
        except ImportError:
            from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().__enter__()

        if self._user_data_dir is None:
            self._user_data_dir = tempfile.mkdtemp(prefix="cenipa_chrome_")

        # ⚠️ patchright manages a consistent stealth fingerprint. Passing a
        # custom user_agent (or extra launch args) BREAKS the CF bypass — the
        # page stays stuck on "Just a moment..." forever. Launch bare (the only
        # safe extra is --disable-dev-shm-usage for low-/dev/shm CI boxes) and
        # reuse the default page the persistent context already opens.
        self._context = self._pw.chromium.launch_persistent_context(
            self._user_data_dir,
            headless=self._headless,
            args=["--disable-dev-shm-usage"],
        )
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()

    def stop(self):
        """Close the browser context and stop Playwright."""
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

    # -- navigation -----------------------------------------------------------

    def _wait_for_cf(self):
        """Wait until the CF JS challenge resolves (table appears or title changes)."""
        import time

        deadline = time.monotonic() + self.CF_TIMEOUT_MS / 1000
        while time.monotonic() < deadline:
            title = self._page.title()
            if "just a moment" not in title.lower():
                # Additional check: table must be in DOM
                try:
                    self._page.wait_for_selector("table", timeout=2_000)
                    return
                except Exception:
                    pass
            time.sleep(1)
        # Last attempt even if CF text is still present
        self._page.wait_for_selector("table", timeout=5_000)

    def get_listing_html(self, n: int) -> str:
        """Fetch page N of the CENIPA listing, return full page HTML.

        Automatically waits for the Cloudflare JS challenge to clear.
        """
        import time

        url = page_url(n)
        self._page.goto(url, wait_until="domcontentloaded")
        self._wait_for_cf()
        time.sleep(DELAY)
        return self._page.content()

    # -- PDF download ---------------------------------------------------------

    def download_pdf(self, url: str, dest: str) -> None:
        """Download a PDF from inside the CF-cleared page context.

        ⚠️ ``context.request.get()`` does NOT pass Cloudflare for the PDF
        endpoint (it lacks the page's TLS/JS fingerprint → HTTP 403). Instead we
        run an in-page ``fetch()`` on the already-cleared listing page: that
        request carries the real browser fingerprint + the cf_clearance cookie,
        so CF lets it through. The body is shuttled back as base64.

        ⚠️ The cf_clearance cookie expires (~30 min) mid-backfill; once stale,
        every in-page fetch 403s and never recovers because the page isn't
        re-navigated. So on a non-OK result we RE-WARM (navigate back to the
        listing → solves a fresh CF challenge → refreshes clearance) and retry
        once. That self-heals expiry: the first 403 after expiry triggers one
        re-nav, then this + subsequent downloads succeed for another ~30 min.

        Raises ``RuntimeError`` on non-200 status (after the re-warm retry).
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
            # Likely expired clearance — re-warm on the listing and retry once.
            try:
                self.get_listing_html(1)
            except Exception:
                pass
            result = self._page.evaluate(_FETCH_JS, url)
        if not result.get("ok"):
            raise RuntimeError(
                f"[cenipa download] HTTP {result.get('status')} for {url}"
            )
        with open(dest, "wb") as fh:
            fh.write(base64.b64decode(result["b64"]))
