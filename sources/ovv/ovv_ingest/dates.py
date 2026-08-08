"""Recover the occurrence date OVV states on the page but not in the title.

205 of the 386 projected rows had no event_date. The scraper's only source was
`_DATE_RE` applied to the page title, and OVV titles are descriptive — "Tail
strike during take-off, Boeing 737-800, Rotterdam Airport" — so a date was
found only when one happened to be written into the headline.

Two other places carry a date, and neither can be trusted alone.

The investigation page has a labelled field:

    Investigation start date 18 July 2014   Publish date report 13 October 2015

Calibrated on 2026-08-06 against 14 rows whose occurrence date is already
known, that field was exact for 12 of them — and wrong by +39 and +73 days for
the other two, where it records when the investigation was opened rather than
when the occurrence happened. A 14% error rate, some of it months wide, is not
something to write into a safety corpus.

The report text states the occurrence date too, but in at least four layouts
across Dutch and English, and the page footer puts a publication date right
beside it ("The Hague, may 24, 2007"). Anchoring on the text alone reached only
46 of the 195.

So the two are used together: the start date is accepted only when the very
same date also appears somewhere in the report. A report says what happened and
when; it does not say when a file was opened. On the calibration sample that
rule rejected both wrong dates and kept three of the four right ones — no false
positives, one conservative miss — and across the whole dateless cohort it
corroborates 156 of 195.

Twin discipline to nsib_ingest/dates.py: two independent witnesses, and silence
rather than a guess.
"""

import re
from datetime import date

EN_MONTHS = ["january", "february", "march", "april", "may", "june", "july",
             "august", "september", "october", "november", "december"]
NL_MONTHS = ["januari", "februari", "maart", "april", "mei", "juni", "juli",
             "augustus", "september", "oktober", "november", "december"]

_MONTH_INDEX = {name: i + 1 for i, name in enumerate(EN_MONTHS)}

# The labelled field on the investigation page. Tags are stripped first, so
# this works whether the value sits in a <dd>, a <span> or bare text.
_START_RE = re.compile(
    r"Investigation start date\s*:?\s*(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})",
    re.I,
)

_TAGS = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def _flatten(html):
    return _WS.sub(" ", _TAGS.sub(" ", html or "")).strip()


def parse_start_date(html):
    """Read 'Investigation start date' off the detail page. ISO or None."""
    text = _flatten(html)
    if not text:
        return None
    match = _START_RE.search(text)
    if not match:
        return None
    month = _MONTH_INDEX.get(match.group(2).lower())
    if not month:
        return None
    try:
        return date(int(match.group(3)), month, int(match.group(1))).isoformat()
    except ValueError:
        return None


def _spellings(day):
    """Every way this corpus writes one date, Dutch and English."""
    en, nl = EN_MONTHS[day.month - 1], NL_MONTHS[day.month - 1]
    return (
        f"{day.day} {en} {day.year}",
        f"{day.day} {nl} {day.year}",
        f"{day.day:02d} {en} {day.year}",
        f"{day.day:02d} {nl} {day.year}",
        f"{en} {day.day}, {day.year}",
        f"{en} {day.day} {day.year}",
        day.isoformat(),
        f"{day.day}-{day.month}-{day.year}",
        f"{day.day:02d}-{day.month:02d}-{day.year}",
    )


def corroborated_date(start_date, narrative_text):
    """Return start_date only if the report text states that same date.

    The second witness is what separates an occurrence date from the day a
    file was opened. An exact match is required: being one day out is the
    error this exists to catch, so a near miss is a refusal, not a rounding.
    """
    if not start_date or not narrative_text:
        return None
    try:
        day = date.fromisoformat(str(start_date))
    except (ValueError, TypeError):
        return None

    haystack = _WS.sub(" ", str(narrative_text)).lower()
    if any(form in haystack for form in _spellings(day)):
        return day.isoformat()
    return None
