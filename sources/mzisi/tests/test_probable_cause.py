"""probable_cause is what makes a Slovenian report's page indexable at all.

prod marks a page indexable at a quality score of 50: a narrative over 300
chars scores 30, a probable_cause over 100 scores 20, and factors_json,
weather_summary and phase_of_flight are hardcoded null at projection.

Fixtures are real pdftotext output. Both carry the two things that make this
source harder than the English ones: the heading also appears in the table of
contents with dot leaders, and "KONČNO POROČILO" — the running header after a
page break — is the second most frequent heading following the section.
"""
import pathlib
import re

from mzisi_ingest import mzisi

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
STORCH = "mzisi-2007-uln-storch-ii-s5-paj-koncno-poroc"
AN2 = "mzisi-2008-an-2-ha-mkk-koncno-porocilo-o-pre"


def _text(name):
    return (FIXTURES / f"{name}.txt").read_text(encoding="utf-8")


def test_cause_is_extracted():
    pc = mzisi.parse_probable_cause(_text(STORCH))
    assert pc
    assert "odpoved delovanja motorja" in pc
    assert "zaledenitvijo uplinja" in pc


def test_the_contents_entry_is_not_mistaken_for_the_section():
    """9 of the 69 reports have ONLY a contents entry; taking it would capture
    the contents page as the cause."""
    for name in (STORCH, AN2):
        body = _text(name)
        assert re.search(r"VZROK[^\n]*[.\-_]{5,}", body, re.I), \
            "fixture lost its contents entry"
        pc = mzisi.parse_probable_cause(body)
        assert pc
        assert not re.search(r"[.\-_]{5,}", pc), f"dot leaders leaked: {pc[:80]!r}"


def test_the_running_header_is_stripped_not_treated_as_a_heading():
    """KONČNO POROČILO follows a page break and carries the registration on the
    same line, so it cannot be matched with $ straight after the phrase — that
    missed 4 of 35 captures and put 'KONČNO POROČILO An-2 HA-MKK' inside a
    cause."""
    for name in (STORCH, AN2):
        body = _text(name)
        assert re.search(r"KON\w*NO PORO", body, re.I), "fixture lost the header"
        pc = mzisi.parse_probable_cause(body)
        assert pc
        assert not re.search(r"KON\w*NO\s+PORO", pc, re.I)


def test_the_agency_name_survives_when_it_opens_a_real_sentence():
    """It is a header on its own line and a finding mid-sentence.

    "Služba za preiskovanje ... nima varnostnih priporočil" says the Service
    has no safety recommendations. Stripping every line containing the phrase
    would delete that finding, so only the whole-line header form is removed.
    """
    sentence = (
        "Vzrok nesreče\n"
        "Pilot je izgubil nadzor nad zrakoplovom med pristankom v močnem "
        "bočnem vetru, kar je privedlo do trka s tlemi in poškodb zrakoplova. "
        "Služba za preiskovanje letalskih, pomorskih in železniških nesreč in "
        "incidentov nima varnostnih priporočil.\n"
        "VARNOSTNA PRIPOROČILA\n"
    )
    pc = mzisi.parse_probable_cause(sentence)
    assert pc
    assert "nima varnostnih priporo" in pc, "a real finding was stripped as furniture"


def test_the_misspelled_terminator_still_ends_the_section():
    """One report writes VAROSTNO for VARNOSTNO. Tolerated rather than lost."""
    body = (
        "Vzrok nesreče\n"
        "Odpoved motorja zaradi pomanjkanja goriva v obeh rezervoarjih med "
        "priblizevanjem letalisc, kar je povzrocilo zasilni pristanek na njivi.\n"
        "VAROSTNO PRIPOROČILO\n"
        "Direktorat naj uvede postopek.\n"
    )
    pc = mzisi.parse_probable_cause(body)
    assert pc
    assert "Direktorat naj uvede" not in pc


def test_absent_section_returns_none():
    assert mzisi.parse_probable_cause("") is None
    assert mzisi.parse_probable_cause("Poročilo brez tega naslova.") is None
