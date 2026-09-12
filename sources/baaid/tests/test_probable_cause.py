"""probable_cause is what makes a Bahrain AAIA page indexable at all.

prod marks a page indexable at a quality score of 50: a narrative over 300
chars scores 30, a probable_cause over 100 scores 20, and factors_json,
weather_summary and phase_of_flight are hardcoded null at projection. Those
two fields are the only route to 50.

Fixtures are real pdftotext output from reports on the host, chosen to cover
the three shapes that matter: a section that ends at a terminator, one that
does not, and the report whose unbounded capture ran to 41,099 characters.
"""
import pathlib

from baaid_ingest import baaid

FIXTURES = pathlib.Path(__file__).parent / "fixtures"

WITH_TERM = "28a217-653c1e05b6d748619a83a399c0c70833"
NO_TERM = "28a217-23ddf383c73443948d06b62889d25881"
RUNAWAY = "fbeb16-runaway-no-terminator"


def _text(name):
    return (FIXTURES / f"{name}.txt").read_text(encoding="utf-8")


def test_cause_is_extracted_and_stops_at_the_recommendations():
    pc = baaid.parse_probable_cause(_text(WITH_TERM))
    assert pc
    assert "controlled flight into terrain" in pc
    assert "Recommendations" not in pc
    assert "The AAIA has recommended to the Commissioner" not in pc


def test_contributing_factors_are_kept_not_treated_as_the_end():
    """They ARE the cause here — CONTRIBUTING FACTORS leads the terminator
    vocabulary (11 of the measured headings), and taking the most frequent
    following heading for the end of the section would truncate a fifth of the
    corpus at exactly the wrong line."""
    pc = baaid.parse_probable_cause(_text(WITH_TERM))
    assert pc
    assert "Contributing factors" in pc
    assert "Failure of the crew to configure the aircraft" in pc


def test_bullets_are_flattened_including_the_private_use_area_glyph():
    pc = baaid.parse_probable_cause(_text(NO_TERM))
    assert pc
    for glyph in ("", "•", "▪"):
        assert glyph not in pc


def test_an_unbounded_capture_is_bounded():
    """The case the window exists for.

    This report has no section heading after PROBABLE CAUSE, so an unbounded
    capture takes the rest of the document — 41,099 characters, the whole
    report filed as a probable cause. It would have cleared every length check
    downstream and rendered as nonsense.
    """
    body = _text(RUNAWAY)
    m = baaid._PC_HEADING_RE.search(body)
    assert m, "fixture lost its heading"
    assert not baaid._PC_TERMINATOR_RE.search(body[m.end():]), \
        "fixture gained a terminator; it no longer covers the unbounded case"
    assert len(body) - m.end() > 40000, "fixture no longer runs long"

    pc = baaid.parse_probable_cause(body)
    assert pc
    assert len(pc) <= baaid._PC_WINDOW, f"capture escaped the window: {len(pc)}"


def test_a_window_truncated_capture_ends_on_a_sentence():
    """When the window rather than a terminator decided where to stop."""
    for name in (NO_TERM, RUNAWAY):
        body = _text(name)
        m = baaid._PC_HEADING_RE.search(body)
        assert not baaid._PC_TERMINATOR_RE.search(body[m.end():])
        pc = baaid.parse_probable_cause(body)
        assert pc and pc.rstrip().endswith("."), f"{name} ends mid-clause: {pc[-60:]!r}"


def test_absent_section_returns_none():
    assert baaid.parse_probable_cause("") is None
    assert baaid.parse_probable_cause("A report with no such heading.") is None


def test_extracted_causes_clear_the_hundred_chars_prod_scores():
    for name in (WITH_TERM, NO_TERM, RUNAWAY):
        pc = baaid.parse_probable_cause(_text(name))
        assert pc and len(pc) >= 100, f"{name}: {len(pc or '')}"
