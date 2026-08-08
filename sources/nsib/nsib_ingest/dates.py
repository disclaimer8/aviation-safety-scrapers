"""Recover the occurrence date NSIB never publishes.

The NSIB API carries no occurrence date — ``published_at`` is the publication
date and trails the event by up to a year — so every record discovered through
``wp_rest`` was inserted with ``date_of_occurrence = None``. Downstream that
means no ``event_date`` in source_accidents, and the occurrences loader drops
dateless rows: 328 documents on prod projected to 138 occurrences.

Two partial sources exist and neither is sufficient alone. Measured over all
150 dateless rows on 2026-08-06:

  case id   28 unambiguously YYYY/MM/DD (third component > 12)
             1 unambiguously YYYY/DD/MM (NPF/2022/26/01 — 26 is no month)
            25 readable both ways: 10 July or 7 October, nothing to choose
            96 a different scheme with no date (NSIB-FIN-5N-PAN)

  prose    111 state it in words, in NSIB's house ordinal style

The id supplies the digits; the prose supplies their order. Together they date
122 of the 150.

The id wins when it carries a date. NSIB's opening sentence usually gives the
date the Bureau was NOTIFIED, which trails the occurrence by a day
(DANAL/2019/01/23, "in the evening of 24th January, 2019") or by two weeks
(SEA/2019/11/19, "notified … on the 2nd December, 2019. On 19th November,
2019, at about 08:45") — while the case number belongs to the occurrence. The
prose is what says which way round to read the digits.

Nothing is guessed: an ambiguous id with no matching prose date stays dateless,
and a genuine disagreement is reported rather than resolved. A wrong date in a
safety corpus is worse than a missing one.

Twin of server/src/services/nsibDateRecovery.js — keep the two in step.
"""

import re
from datetime import date as _date, datetime as _datetime, timezone as _timezone

MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}
_MONTH_NAMES = "|".join(MONTHS)

# NSIB's oldest report is from the 1970s; outside this range it is a parse
# artefact rather than an occurrence.
MIN_YEAR = 1950

_ID_RE = re.compile(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})")
# NSIB's house style is the ordinal, with an optional "of": "10th September,
# 2020", "3rd of August 2019". Requiring whitespace straight after the day
# number missed 26 of the 150 rows.
_DMY_RE = re.compile(
    r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?(" + _MONTH_NAMES + r")\.?,?\s+(\d{4})\b",
    re.IGNORECASE,
)
_MDY_RE = re.compile(
    r"\b(" + _MONTH_NAMES + r")\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b",
    re.IGNORECASE,
)


def _max_year():
    return _datetime.now(_timezone.utc).year + 1


def _iso(year, month, day):
    """A real calendar date in range as YYYY-MM-DD, else None. 31 Feb is not one."""
    if not year or not month or not day:
        return None
    if year < MIN_YEAR or year > _max_year():
        return None
    try:
        return _date(year, month, day).isoformat()
    except ValueError:
        return None


def from_case_id(case_id):
    """Digits and provable ordering from `UNA/2021/11/17/F`.

    Returns {'year', 'a', 'b', 'order'} with order in {'ymd','ydm','ambiguous'},
    or None when the id carries no date.
    """
    m = _ID_RE.search(case_id or "")
    if not m:
        return None
    year, a, b = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if a > 12 and b > 12:
        return None  # neither reading is a month
    if a > 12:
        order = "ydm"
    elif b > 12:
        order = "ymd"
    else:
        order = "ambiguous"
    return {"year": year, "a": a, "b": b, "order": order}


def from_narrative(text):
    """The date NSIB states in prose, or None."""
    s = text or ""
    if not s:
        return None
    m = _DMY_RE.search(s)
    if m:
        iso = _iso(int(m.group(3)), MONTHS[m.group(2).lower()], int(m.group(1)))
        if iso:
            return iso
    m = _MDY_RE.search(s)
    if m:
        iso = _iso(int(m.group(3)), MONTHS[m.group(1).lower()], int(m.group(2)))
        if iso:
            return iso
    return None


def recover_event_date(case_id, narrative_text):
    """Return (date, basis) where basis is 'narrative+id' | 'id' | 'narrative' | None.

    basis records which evidence carried the date so a run can be audited
    afterwards rather than taken on trust. A disagreement returns
    (None, 'conflict').
    """
    from_id = from_case_id(case_id)
    from_text = from_narrative(narrative_text)

    id_ymd = _iso(from_id["year"], from_id["a"], from_id["b"]) if from_id and from_id["order"] != "ydm" else None
    id_ydm = _iso(from_id["year"], from_id["b"], from_id["a"]) if from_id and from_id["order"] != "ymd" else None

    # The prose confirms the id's ordering — that is its main job here.
    if from_text and from_text in (id_ymd, id_ydm):
        return from_text, "narrative+id"

    # An unambiguous id beats a prose date that disagrees: the prose is usually
    # the notification, the case number is the occurrence.
    if from_id and from_id["order"] == "ymd" and id_ymd:
        return id_ymd, "id"
    if from_id and from_id["order"] == "ydm" and id_ydm:
        return id_ydm, "id"

    # An ambiguous id the prose does not match resolves nothing.
    if from_id and from_text:
        return None, "conflict"

    # No date in the id at all (the NSIB-FIN-* scheme): prose is all there is.
    if from_text:
        return from_text, "narrative"

    return None, None
