# griaa_ingest/text.py
import html
import re

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
_NONSLUG = re.compile(r"[^a-z0-9]+")


def strip_html(s):
    if not s:
        return ""
    s = _TAG.sub(" ", s)
    s = html.unescape(s)
    return _WS.sub(" ", s).strip()


def slugify(s):
    if not s:
        return ""
    return _NONSLUG.sub("-", s.lower()).strip("-")


def make_site_slug(case_id):
    """Site slug = lowercased case_id with [^a-z0-9]+ -> '-' (mirrors ciaiac/cenipa).

    e.g. 'COL-08-31-GIA' -> 'col-08-31-gia',
         'COL-24-58-DIACC' -> 'col-24-58-diacc'.
    """
    base = slugify(case_id)
    return base if base else "crash-griaa"


# ── case_id normalisation ──────────────────────────────────────────────────────
# Collapse whitespace around the '-' and '/' separators so the same case does
# not appear under two spellings (CENIPA slug-collision lesson: 'A - 013/CENIPA/2013'
# vs 'A-013/CENIPA/2013').  Also uppercases and collapses internal runs of spaces.

_SEP_WS = re.compile(r"\s*([-/])\s*")


def normalize_case_id(raw):
    """Normalise a GRIAA case reference.

    - collapse whitespace around '-' and '/'
    - collapse remaining whitespace runs to a single space, then strip
    - uppercase
    """
    if not raw:
        return ""
    s = html.unescape(raw)
    s = _WS.sub(" ", s).strip()
    s = _SEP_WS.sub(r"\1", s)
    return s.upper()


# ── date parsing ───────────────────────────────────────────────────────────────

def dmy_to_iso(s):
    """Convert DD/MM/YYYY to YYYY-MM-DD, or None on failure."""
    s = (s or "").strip()
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", s)
    if not m:
        return None
    d, mo, y = m.groups()
    return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"


# ── ADVERTENCIA legal-preamble stripping ───────────────────────────────────────
# Every GRIAA report (preliminary and final) opens its body with an
# "ADVERTENCIA" legal disclaimer block.  The wording differs between preliminary
# and final reports, but the block is always bounded:
#   start: a line that is exactly "ADVERTENCIA"
#   end:   the first following structural heading (table of contents / synopsis /
#          acronyms / glossary / index), or a page-number-only line followed by a
#          section heading.
# We remove the block from the ADVERTENCIA header up to (but not including) the
# first such heading.  If no heading is found, fall back to removing the
# ADVERTENCIA header line only (conservative — keeps body text).

_ADV_HEADER = re.compile(r"(?im)^[ \t\f]*ADVERTENCIA[ \t\f]*$")

# Structural headings that mark the end of the preamble (start of real content).
_PREAMBLE_END = re.compile(
    r"(?im)^[ \t\f]*("
    r"TABLA\s+DE\s+CONTENIDO"
    r"|CONTENIDO"
    r"|[ÍI]NDICE"
    r"|SINOPSIS"
    r"|SIGLAS(?:\s+Y\s+ABREVIATURAS)?"
    r"|GLOSARIO"
    r"|ABREVIATURAS"
    r"|RESUMEN"
    r")\b"
)


def strip_advertencia(text):
    """Remove the leading ADVERTENCIA legal-preamble block from report text.

    Returns the text with the disclaimer removed and surrounding whitespace
    tidied.  No-op (returns the input, stripped) when no ADVERTENCIA header is
    present.
    """
    if not text:
        return text or ""
    m = _ADV_HEADER.search(text)
    if not m:
        return text.strip()
    head = text[: m.start()]
    rest = text[m.end():]
    end = _PREAMBLE_END.search(rest)
    if end:
        tail = rest[end.start():]
    else:
        # No structural heading found: drop only the ADVERTENCIA header line,
        # keep the body to avoid eating real content.
        tail = rest
    combined = (head.rstrip() + "\n\n" + tail.lstrip()).strip()
    return combined
