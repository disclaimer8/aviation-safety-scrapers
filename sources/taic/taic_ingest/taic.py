# taic_ingest/taic.py
"""
TAIC (Transport Accident Investigation Commission, New Zealand) listing and
inquiry-page parser.

The listing at /inquiries-recommendations is a paginated Drupal card grid
(12 cards/page, ?page=N 0-indexed, ~98 pages).  ⚠️ The mode filter
(?mode[0]=aviation) is JS/AJAX-only — the server IGNORES it and returns all
modes mixed (ao-/mo-/ro- prefixes).  We therefore walk ALL pages and filter
aviation client-side by the case_id prefix "AO-".  A page past the end
returns HTTP 200 with zero cards → stop-on-empty.

Inquiry pages are Drupal:
  - metadata in field--name-field-<name> divs (Details section; modern pages
    only — old pages have just title + PDF link)
  - narrative in field--name-field-rich-content blocks, each preceded by an
    <h2> section heading (English + Māori subtitle; Māori line is stripped)
  - report PDFs under /sites/default/files/ (⚠️ pre-~2000 PDFs are SCANS —
    photocopier output, no text layer; pdftotext yields nothing → the
    pipeline marks those source_tier='scanned')
"""
import html as _html
import json
import re
from urllib.parse import urljoin

BASE = "https://taic.org.nz"
LISTING_URL = BASE + "/inquiries-recommendations"
DELAY = 2.0

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Accept-Language": "en-NZ,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
# ⚠️ Drupal BigPipe: on a Varnish cache MISS the results region arrives as
# JSON command payloads in <script type="application/vnd.drupal-ajax"> tags
# instead of flat HTML (zero cards for a naive parser; bit us on the first
# backfill — pages 5+ were cache-cold and "empty" → walk stopped at 21 rows).
# This documented cookie makes Drupal use the single-flush no-JS path.
COOKIES = {"big_pipe_nojs": "1"}

# ──────────────────────────────────────────────────────────────────────────────
# Listing parsing
# ──────────────────────────────────────────────────────────────────────────────

# One result card. Cards do not nest, so a lazy match up to card__footer's
# closing pill span is safe.
_CARD_RE = re.compile(
    r'<div class="card card-type--inquiry[^"]*"(.*?)card__pill">([^<]*)<',
    re.DOTALL,
)
_CASE_RE = re.compile(r'card__incident"?\s*>\s*([A-Za-z]{2}-\d{4}-\d{3})')
_TITLE_RE = re.compile(
    r'card__title[^>]*>\s*<a\s+href="(/inquiry/[^"]+)"[^>]*>(.*?)</a>',
    re.DOTALL,
)
_TEXT_RE = re.compile(r'card__text[^>]*>(.*?)</p>', re.DOTALL)
# Two card__date spans: "Incident date:" then "Publish date:". Value is either
# a <time datetime="..."> or plain text ("Not yet published").
_DATE_SPAN_RE = re.compile(
    r'date-type">([^<]+)</span>\s*<span class="date-value">(.*?)</span>',
    re.DOTALL,
)
_TIME_RE = re.compile(r'datetime="(\d{4}-\d{2}-\d{2})')


def _strip_tags(fragment):
    text = re.sub(r"<[^>]+>", " ", fragment or "")
    text = _html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _parse_date_value(fragment):
    """A date-value span: <time datetime=...> → ISO date, else None."""
    m = _TIME_RE.search(fragment)
    return m.group(1) if m else None


# BigPipe placeholder payloads (cache-miss variant): JSON arrays of AJAX
# commands whose "data" fields hold the actual card HTML.
_BIG_PIPE_RE = re.compile(
    r'<script type="application/vnd\.drupal-ajax"[^>]*>(.*?)</script>',
    re.DOTALL,
)


def _expand_big_pipe(html):
    """
    Append the HTML hidden in BigPipe placeholder payloads to the document so
    the card regexes can see it.  No-op for flat (cached) pages.
    """
    extra = []
    for payload in _BIG_PIPE_RE.findall(html):
        payload = payload.strip()
        if not payload.startswith("["):
            continue
        try:
            cmds = json.loads(payload)
        except ValueError:
            continue
        for cmd in cmds:
            data = cmd.get("data") if isinstance(cmd, dict) else None
            if isinstance(data, str) and data:
                extra.append(data)
    return html + "\n".join(extra)


def parse_listing(html):
    """
    Parse one listing page → list of card dicts (ALL modes; caller filters).

    Each dict:
        case_id      – canonical id, e.g. "AO-2018-006" (uppercase)
        inquiry_url  – absolute URL to the inquiry page
        title        – card title text
        summary      – card__text teaser (may be None)
        event_date   – ISO incident date or None
        publish_date – ISO publish date or None (None = not yet published)
        pill         – card__pill text, "In progress" | "Published"
    """
    html = _expand_big_pipe(html)
    results = []
    for m in _CARD_RE.finditer(html):
        card_html, pill = m.group(1), _strip_tags(m.group(2))

        case_m = _CASE_RE.search(card_html)
        if not case_m:
            continue
        case_id = case_m.group(1).upper()

        title_m = _TITLE_RE.search(card_html)
        if not title_m:
            continue
        inquiry_url = urljoin(BASE, title_m.group(1))
        title = _strip_tags(title_m.group(2))

        text_m = _TEXT_RE.search(card_html)
        summary = _strip_tags(text_m.group(1)) if text_m else None

        event_date = publish_date = None
        for label, value in _DATE_SPAN_RE.findall(card_html):
            label = label.strip().lower()
            if label.startswith("incident"):
                event_date = _parse_date_value(value)
            elif label.startswith("publish"):
                publish_date = _parse_date_value(value)

        results.append(
            {
                "case_id": case_id,
                "inquiry_url": inquiry_url,
                "title": title,
                "summary": summary or None,
                "event_date": event_date,
                "publish_date": publish_date,
                "pill": pill,
            }
        )
    return results


def is_aviation(case_id):
    """Aviation inquiries are AO-YYYY-NNN (marine MO-, rail RO-)."""
    return (case_id or "").upper().startswith("AO-")


# ──────────────────────────────────────────────────────────────────────────────
# Inquiry page parsing
# ──────────────────────────────────────────────────────────────────────────────

# Metadata fields we map into report columns.  Drupal field machine names →
# our keys. (field--name-field-<machine> ... field__item">VALUE<)
_META_FIELDS = {
    "field-aircraft-registration": "registration",
    "field-type-and-serial-number": "aircraft",
    "field-operator": "operator",
    "field-location": "location",
    "field-injuries": "injuries",
}

# ⚠️ the wrapper div's class ends in "field__items" (plural) — anchor on the
# closing quote so we hit the value div, not the wrapper.
_FIELD_ITEM_RE = r'field__item">(.*?)</div>'

# Occurrence datetime: <time datetime="2018-07-21T01:04:00Z"> inside the
# field--name-field-date-and-time div.
_DATE_FIELD_RE = re.compile(
    r'field--name-field-date-and-time.*?datetime="(\d{4}-\d{2}-\d{2})',
    re.DOTALL,
)

# Site-local report PDFs (exclude external/squarespace links)
_PDF_RE = re.compile(r'href="(/sites/default/files/[^"]+\.pdf[^"]*)"', re.IGNORECASE)

# Section heading immediately before a rich-content block (English line; a
# Māori subtitle may follow inside the same heading — separated by markup).
_H2_RE = re.compile(r"<h([2-4])[^>]*>(.*?)</h\1>", re.DOTALL)

_RICH_MARK = 'field--name-field-rich-content'

_DIV_TOKEN_RE = re.compile(r"<div\b|</div>", re.IGNORECASE)


def _balanced_div(html, open_tag_start):
    """
    Given the index of a '<div' opening tag, return the end index of its
    matching '</div>' (exclusive) by depth counting.  Returns len(html) if
    unbalanced (truncated page).
    """
    depth = 0
    for m in _DIV_TOKEN_RE.finditer(html, open_tag_start):
        if m.group(0).lower() == "<div":
            depth += 1
        else:
            depth -= 1
            if depth == 0:
                return m.end()
    return len(html)


def _html_to_text(fragment):
    """Convert an HTML fragment to clean plain text, preserving paragraphs."""
    text = re.sub(r"<br\s*/?>", "\n", fragment)
    text = re.sub(r"</(?:p|li|h[2-6]|div|section|tr)>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = _html.unescape(text).replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _heading_before(html, pos):
    """English text of the last h2-h4 that ends before pos, or None."""
    last = None
    for m in _H2_RE.finditer(html, 0, pos):
        last = m
    if not last:
        return None
    text = _html_to_text(last.group(2))
    # The Māori subtitle sits on its own line after the English one
    return text.split("\n")[0].strip() or None


def parse_inquiry(html):
    """
    Parse an inquiry page → dict:
        narrative_text – joined rich-content sections w/ headings ('' if none)
        registration / aircraft / operator / location / injuries – from the
            Details metadata fields (None when absent — old pages)
        event_date     – ISO date from field-date-and-time or None
        pdf_urls       – list of absolute site-local PDF URLs (dedup, in order)
    """
    # Work inside <main> when present
    main_m = re.search(r"<main\b[^>]*>", html)
    main = html[main_m.start():] if main_m else html
    end = main.rfind("</main>")
    if end != -1:
        main = main[: end + 7]

    out = {k: None for k in _META_FIELDS.values()}
    for machine, key in _META_FIELDS.items():
        m = re.search(
            r'field--name-' + machine + r'.*?' + _FIELD_ITEM_RE, main, re.DOTALL
        )
        if m:
            val = _strip_tags(m.group(1))
            if key == "aircraft" and val:
                # "Type and serial number" field carries a verbose tail:
                # "Bell ... 206L-3 LongRanger serial number 51221" → trim it
                val = re.sub(r"[,;]?\s*serial\s+(?:number|no\.?)\b.*$", "",
                             val, flags=re.IGNORECASE).strip()
            out[key] = val or None

    date_m = _DATE_FIELD_RE.search(main)
    out["event_date"] = date_m.group(1) if date_m else None

    pdf_urls = []
    for href in _PDF_RE.findall(main):
        url = urljoin(BASE, _html.unescape(href))
        if url not in pdf_urls:
            pdf_urls.append(url)
    out["pdf_urls"] = pdf_urls

    # Narrative: every rich-content block with its preceding section heading
    sections = []
    pos = 0
    while True:
        i = main.find(_RICH_MARK, pos)
        if i == -1:
            break
        open_i = main.rfind("<div", 0, i)
        if open_i == -1:
            pos = i + len(_RICH_MARK)
            continue
        end_i = _balanced_div(main, open_i)
        block_text = _html_to_text(main[open_i:end_i])
        heading = _heading_before(main, open_i)
        if block_text:
            sections.append(f"{heading}\n{block_text}" if heading else block_text)
        pos = end_i

    out["narrative_text"] = "\n\n".join(sections)
    return out


# ──────────────────────────────────────────────────────────────────────────────
# HTTP helpers (live network; not exercised in offline tests)
# ──────────────────────────────────────────────────────────────────────────────


def fetch_listing_page(client, page):
    resp = client.get(LISTING_URL, params={"page": page})
    resp.raise_for_status()
    return resp.text


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
