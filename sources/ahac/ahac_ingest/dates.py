"""Recover the occurrence date AHAC states in the report but not in the index.

The listing gives filenames only, so every one of the 34 AHAC documents on prod
reached the corpus with no event_date — and the occurrences loader drops
dateless rows, which makes the whole bureau invisible. The date is in the PDF
text, in one of two layouts:

  Fecha de Accidente:  12-Enero-2025          (preliminary / provisional)
  FECHA…………………………….23 DE ABRIL DEL 2010     (older finals, dot leaders)
  "... el día 20 de octubre del año 2018,     (finals, synopsis sentence)
   aproximadamente a las 1530 UTC"

What makes this worth writing carefully is what else is in those documents.
"First Spanish date in the text" would find a date in nearly every one of them
and would be wrong in most:

  "a partir de la fecha en que ocurrió el accidente"   Annex 13 boilerplate,
                                                       present in most finals
  "con fecha de expiración 30/Julio/2025"              a licence expiry
  "Clase I se emitió el 17/Enero/2025"                 a licence issue
  "Año de Fabricación: 2009"                           the airframe build year

So the anchor is the field label, never proximity. A document without one of
the two labels stays dateless. That is deliberate: this corpus already had 30
rows stamped with an agency's founding date by a recovery that took the first
date it saw, and a wrong date in a safety corpus is worse than a missing one.
"""

import re
from datetime import date as _date

MONTHS = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12,
}

_MONTH_NAMES = "|".join(MONTHS)

# 12-Enero-2025 · 03 julio 2023 · 23 DE ABRIL DEL 2010 · 1/Diciembre/2021
# · 10 de enero del presente año 2019
_PHRASE = (
    r"(\d{1,2})\s*(?:[-/]|\s+de\s+|\s+)\s*(" + _MONTH_NAMES + r")"
    r"\s*(?:[-/]|\s+del?\s+|\s+)\s*(?:presente\s+)?(?:año\s+)?(\d{4})"
)
_PHRASE_RE = re.compile(_PHRASE, re.IGNORECASE)

# The explicit field. Bounded to a short window so it cannot run past the
# field's own value into the next paragraph, and anchored on the noun so
# "fecha de expiración" and "fecha en que ocurrió" cannot match.
_LABEL_RE = re.compile(
    r"Fecha\s+(?:de\s+|del\s+)?(?:Accidente|Incidente|Suceso|Ocurrencia|Evento)"
    r"\s*:?\s*(.{0,30})",
    re.IGNORECASE | re.DOTALL,
)

# The dot-leader table row: FECHA followed by leaders, then the value.
_TABLE_RE = re.compile(
    r"\bFECHA[\s.…]{3,}(.{0,30})",
    re.IGNORECASE | re.DOTALL,
)

# The synopsis sentence in the final reports:
#   "... el día 20 de octubre del año 2018, aproximadamente a las 1530 UTC"
#
# The date is required to be followed closely by a clock time. That pairing is
# what separates the occurrence from the licence and certificate dates written
# in identical wording elsewhere in the same document — none of those is
# followed by a time. A date on its own is not enough here.
_SYNOPSIS_RE = re.compile(
    _PHRASE + r".{0,40}?\ba\s+las\s+\d",
    re.IGNORECASE | re.DOTALL,
)


def _iso(day, month_word, year):
    """Build an ISO date, or None if it is not a real past date."""
    month = MONTHS.get(month_word.lower())
    if not month:
        return None
    try:
        parsed = _date(int(year), month, int(day))
    except ValueError:
        return None  # 31 February and friends
    if parsed > _date.today():
        return None  # a mistyped year is not a date we can use
    return parsed.isoformat()


def _first_phrase(fragment):
    match = _PHRASE_RE.search(fragment or "")
    if not match:
        return None
    return _iso(match.group(1), match.group(2), match.group(3))


def recover_event_date(narrative_text):
    """Return (iso_date, basis) or (None, None).

    basis is 'label' for the explicit Fecha de Accidente field and 'table' for
    the dot-leader row. The label wins when a document carries both.
    """
    text = re.sub(r"\s+", " ", str(narrative_text or ""))
    if not text:
        return (None, None)

    for match in _LABEL_RE.finditer(text):
        iso = _first_phrase(match.group(1))
        if iso:
            return (iso, "label")

    for match in _TABLE_RE.finditer(text):
        iso = _first_phrase(match.group(1))
        if iso:
            return (iso, "table")

    for match in _SYNOPSIS_RE.finditer(text):
        iso = _iso(match.group(1), match.group(2), match.group(3))
        if iso:
            return (iso, "synopsis")

    return (None, None)
