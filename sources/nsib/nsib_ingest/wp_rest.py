# nsib_ingest/wp_rest.py
"""
WordPress REST API discover extension for NSIB final reports.

The NSIB site publishes 88 "Final Report" entries.  Only ~3 of them carry
a downloadable PDF (uploaded June 2026 onwards — /ninja-forms/102/ path).
The remaining 85 posts contain a short HTML narrative summary (80–1020 chars
clean text) embedded in the post content — not a PDF, but still usable as a
narrative body where the text exceeds the floor.

This module:
  - Paginates wp/v2/posts?search=final+report to collect all final-report slugs.
  - For posts WITH a PDF href in content: only inserts if that pdf_url is not
    already known (prevents duplicates when the PDF was already discovered via
    the HTML listing table under a different case_id).
  - For posts WITHOUT PDF: records with source_tier='html', narrative_text set
    immediately (status='parsed', skip fetch/parse — there is no PDF).
  - Deduplicates by case_id AND by pdf_url.
  - Case ID extraction: posts have a structured ref in content like
    UNA/2021/11/17/F or MAL/2022/04/26/F — use that as case_id.
    Fallback: NSIB-FIN-<REG>-<DATE>.
"""
import re
import time
import html as _html

_PDF_RE = re.compile(r'href="(https://nsib\.gov\.ng/wp-content/uploads/[^"]+?\.pdf)"', re.I)
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
# Case ref patterns embedded in content: CODE/YYYY/MM/DD/F or CODE/YYYY/MM/DD/F/NN
_CASE_RE = re.compile(
    r'\b([A-Z0-9]{2,15}/\d{4}/\d{2}/\d{2}/F(?:/\d{1,3})?)\b', re.I
)
# Registration from slug: final-report-<operator>-<reg> (e.g. -5n-bun, -tc-lol)
_REG_IN_SLUG = re.compile(r'-((?:5[Nn]-[A-Z]{3}|[A-Z]{1,2}-[A-Z]{3,4}|\d{4}[A-Z]{0,3}))\b', re.I)

WP_SEARCH_URL = "https://nsib.gov.ng/wp-json/wp/v2/posts"
DELAY = 1.5


def _clean_html(h):
    return _WS.sub(" ", _html.unescape(_TAG.sub(" ", h))).strip()


def _extract_case_id(content, slug):
    """Extract structured case_id from HTML content or slug."""
    m = _CASE_RE.search(content)
    if m:
        return m.group(1).upper()
    rm = _REG_IN_SLUG.search(slug)
    if rm:
        return f"NSIB-FIN-{rm.group(1).upper()}"
    return f"NSIB-FIN-WP-{slug[:40]}"


def discover_wp_rest(conn, client, db):
    """
    Walk WP REST API pages for final-report posts.
    Insert new case_ids not already in nsib_reports.

    Returns dict with keys:
      inserted_with_pdf: int
      inserted_html_only: int
      already_known: int
    """
    from . import db as db_mod

    # Build sets of already-known case_ids and pdf_urls for dedup
    known_case_ids = set(
        row[0] for row in conn.execute("SELECT case_id FROM nsib_reports").fetchall()
    )
    known_pdf_urls = set(
        row[0] for row in conn.execute(
            "SELECT pdf_url FROM nsib_reports WHERE pdf_url IS NOT NULL"
        ).fetchall()
    )

    inserted_with_pdf = 0
    inserted_html_only = 0
    already_known = 0

    page = 1
    while True:
        r = client.get(
            WP_SEARCH_URL,
            params={"per_page": 100, "search": "final report", "page": page},
            timeout=60,
        )
        r.raise_for_status()
        posts = r.json()
        if not posts:
            break

        for p in posts:
            slug = p.get("slug", "")
            if not slug.startswith("final-report"):
                continue

            content_raw = p.get("content", {}).get("rendered", "")
            post_url = p.get("link", "")

            # Find PDF links
            pdfs = _PDF_RE.findall(content_raw)
            pdf_url = pdfs[0] if pdfs else None

            # Skip if PDF already known under a different case_id
            if pdf_url and pdf_url in known_pdf_urls:
                already_known += 1
                continue

            # Extract clean text narrative
            clean_text = _clean_html(content_raw)

            # Build case_id
            case_id = _extract_case_id(content_raw, slug)

            # Check for duplicate case_id
            if case_id in known_case_ids:
                already_known += 1
                continue

            ts = db_mod.now_ms()
            known_case_ids.add(case_id)

            if pdf_url:
                # Has PDF — insert as 'new' for normal fetch->parse->build
                conn.execute(
                    "INSERT INTO nsib_reports "
                    "(case_id, report_url, pdf_url, title, event_class, aircraft, "
                    "registration, operator, date_of_occurrence, location, "
                    "report_type, lang, status, discovered_at, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        case_id, post_url, pdf_url,
                        slug.replace("-", " "),
                        None, None, None, None, None, None,
                        "Final Report", "en",
                        db_mod.STATUS_NEW,
                        ts, ts,
                    ),
                )
                known_pdf_urls.add(pdf_url)
                inserted_with_pdf += 1
            else:
                # HTML-only: store narrative directly, skip to 'parsed'
                if len(clean_text) < 50:
                    already_known += 1
                    continue
                conn.execute(
                    "INSERT INTO nsib_reports "
                    "(case_id, report_url, pdf_url, pdf_path, title, event_class, aircraft, "
                    "registration, operator, date_of_occurrence, location, "
                    "report_type, narrative_text, source_tier, lang, status, "
                    "discovered_at, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        case_id, post_url, None, None,
                        slug.replace("-", " "),
                        None, None, None, None, None, None,
                        "Final Report",
                        clean_text, "html", "en",
                        db_mod.STATUS_PARSED,
                        ts, ts,
                    ),
                )
                inserted_html_only += 1

        conn.commit()

        total_pages = int(r.headers.get("X-WP-TotalPages", 1))
        if page >= total_pages:
            break
        page += 1
        time.sleep(DELAY)

    return {
        "inserted_with_pdf": inserted_with_pdf,
        "inserted_html_only": inserted_html_only,
        "already_known": already_known,
    }
