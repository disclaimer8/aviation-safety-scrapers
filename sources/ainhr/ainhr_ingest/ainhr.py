# ainhr_ingest/ainhr.py
"""AIN.HR (Croatia) HTML scraper for the aviation-investigations category.

Source: https://ain.hr/kategorije/zrakoplovne-istrage/
- The aviation category page is a single server-rendered (Divi/WordPress) page
  listing every aviation investigation as a /istrage/<slug>/ post link. There is
  no pagination (~63 posts on one page).
- Each post page links one or more report PDFs (HR final / HR preliminary plus
  EN translations) hosted on ain.hr/wp-content/uploads. PDFs have a text layer.
- There is no clean case number on the public site, so the post slug itself is
  used as the intrinsic, order-independent case_id (aircraft-location-date).
"""
import html as _html
import re
from pathlib import Path

BASE = "https://ain.hr"
INDEX_URL = BASE + "/kategorije/zrakoplovne-istrage/"
REFERER = BASE + "/"
DELAY = 1.8

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0 Safari/537.36"
)

HEADERS = {
    "User-Agent": UA,
    "Referer": REFERER,
}

# ──────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────

# Croatian month names (genitive forms as used in slugs/titles) -> month number.
_MONTHS_HR = {
    "sijecnja": 1, "veljace": 2, "ozujka": 3, "travnja": 4, "svibnja": 5,
    "lipnja": 6, "srpnja": 7, "kolovoza": 8, "rujna": 9, "listopada": 10,
    "studenog": 11, "studenoga": 11, "prosinca": 12,
}

# ──────────────────────────────────────────────
# Compiled regexes
# ──────────────────────────────────────────────

# Post links: https://ain.hr/istrage/<slug>/  (the bare /istrage/ index excluded)
_POST_LINK_RE = re.compile(r'href="https://ain\.hr/istrage/([a-z0-9][a-z0-9-]*)/"')

# Trailing date in slug: -DD-MM-YYYY  (numeric)
_SLUG_DATE_NUM_RE = re.compile(r"-(\d{1,2})-(\d{1,2})-(\d{4})$")
# Trailing date in slug: -DD-<croatian-month>-YYYY  (word month)
_SLUG_DATE_WORD_RE = re.compile(r"-(\d{1,2})-([a-z]+)-(\d{4})$")

# PDF anchor on a post page: capture href + inner text (the link label)
_PDF_ANCHOR_RE = re.compile(
    r'<a[^>]+href="(https://ain\.hr/wp-content/uploads/[^"]+\.pdf)"[^>]*>(.*?)</a>',
    re.DOTALL | re.IGNORECASE,
)

# Post H1 title
_H1_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.DOTALL | re.IGNORECASE)

# Registration in PDF text: Croatian 9A-XXX or common foreign marks (D-, HA-,
# OE-, OK-, S5-, F-, G-, N..., I-, OM-, SP-, OY-, etc.). Hyphenated, uppercase.
_PDF_REG_RE = re.compile(
    r"\b(9A-[A-Z0-9]{2,4}"
    r"|[A-Z]{1,2}-[A-Z]{3,5}"
    r"|N\d{1,5}[A-Z]{0,2})\b"
)

# Aircraft type line in PDF header is unreliable to generalise; left to title.


# ──────────────────────────────────────────────
# Client factory
# ──────────────────────────────────────────────

def make_client():
    """Return an httpx.Client configured with browser UA."""
    import httpx
    return httpx.Client(
        headers=HEADERS,
        follow_redirects=True,
        timeout=30.0,
    )


# ──────────────────────────────────────────────
# case_id normalisation
# ──────────────────────────────────────────────

_CASE_NONSLUG = re.compile(r"[^a-z0-9]+")


def _normalize_case_id(slug: str) -> str:
    """Normalise a post slug to a canonical, intrinsic case_id.

    Lowercase, collapse any non [a-z0-9] run to a single '-', strip edges.
    The AIN.HR slug is already in this shape, so this is mostly a guard against
    stray casing / trailing separators.
    """
    s = (slug or "").lower().strip()
    s = _CASE_NONSLUG.sub("-", s).strip("-")
    return s


def make_case_id(slug: str) -> str:
    """Public alias — the case_id IS the normalised post slug (intrinsic)."""
    return _normalize_case_id(slug)


# ──────────────────────────────────────────────
# Discovery
# ──────────────────────────────────────────────

def iter_post_slugs(index_html: str) -> list[str]:
    """Parse the aviation category page → ordered, de-duplicated list of post
    slugs (each a /istrage/<slug>/ link)."""
    seen: set[str] = set()
    slugs: list[str] = []
    for m in _POST_LINK_RE.finditer(index_html):
        slug = m.group(1)
        if slug in seen:
            continue
        seen.add(slug)
        slugs.append(slug)
    return slugs


def post_url(slug: str) -> str:
    return f"{BASE}/istrage/{slug}/"


# ──────────────────────────────────────────────
# Field extraction from slug
# ──────────────────────────────────────────────

def date_from_slug(slug: str) -> str | None:
    """Extract trailing date from a slug → ISO YYYY-MM-DD, or None.

    Handles both numeric (-DD-MM-YYYY) and Croatian word-month
    (-DD-<month>-YYYY) trailing dates.
    """
    s = slug.strip()
    m = _SLUG_DATE_NUM_RE.search(s)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            return f"{y:04d}-{mo:02d}-{d:02d}"
    m = _SLUG_DATE_WORD_RE.search(s)
    if m:
        d = int(m.group(1))
        mo = _MONTHS_HR.get(m.group(2))
        y = int(m.group(3))
        if mo and 1 <= d <= 31:
            return f"{y:04d}-{mo:02d}-{d:02d}"
    return None


def event_class_from_slug(slug: str) -> str:
    """Infer event class from the Croatian slug prefix.

    'ozbiljna-nezgoda' → Serious incident; 'nezgoda' → Incident;
    everything else (nesreca / pad / izlijetanje / …) → Accident.
    """
    s = slug.lower()
    if s.startswith("ozbiljna-nezgoda") or "ozbiljna-nezgoda" in s[:24]:
        return "Serious incident"
    if s.startswith("nezgoda") or "-nezgoda-" in s[:20]:
        return "Incident"
    return "Accident"


# ──────────────────────────────────────────────
# PDF selection from a post page
# ──────────────────────────────────────────────

def _label_text(raw: str) -> str:
    return _html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", raw))).strip()


def parse_post(html: str, slug: str) -> dict:
    """Parse a single /istrage/<slug>/ post page.

    Returns dict with: case_id, report_url, pdf_url_hr, pdf_url_en, pdf_url,
    lang, title, event_class, date_of_occurrence.

    PDF preference for the (Croatian) narrative pdf_url:
      HR final  (Završno izvješće / *zavrsno_izvjesce*)   >
      HR preliminary (Preliminarno izvješće / *preliminarno*) >
      any other HR pdf (not *final_report* / *preliminary_report* / EN label).
    EN PDFs (Final report / Preliminary report / *final_report*) are stored
    separately in pdf_url_en for reference.
    """
    case_id = _normalize_case_id(slug)

    h1_m = _H1_RE.search(html)
    title = _label_text(h1_m.group(1)) if h1_m else slug

    pdf_url_hr_final = None
    pdf_url_hr_prelim = None
    pdf_url_hr_other = None
    pdf_url_en = None

    for m in _PDF_ANCHOR_RE.finditer(html):
        url = m.group(1)
        label = _label_text(m.group(2)).lower()
        fname = url.rsplit("/", 1)[-1].lower()

        is_en = (
            "final report" in label
            or "preliminary report" in label
            or "final_report" in fname
            or "preliminary_report" in fname
            or "_eng" in fname
            or "english" in label
        )
        if is_en:
            if pdf_url_en is None:
                pdf_url_en = url
            continue

        # Croatian PDFs
        if "zavrsno" in fname or "završno" in label or "zavrsno" in label:
            if pdf_url_hr_final is None:
                pdf_url_hr_final = url
        elif "preliminarno" in fname or "preliminarno" in label:
            if pdf_url_hr_prelim is None:
                pdf_url_hr_prelim = url
        else:
            if pdf_url_hr_other is None:
                pdf_url_hr_other = url

    pdf_url_hr = pdf_url_hr_final or pdf_url_hr_prelim or pdf_url_hr_other

    if pdf_url_hr:
        pdf_url, lang = pdf_url_hr, "hr"
    elif pdf_url_en:
        pdf_url, lang = pdf_url_en, "en"
    else:
        pdf_url, lang = None, None

    return {
        "case_id": case_id,
        "report_url": post_url(slug),
        "pdf_url_hr": pdf_url_hr,
        "pdf_url_en": pdf_url_en,
        "pdf_url": pdf_url,
        "lang": lang,
        "title": title,
        "event_class": event_class_from_slug(slug),
        "date_of_occurrence": date_from_slug(slug),
    }


# ──────────────────────────────────────────────
# Registration extraction from PDF text
# ──────────────────────────────────────────────

def registration_from_text(narrative: str) -> str | None:
    """Best-effort registration mark from the report text header region."""
    if not narrative:
        return None
    head = narrative[:1500]
    m = _PDF_REG_RE.search(head)
    return m.group(1) if m else None


# ──────────────────────────────────────────────
# Download
# ──────────────────────────────────────────────

def download(client, pdf_url: str, dest: str | Path) -> None:
    """GET pdf_url with Referer header and write bytes to dest."""
    resp = client.get(pdf_url, headers={"Referer": REFERER})
    resp.raise_for_status()
    with open(dest, "wb") as fh:
        fh.write(resp.content)


# ── probable cause ────────────────────────────────────────────────────────────
#
# AIN Croatia reports carry "UZROK" as its own line, then sub-headings that are
# part of the cause and must be kept — "Neposredni uzrok" (direct) and
# "Kontributivni čimbenik" (contributing). The section ends at the safety
# recommendations.
#
# The vocabulary is measured across all 62 PDFs on the host, not guessed:
#
#     SIGURNOSNE PREPORUKE              26
#     AGENCIJA ZA ISTRAŽIVANJE NESREĆA  15   <- page furniture, not a heading
#     KONTRIBUTIVNI ČIMBENICI            2   <- part of the cause, not the end
#     PODUZETE MJERE                     2
#     PREPORUKE                          1
#
# The agency name appearing as the "next heading" in 15 of 46 reports is the
# running footer landing mid-section. Treating it as a terminator would cut the
# contributing-factor paragraph off every one of them, so it is stripped as
# furniture instead. Measuring the vocabulary rather than assuming it is the
# only reason that distinction was visible at all.
#
# probable_cause decides indexability: prod needs a quality score of 50, a
# narrative over 300 scores 30 and a cause over 100 scores 20, and the other
# three components are hardcoded null at projection.
_PC_HEADING_RE = re.compile(
    r"^[ \t\f]*(?:\d+(?:\.\d+)*[.)]?[ \t\f]*)?UZRO(?:K|CI)[ \t]*:?[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)
_PC_TERMINATOR_RE = re.compile(
    r"^[ \t\f]*(?:\d+(?:\.\d+)*[.)]?[ \t\f]*)?"
    r"(?:SIGURNOSNE\s+PREPORUKE|PREPORUKE|PODUZETE\s+MJERE|PRILO(?:G|ZI))"
    r"[ \t]*:?[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)
_PC_FURNITURE_RE = re.compile(
    r"^[ \t\f]*(?:[_\-\u2014]{10,}"
    r"|Agencija\s+za\s+istra\w*\s+nesre\w*.*"
    r"|Stranica\s+\d+.*|\d+\s*/\s*\d+)[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)
_PC_BULLET_RE = re.compile(
    r"^[ \t\f]*[\u2022\u25aa\u25cf\u25a0\u00b7\u2013\u2014\-\*\uE000-\uF8FF]+[ \t\f]*",
    re.MULTILINE,
)

PROBABLE_CAUSE_MIN = 40
_PC_WINDOW = 6000  # chars; see the note in parse_probable_cause


def parse_probable_cause(text: str) -> str | None:
    """Return the UZROK section as one normalised string, or None."""
    if not text:
        return None
    m = _PC_HEADING_RE.search(text)
    if not m:
        return None
    # Bound the window before looking for the terminator. Bahrain is where this
    # bites: 13 of its 46 captures have no following section heading at all, so
    # an unbounded capture ran to the end of the document — one produced 40,998
    # characters, the whole report filed as a probable cause. It would have
    # passed every length check downstream and read as nonsense on the page.
    #
    # This corpus has no such capture today (0 of 46 here). The bound is added
    # anyway: the failure is silent when it happens, and the same parser shape
    # now lives in three packages — a fix that stays in the one package where
    # the bug surfaced is not a fix for the class.
    #
    # 6000 is taken from the corpora, not chosen for looks: the longest genuine
    # sections are 3,707 (Philippines), 2,227 (Croatia) and 1,831 at Bahrain's
    # 90th percentile.
    rest = text[m.end(): m.end() + _PC_WINDOW]
    end = _PC_TERMINATOR_RE.search(rest)
    body = rest[: end.start()] if end else rest

    body = _PC_FURNITURE_RE.sub("", body)
    body = _PC_BULLET_RE.sub("", body)

    out = []
    for ln in (l.strip() for l in body.splitlines()):
        if not ln:
            continue
        if out and not out[-1].endswith((".", ";", ":")):
            out[-1] = out[-1] + " " + ln
        else:
            out.append(ln)
    joined = re.sub(r"\s+", " ", " ".join(out)).strip()
    # The next section's number sits on its own line before its title:
    #     ...masu zrakoplova.
    #
    #     4.
    #
    #     SIGURNOSNE PREPORUKE
    # The terminator matches the title line, so a bare "4." trails the capture.
    # Stripped here rather than treated as a terminator: a lone number could
    # legitimately open a numbered cause item, and this source's causes use
    # named sub-headings, so removing it from the tail is safe where removing
    # it from the middle would not be.
    joined = re.sub(r"\s+\d+(?:\.\d+)*[.)]?\s*$", "", joined).strip()
    if end is None and len(joined) > PROBABLE_CAUSE_MIN:
        # No terminator: the window decided where this stopped, so cut back to
        # the last sentence rather than ending mid-clause.
        cut = joined.rfind(". ")
        if cut > PROBABLE_CAUSE_MIN:
            joined = joined[: cut + 1]
    return joined if len(joined) >= PROBABLE_CAUSE_MIN else None
