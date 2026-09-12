"""probable_cause is what makes an AIN Croatia page indexable at all.

prod marks a page indexable at a quality score of 50: a narrative over 300
chars scores 30, a probable_cause over 100 scores 20, and factors_json,
weather_summary and phase_of_flight are hardcoded null at projection. Those
two fields are the only route to 50, so a source with no cause parser has no
indexable pages however long its narratives are.

Fixtures are real pdftotext output from reports on the host. Both carry the
agency footer that lands mid-section — the case a hand-written sample would
not have had, and the one that made the terminator vocabulary worth measuring
instead of assuming.
"""
import pathlib

from ainhr_ingest import ainhr

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def _text(name):
    return (FIXTURES / f"{name}.txt").read_text(encoding="utf-8")


FOX = "apollo-fox-daruvar-01-08-2015"
PIPISTREL = "apollo-pipistrel-aerodrom-cepin-10-08-2014"


def test_direct_and_contributing_cause_are_both_kept():
    """Both sub-headings are part of the cause, not the end of it."""
    pc = ainhr.parse_probable_cause(_text(FOX))
    assert pc
    assert "Neposredni uzrok" in pc
    assert "gubitak uzgona" in pc
    assert "Kontributivni" in pc, "the contributing factor was cut off"
    assert "alkohola" in pc


def test_capture_stops_at_the_recommendations():
    for name in (FOX, PIPISTREL):
        pc = ainhr.parse_probable_cause(_text(name))
        assert pc
        assert "SIGURNOSNE PREPORUKE" not in pc


def test_the_agency_footer_is_stripped_not_treated_as_a_heading():
    """It appears as the 'next heading' in 15 of 46 reports.

    Treating it as a terminator would cut the contributing-factor paragraph
    off every one of them; treating it as furniture keeps them whole.
    """
    for name in (FOX, PIPISTREL):
        body = _text(name)
        assert "Agencija za istra" in body, "fixture lost the footer"
        pc = ainhr.parse_probable_cause(body)
        assert pc
        assert "Agencija za istra" not in pc


def test_the_next_sections_number_does_not_trail_the_capture():
    """The number sits on its own line before its title, so it survives the
    terminator match and has to be stripped from the tail."""
    pc = ainhr.parse_probable_cause(_text(PIPISTREL))
    assert pc
    assert not pc.rstrip().endswith("4."), f"trailing section number: {pc[-40:]!r}"
    assert pc.rstrip().endswith(".")


def test_absent_section_returns_none():
    assert ainhr.parse_probable_cause("") is None
    assert ainhr.parse_probable_cause("Izvjestaj bez tog naslova.") is None


def test_extracted_causes_clear_the_hundred_chars_that_prod_scores():
    for name in (FOX, PIPISTREL):
        pc = ainhr.parse_probable_cause(_text(name))
        assert pc and len(pc) >= 100, f"{name}: {len(pc or '')} chars"
