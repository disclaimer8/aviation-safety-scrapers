"""The build floor has to agree with what production does with the row.

FlightFinder's server/scripts/build-source-narratives.js carries
`NARRATIVE_MIN = 300  // floor: render noindex (score 30), never 410`, so a row
whose narrative is shorter than 300 becomes a page that exists and is never
indexed. Fifteen packages shipped at 80, which could only ever produce those
pages — and index bloat has cost this project a traffic cliff once already.

This test does not import FlightFinder (different repository). It pins the
number and says where it came from, so a future change is a deliberate one.
"""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent

# FlightFinder server/scripts/build-source-narratives.js, checked 2026-09-11.
PROD_NARRATIVE_MIN = 300

# Per-source exceptions, in either direction, each with its reason. The name
# and the old comment said "nothing may be LOOSER", but ntsbcarol has always
# been 200 — looser than prod — so the rule as written never matched the rule
# as enforced. Corrected here rather than left as a trap for the next reader.
#
# Looser is allowed because _NARRATIVE_FLOOR gates building the occurrence
# ROW, not just its page. A row below prod's minimum renders noindex, but its
# structured fields — date, registration, aircraft type, event class — are
# still the occurrence. Dropping it loses data, not only a page. A source may
# take that trade deliberately; it may not take it silently.
BY_DESIGN = {
    "ntsbcarol": 200,   # CAROL abstracts are short by nature
    "ntsbaar": 1000,    # "an AAR is a long document; less than this is a scan"
    "aaibmn": 30,       # scans with no text layer; the listing title carries
                        # the date, aircraft and registration, and is the only
                        # narrative these rows will ever have
}
STRICTER_BY_DESIGN = BY_DESIGN  # kept: existing name, corrected meaning

_FLOOR = re.compile(r"^_NARRATIVE_FLOOR\s*=\s*(\d+)", re.M)


def _floors():
    found = {}
    for path in sorted(ROOT.glob("sources/*/*_ingest/pipeline.py")):
        code = path.parts[-3]
        m = _FLOOR.search(path.read_text(encoding="utf-8"))
        if m:
            found[code] = int(m.group(1))
    return found


def test_no_source_admits_rows_prod_would_noindex():
    floors = _floors()
    assert floors, "no _NARRATIVE_FLOOR found at all — has the constant moved?"
    too_low = {
        code: value for code, value in floors.items()
        if value < PROD_NARRATIVE_MIN and STRICTER_BY_DESIGN.get(code) != value
    }
    assert not too_low, (
        f"{too_low} admit rows below prod's NARRATIVE_MIN ({PROD_NARRATIVE_MIN}), "
        "so they can only ever produce noindex pages. Raise the floor, or add "
        "the source to STRICTER_BY_DESIGN with the reason."
    )


def test_the_documented_exceptions_still_exist():
    # If one is retired, this list should shrink rather than quietly grant an
    # exemption to whatever takes its name.
    floors = _floors()
    for code, expected in STRICTER_BY_DESIGN.items():
        assert code in floors, f"{code} no longer defines a floor — drop it from the list"
        assert floors[code] == expected, (
            f"{code} floor is {floors[code]}, not the documented {expected}"
        )


def test_the_two_constants_are_not_confused():
    # MIN_NARRATIVE (PDF text-layer quality, canonically 600) and
    # _NARRATIVE_FLOOR (build admission) answer different questions. A package
    # that sets its build floor from the OCR threshold has conflated them.
    for path in sorted(ROOT.glob("sources/*/*_ingest/pipeline.py")):
        body = path.read_text(encoding="utf-8")
        assert not re.search(r"^_NARRATIVE_FLOOR\s*=\s*MIN_NARRATIVE", body, re.M), (
            f"{path.parts[-3]} sets its build floor from MIN_NARRATIVE, which is "
            "the PDF text-layer threshold, not the admission gate"
        )
