"""probable_cause is what decides whether an AAIIB page is indexable at all.

prod marks a page indexable at a quality score of 50. A narrative over 300
chars scores 30 and a probable_cause over 100 scores 20, while factors_json,
weather_summary and phase_of_flight are hardcoded null at projection. Those
two fields are the only route to 50 — so a source with 169 rows and no
probable_cause has 169 rows that can never be indexed, however long their
narratives are.

Fixtures are real pdftotext output from reports on the host, not hand-written
samples: the form-feed case below was invisible until the parser was run
against the actual corpus, and a fixture I wrote myself would not have had it.
"""
import pathlib

from aaiib_ingest import aaiib

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def _text(name):
    return (FIXTURES / f"{name}.txt").read_text(encoding="utf-8")


def test_prose_cause_is_extracted():
    pc = aaiib.parse_probable_cause(_text("AAIIB-2008-RP-C1124"))
    assert pc
    assert pc.startswith("The pilot-in-command")
    assert "porpoise induced oscillation" in pc
    # stops at the next heading rather than swallowing the recommendations
    assert "SAFETY RECOMMENDATIONS" not in pc
    assert "evaluation of the quality of instructors" not in pc


def test_bulleted_cause_is_flattened():
    pc = aaiib.parse_probable_cause(_text("AAIIB-2008-RP-C1950"))
    assert pc
    assert "Inadequate supply of oil" in pc
    assert "clogged up oil screen" in pc
    # bullet glyphs are gone; prod renders this as one field
    for glyph in ("", "•", "▪"):
        assert glyph not in pc


def test_heading_after_a_page_break_is_found():
    """pdftotext puts \\f before a heading that starts a page.

    The first version of this parser anchored on [ \\t] only, so every report
    whose PROBABLE CAUSE began a page was silently skipped — 36 of 187, a
    fifth of the corpus, reported as 'no cause in the document'.
    """
    body = _text("AAIIB-2010-RP-C7254")
    assert "\f" in body[: body.index("PROBABLE CAUSE")], "fixture lost its page break"
    pc = aaiib.parse_probable_cause(body)
    assert pc and len(pc) >= 100


def test_page_furniture_is_stripped():
    for name in ("AAIIB-2008-RP-C1124", "AAIIB-2008-RP-C1950", "AAIIB-2010-RP-C7254"):
        pc = aaiib.parse_probable_cause(_text(name))
        if not pc:
            continue
        assert "Page 1 of" not in pc
        assert "Investigation Report RP-" not in pc


def test_absent_section_returns_none():
    assert aaiib.parse_probable_cause("") is None
    assert aaiib.parse_probable_cause("A report with no such heading at all.") is None


def test_a_heading_with_nothing_under_it_returns_none():
    """A bare heading echo is not a cause; emitting it would score 0 anyway."""
    assert aaiib.parse_probable_cause("PROBABLE CAUSE\n\nSAFETY RECOMMENDATIONS\n") is None


def test_the_extracted_causes_are_long_enough_to_score():
    """The point of the exercise: >=100 chars is what prod's score needs."""
    scored = [
        name for name in ("AAIIB-2008-RP-C1124", "AAIIB-2008-RP-C1950", "AAIIB-2010-RP-C7254")
        if (aaiib.parse_probable_cause(_text(name)) or "") and
           len(aaiib.parse_probable_cause(_text(name))) >= 100
    ]
    assert len(scored) == 3, f"only {scored} clear 100 chars"
